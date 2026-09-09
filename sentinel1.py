"""
sentinel1.py
------------
Finds and preprocesses pre-flood / post-flood Sentinel-1 GRD scenes for a
given flood event, with **explicit, configurable temporal-selection
criteria** and **a recorded rationale** for whichever scene is chosen.

Collection: COPERNICUS/S1_GRD (C-band GRD, IW mode).
Values are used as provided by GEE (calibrated sigma0 in dB); no manual
dB conversion is performed, per the thesis notes.
"""

from __future__ import annotations

import ee

import gee_config as config


def _base_collection(region: ee.Geometry) -> ee.ImageCollection:
    """The candidate Sentinel-1 stack for `region`: IW, dual-pol VV+VH."""
    coll = (
        ee.ImageCollection(config.S1_COLLECTION)
        .filterBounds(region)
        .filter(ee.Filter.eq("instrumentMode", config.S1_INSTRUMENT_MODE))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .select(config.S1_POLARIZATIONS)
    )
    return coll


def _image_date_str(img: ee.Image) -> str:
    """Format an image's system:time_start as YYYY-MM-DD."""
    return ee.Date(img.get("system:time_start")).format("YYYY-MM-dd").getInfo()


def _scene_info(img: ee.Image) -> dict:
    """
    Record the properties needed for reproducibility / selection reason.
    Only properties that actually exist on COPERNICUS/S1_GRD are read:
    orbitProperties_pass, relativeOrbitNumber_start, missionID.
    """
    props = img.toDictionary([
        "orbitProperties_pass",
        "relativeOrbitNumber_start",
        "relativeOrbitNumber_stop",
        "missionID",
        "satelliteNumber",
    ]).getInfo()
    return {
        "date": _image_date_str(img),
        **props,
    }


def is_cloud_ok(region: ee.Geometry) -> bool:
    """
    True if there is at least one IW VV+VH scene over `region` at all.
    Used only for a cheap pre-check; the real selection is in
    match_pre_post_scenes.
    """
    return _base_collection(region).size().getInfo() > 0


def find_post_flood_scene(
    region: ee.Geometry,
    flood_date: str,
    window_days: int = config.POST_FLOOD_SEARCH_WINDOW_DAYS,
    preferred_pass: str | None = None,
) -> tuple[ee.Image, dict]:
    """
    Earliest Sentinel-1 scene at-or-after flood_date, within window_days.

    Rationale for "earliest at-or-after": minimizes floodwater recession
    before observation — the most important scene property for capturing
    the actual event extent. Configurable via window_days.

    If `preferred_pass` is given (e.g. "DESCENDING"), first try only scenes
    on that pass; if none exist in the window, fall back to the earliest
    scene on any pass. This keeps relaxed basins on a same-pass pre/post
    pair when one exists, while not hard-failing when it doesn't.

    Returns (image, info) where info records date/pass/relative orbit.
    """
    start = ee.Date(flood_date)
    end = start.advance(window_days, "day")
    coll = (
        _base_collection(region)
        .filterDate(start, end)
        .sort("system:time_start")          # ascending -> earliest first
    )

    if preferred_pass is not None:
        same_pass = coll.filter(
            ee.Filter.eq("orbitProperties_pass", preferred_pass)
        )
        if same_pass.size().getInfo() > 0:
            coll = same_pass

    size = coll.size().getInfo()
    if size == 0:
        raise RuntimeError(
            f"No post-flood S1 scene found for {flood_date} within "
            f"{window_days} day(s)."
        )
    img = ee.Image(coll.first())
    # Reason for selection
    info = _scene_info(img)
    pass_note = f" (preferred_pass={preferred_pass})" if preferred_pass else ""
    info["reason"] = (
        f"Earliest valid S1 IW VV+VH scene at-or-after flood_date "
        f"{flood_date}, within {window_days}-day post window "
        f"({start.format('YYYY-MM-dd').getInfo()} .. "
        f"{end.format('YYYY-MM-dd').getInfo()}){pass_note}."
    )
    return img, info


def find_pre_flood_scene(
    region: ee.Geometry,
    flood_date: str,
    required_orbit_pass: str | None = None,
    window_days: int = config.PRE_FLOOD_SEARCH_WINDOW_DAYS,
    min_gap_days: int = config.PRE_FLOOD_MIN_GAP_DAYS,
) -> tuple[ee.Image, dict]:
    """
    Latest Sentinel-1 scene strictly BEFORE (flood_date - min_gap_days),
    within window_days, optionally restricted to `required_orbit_pass`.

    Rationale for "latest before": keeps land-cover conditions as close to
    the post-event state as possible, so the pre/post *difference* is
    attributable to the flood rather than unrelated land-cover change over
    a long gap. `min_gap_days` avoids catching early-arriving rain signal.

    Returns (image, info) with reason.
    """
    end = ee.Date(flood_date).advance(-min_gap_days, "day")
    start = ee.Date(flood_date).advance(-window_days, "day")
    coll = _base_collection(region).filterDate(start, end)
    if required_orbit_pass:
        coll = coll.filter(ee.Filter.eq("orbitProperties_pass", required_orbit_pass))
    coll = coll.sort("system:time_start", False)   # descending -> latest first
    size = coll.size().getInfo()
    if size == 0:
        req = f", pass={required_orbit_pass}" if required_orbit_pass else ""
        raise RuntimeError(
            f"No pre-flood S1 scene found for {flood_date} within "
            f"{window_days} day(s), min_gap={min_gap_days} day(s){req}."
        )
    img = ee.Image(coll.first())
    info = _scene_info(img)
    orbit_part = f", pass={required_orbit_pass}" if required_orbit_pass else ""
    info["reason"] = (
        f"Latest valid S1 IW VV+VH scene strictly before flood_date "
        f"{flood_date} (min {min_gap_days} day gap), within {window_days}-day "
        f"pre window ({start.format('YYYY-MM-dd').getInfo()} .. "
        f"{end.format('YYYY-MM-dd').getInfo()}){orbit_part}."
    )
    return img, info


