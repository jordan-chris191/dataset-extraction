"""
exports.py
----------
Thin orchestration over the EE batch exports +
local metadata bookkeeping. Each event writes:

    <OUTPUT_ROOT>/<event_id>/metadata.json        # full selection ledger
    <OUTPUT_ROOT>/<event_id>/patches/<patch_id>.tif

and appends one row per exported patch to the master metadata CSV.

The actual GeoTIFF export is `patching.export_patch`. This module only
decides WHERE things go and records what was requested (task ids etc.).
"""

from __future__ import annotations

import os

import ee

import gee_config as config
import metadata
import patching


def export_event_patches(
    full_stack: ee.Image,
    mask: ee.Image,
    selected: list,
    epsg: str,
    event: object,
    sar_meta: dict,
    event_extra: dict | None = None,
) -> list:
    """
    Export every selected patch of `event` and log its metadata.

    Returns the list of records written to the master CSV (one per patch).
    """
    out_root = config.OUTPUT_ROOT
    out_event_dir = os.path.join(out_root, event.event_id)
    os.makedirs(os.path.join(out_event_dir, "patches"), exist_ok=True)

    records = []
    for p in selected:
        task = patching.export_patch(full_stack, mask, p, epsg, event.event_id)
        patch_id = f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}"

        record = {
            "event_id": event.event_id,
            "basin": event.basin,
            "flood_date": event.flood_date,
            "pre_s1_date": sar_meta.get("pre_s1_date"),
            "post_s1_date": sar_meta.get("post_s1_date"),
            "pre_post_days": metadata.pre_post_days(
                event.flood_date,
                sar_meta.get("pre_s1_date", ""),
                sar_meta.get("post_s1_date", ""),
            ),
            "orbit_pass": sar_meta.get("orbit_pass"),
            "relative_orbit": sar_meta.get("post_relative_orbit"),
            "pre_relative_orbit": sar_meta.get("pre_relative_orbit"),
            "post_relative_orbit": sar_meta.get("post_relative_orbit"),
            "pre_absolute_orbit": sar_meta.get("pre_absolute_orbit"),
            "post_absolute_orbit": sar_meta.get("post_absolute_orbit"),
            "mission": sar_meta.get("mission"),
            "orbit_pass_matched": sar_meta.get("orbit_pass_matched"),
            "epsg": epsg,
            "patch_id": patch_id,
            "min_lat": p.get("min_lat"),
            "max_lat": p.get("max_lat"),
            "min_lon": p.get("min_lon"),
            "max_lon": p.get("max_lon"),
            "flood_pixel_percentage": p.get("flood_pixel_percentage"),
            "valid_pixel_percentage": p.get("valid_pixel_percentage"),
            "nodata_fraction": round(
                (100.0 - (p.get("valid_pixel_percentage") or 0.0)) / 100.0, 4
            ),
            "row": p.get("row"),
            "col": p.get("col"),
            "export_task_id": task.id,
            "pre_selection_reason": sar_meta.get("pre_selection_reason"),
            "post_selection_reason": sar_meta.get("post_selection_reason"),
        }
        metadata.append_patch_record(record)
        records.append(record)

    # Per-event metadata.json (the reproducible ledger)
    event_info = {
        "event_id": event.event_id,
        "basin": event.basin,
        "flood_date": event.flood_date,
        "shapefile": event.philsa_shapefile_path,
        "source_fields": getattr(event, "source_fields", []),
        "crs": getattr(event, "crs", None),
        "sentinel1": sar_meta,
        "parameters": {
            "collection": config.S1_COLLECTION,
            "instrument_mode": config.S1_INSTRUMENT_MODE,
            "polarizations": config.S1_POLARIZATIONS,
            "pre_window_days": config.PRE_FLOOD_SEARCH_WINDOW_DAYS,
            "pre_min_gap_days": config.PRE_FLOOD_MIN_GAP_DAYS,
            "post_window_days": config.POST_FLOOD_SEARCH_WINDOW_DAYS,
            "orbit_pass_matched": config.S1_REQUIRE_MATCHING_ORBIT_PASS,
            "speckle_filter": config.SPECKLE_FILTER,
            "merit_hydro_image": config.MERIT_HYDRO_IMAGE,
            "merit_band_map": config.MERIT_BAND_MAP,
            "merit_resample": config.MERIT_RESAMPLE,
            "target_resolution_m": config.TARGET_RESOLUTION_M,
            "patch_size_px": config.PATCH_SIZE_PX,
            "patch_size_m": config.PATCH_SIZE_M,
            "max_nodata_fraction": config.MAX_NODATA_FRACTION,
            "min_valid_pixel_percent": config.MIN_VALID_PIXEL_PERCENT,
            "min_flood_pixel_percent": config.MIN_FLOOD_PIXEL_PERCENT,
            "flood_coverage_buckets": [list(b) for b in config.FLOOD_COVERAGE_BUCKETS],
            "max_patches_per_bucket_per_event": config.MAX_PATCHES_PER_BUCKET_PER_EVENT,
        },
        "output_bands": config.OUTPUT_BANDS + [config.LABEL_BAND],
        "output_root": config.OUTPUT_ROOT,
        "export_destination": config.EXPORT_DESTINATION,
        "n_exported_patches": len(records),
    }
    if event_extra:
        event_info.update(event_extra)

    metadata.write_event_metadata(event.event_id, event_info)

    return records