def match_pre_post_scenes(
    region: ee.Geometry, flood_date: str, basin: str = ""
) -> tuple[ee.Image, ee.Image, dict, dict]:
    """
    Finds a matched pre/post Sentinel-1 pair for an event, enforcing that
    both scenes share the same orbit pass when
    config.S1_REQUIRE_MATCHING_ORBIT_PASS is True — UNLESS the basin is
    listed in config.S1_MATCH_ORBIT_PASS_BY_BASIN as False, in which case
    pass-matching is relaxed for that event (some basins have no coverage on
    one pass around the flood; forcing same-pass there yields missing_pre_s1).

    Returns (pre_img, post_img, pre_info, post_info).
    """
    require_match = bool(config.S1_REQUIRE_MATCHING_ORBIT_PASS)
    if basin in config.S1_MATCH_ORBIT_PASS_BY_BASIN:
        require_match = bool(config.S1_MATCH_ORBIT_PASS_BY_BASIN[basin])

    if require_match:
        # Strict path: pick the post scene, then force the pre onto the same
        # orbit pass so incidence-angle geometry doesn't confound the signal.
        post_img, post_info = find_post_flood_scene(region, flood_date)
        # NOTE: _scene_info() stores this under the literal EE property name
        # "orbitProperties_pass" (via toDictionary), not "orbit_pass".
        # Previously this line read post_info["orbit_pass"], which raised a
        # KeyError as soon as a post-flood scene was actually found.
        required_pass = post_info["orbitProperties_pass"]
        post_info["pass_match_required"] = True
        pre_img, pre_info = find_pre_flood_scene(
            region, flood_date, required_orbit_pass=required_pass
        )
    else:
        # Relaxed path (basin override): the basin may have zero coverage on
        # one orbit pass around the event (e.g. Pampanga ASC orbit-142 has no
        # pre-flood scene). Pick the pre scene first (unconstrained, latest
        # before flood), then pick the post scene preferring the SAME pass as
        # the pre when one exists, else the earliest scene of any pass.
        pre_img, pre_info = find_pre_flood_scene(region, flood_date)
        post_img, post_info = find_post_flood_scene(
            region, flood_date, preferred_pass=pre_info.get("orbitProperties_pass")
        )
        post_info["pass_match_required"] = False

    pre_info["pass_match_required"] = require_match

    return pre_img, post_img, pre_info, post_info


def _speckle_filter(img: ee.Image) -> ee.Image:
    """
    Apply the configured speckle filter to a scene. focal_median is the
    default baseline (fast, adequate). refined_lee is not implemented.
    """
    if config.SPECKLE_FILTER is None:
        return img
    if config.SPECKLE_FILTER == "focal_median":
        radius_px = config.SPECKLE_KERNEL_RADIUS_M / config.TARGET_RESOLUTION_M
        return img.focalMedian(radius=radius_px, units="pixels")
    if config.SPECKLE_FILTER == "refined_lee":
        raise NotImplementedError(
            "refined_lee is not implemented yet — use 'focal_median' or None "
            "in gee_config.SPECKLE_FILTER."
        )
    raise ValueError(f"Unknown SPECKLE_FILTER: {config.SPECKLE_FILTER}")


def preprocess_scene(img: ee.Image, region: ee.Geometry, suffix: str) -> ee.Image:
    """
    Speckle-filter, clip, and rename bands with a pre/post suffix,
    e.g. VV -> VV_pre, VH -> VH_pre.
    """
    filtered = _speckle_filter(img).clip(region)
    renamed = filtered.select(
        config.S1_POLARIZATIONS,
        [f"{b}_{suffix}" for b in config.S1_POLARIZATIONS],
    )
    return renamed


def get_temporal_sar_stack(
    region: ee.Geometry, flood_date: str, basin: str = ""
) -> dict:
    """
    Returns {
      "image": 4-band image (VV_pre, VH_pre, VV_post, VH_post),
      "pre_info": selection info for pre scene,
      "post_info": selection info for post scene,
      "sar_meta": combined metadata for the log
    }.
    """
    pre_img, post_img, pre_info, post_info = match_pre_post_scenes(region, flood_date, basin=basin)

    pre_processed = preprocess_scene(pre_img, region, "pre")
    post_processed = preprocess_scene(post_img, region, "post")
    stack = pre_processed.addBands(post_processed)

    sar_meta = {
        "pre_s1_date": pre_info["date"],
        "post_s1_date": post_info["date"],
        # Same key-name fix as in match_pre_post_scenes: the property is
        # "orbitProperties_pass", not "orbit_pass".
        "orbit_pass": post_info["orbitProperties_pass"],
        "pre_relative_orbit": pre_info.get("relativeOrbitNumber_start"),
        "post_relative_orbit": post_info.get("relativeOrbitNumber_start"),
        "pre_absolute_orbit": pre_info.get("absoluteOrbitNumber"),
        "post_absolute_orbit": post_info.get("absoluteOrbitNumber"),
        "mission": post_info.get("missionID"),
        # Reflect the per-event decision (may be relaxed per-basin), not the
        # global config: a relaxed basin reports False, a strict one True.
        "orbit_pass_matched": bool(pre_info.get("pass_match_required", False)),
        "pre_selection_reason": pre_info["reason"],
        "post_selection_reason": post_info["reason"],
    }
    return {"image": stack, "pre_info": pre_info, "post_info": post_info,
            "sar_meta": sar_meta}