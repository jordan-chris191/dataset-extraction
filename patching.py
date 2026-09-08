"""
patching.py
-----------
Tiles an event's working region into a grid of PATCH_SIZE_PX x PATCH_SIZE_PX
(at TARGET_RESOLUTION_M) patches, computes per-patch statistics (valid-pixel
percentage = percentage of pixels present across ALL output bands, and flood
coverage) in a single server-side reduceRegions call, applies quality
filtering and flood-coverage stratification, and exports the surviving
patches as single multi-band GeoTIFFs.

All bands (SAR + MERIT Hydro + PhilSA mask) are stacked into ONE image before
export so that every exported GeoTIFF is guaranteed pixel-aligned across
modalities — there is no separate resampling step per band that could drift.
"""

from __future__ import annotations

import math

import ee

import gee_config as config


def utm_epsg_for_lonlat(lon: float, lat: float) -> str:
    """
    EPSG code for the UTM zone containing (lon, lat). The Philippines spans
    UTM 50N/51N; this generalizes so the pipeline works basin-to-basin
    without hardcoding a zone.
    """
    zone = int(math.floor((lon + 180) / 6) + 1)
    hemisphere = 326 if lat >= 0 else 327  # 326xx = north, 327xx = south
    return f"EPSG:{hemisphere}{zone:02d}"


def generate_patch_grid(
    region: ee.Geometry,
    patch_size_m: float = config.PATCH_SIZE_M,
) -> dict:
    """
    Builds a non-overlapping grid of patch_size_m x patch_size_m squares
    covering `region`'s bounding box, in a local UTM projection so patch
    size is accurate in meters.

    Returns {"epsg": str, "patches": [{"row","col","rect"}]}.
    Patches are generated over the bounding box; patches that don't
    actually intersect `region` are dropped (so we don't waste export
    quota on empty box-corner patches).
    """
    lonlat = region.centroid(maxError=1).coordinates().getInfo()
    lon, lat = lonlat[0], lonlat[1]
    epsg = utm_epsg_for_lonlat(lon, lat)
    proj = ee.Projection(epsg)

    region_proj = region.transform(proj, maxError=1)
    bounds = region_proj.bounds(maxError=1).coordinates().get(0).getInfo()
    xs = [pt[0] for pt in bounds]
    ys = [pt[1] for pt in bounds]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    n_cols = math.ceil((xmax - xmin) / patch_size_m)
    n_rows = math.ceil((ymax - ymin) / patch_size_m)

    patches = []
    for row in range(n_rows):
        for col in range(n_cols):
            x0 = xmin + col * patch_size_m
            y0 = ymin + row * patch_size_m
            rect = ee.Geometry.Rectangle(
                [x0, y0, x0 + patch_size_m, y0 + patch_size_m],
                proj=proj,
                geodesic=False,
            )
            patches.append({"row": row, "col": col, "rect": rect})

    # Keep only patches that actually intersect the (possibly irregular)
    # region, not just its bounding box.
    intersecting = []
    for p in patches:
        does_intersect = p["rect"].intersects(region_proj, maxError=1).getInfo()
        if does_intersect:
            intersecting.append(p)

    return {"epsg": epsg, "patches": intersecting}


def compute_patch_stats(
    stack: ee.Image, mask: ee.Image, patches: list, epsg: str
) -> list:
    """
    For every patch, computes:
      - valid_pixel_percentage: fraction of pixels valid (present) across
        ALL OUTPUT_BANDS (the strictest measure).
      - flood_pixel_percentage: mean of the binary flood mask.
      - patch bounds (min/max lon/lat) via feature geometry.

    Done as ONE reduceRegions call (not one getInfo() per patch) to avoid
    thousands of round trips.

    Geospatial correctness: the union of band masks is what counts as
    "valid" for a patch; a patch is rejected if ANY band is missing there.
    """
    proj = ee.Projection(epsg)

    # total pixels per feature: reducer.count on a constant image
    total_px_img = ee.Image(1).rename("total_px")

    # valid (present) pixels across every band: AND of each band's mask
    all_bands_valid = stack.select(config.OUTPUT_BANDS).mask().reduce(ee.Reducer.min())
    all_bands_valid = all_bands_valid.rename("all_bands_valid")

    stats_src = (
        all_bands_valid
        .addBands(total_px_img)
        .addBands(stack.select(config.OUTPUT_BANDS).unmask(0.0))  # for count of valid
        .addBands(mask.rename(config.LABEL_BAND))
    )

    features = [
        ee.Feature(p["rect"], {"row": p["row"], "col": p["col"], "patch_index": i})
        for i, p in enumerate(patches)
    ]
    fc = ee.FeatureCollection(features)

    reducer = ee.Reducer.mean()
    reduced = stats_src.reduceRegions(
        collection=fc,
        reducer=reducer,
        scale=config.TARGET_RESOLUTION_M,
        crs=proj,
    )

    results = reduced.getInfo()["features"]

    stats = []
    for feat in results:
        props = feat["properties"]
        valid_frac = props.get("all_bands_valid", 0.0) or 0.0
        flood_mean = props.get(config.LABEL_BAND, 0.0) or 0.0
        # min/max lon/lat via the feature geometry (projected rect -> WGS84)
        try:
            rect = feat["geometry"]["coordinates"]
            lon_min = min(p[0] for p in rect[0])
            lon_max = max(p[0] for p in rect[0])
            lat_min = min(p[1] for p in rect[0])
            lat_max = max(p[1] for p in rect[0])
        except Exception:  # noqa: BLE001
            lon_min = lon_max = lat_min = lat_max = None
        stats.append({
            "patch_index": props["patch_index"],
            "row": props["row"],
            "col": props["col"],
            "valid_pixel_percentage": round(valid_frac * 100.0, 2),
            "flood_pixel_percentage": round(flood_mean * 100.0, 2),
            "min_lon": lon_min,
            "max_lon": lon_max,
            "min_lat": lat_min,
            "max_lat": lat_max,
        })
    return stats


def _bucket_for_fraction(fraction: float) -> tuple:
    for lo, hi in config.FLOOD_COVERAGE_BUCKETS:
        if lo <= fraction < hi or (hi == 1.0 and fraction == 1.0):
            return (lo, hi)
    return config.FLOOD_COVERAGE_BUCKETS[-1]


def filter_and_stratify(patches: list, stats: list) -> list:
    """
    Applies the no-data / valid-pixel quality filters, then caps how many
    patches are kept per flood-coverage bucket
    (config.MAX_PATCHES_PER_BUCKET_PER_EVENT) so an event dominated by dry
    background doesn't drown out the flooded/partial-flood patches — and
    vice versa. Within a bucket, patches closest to the bucket's median
    flood fraction are prioritized, so selection is deterministic (input row
    order is stable) rather than arbitrary.

    Dry/non-flood patches are deliberately kept (the bucket 0-0.01), because
    the binary segmentation model needs hard negatives (rice paddies, roads,
    buildings, vegetation, bare soil, permanent water, urban areas), per the
    thesis notes.
    """
    stats_by_index = {s["patch_index"]: s for s in stats}
    kept_by_bucket: dict = {b: [] for b in config.FLOOD_COVERAGE_BUCKETS}

    for i, p in enumerate(patches):
        s = stats_by_index.get(i)
        if s is None:
            continue
        # valid-pixel gate (strictest across bands) + configurable min
        if s["valid_pixel_percentage"] < config.MIN_VALID_PIXEL_PERCENT:
            continue
        if s["valid_pixel_percentage"] < (1.0 - config.MAX_NODATA_FRACTION) * 100.0:
            continue
        if s["flood_pixel_percentage"] < config.MIN_FLOOD_PIXEL_PERCENT:
            continue
        bucket = _bucket_for_fraction(s["flood_pixel_percentage"] / 100.0)
        entry = {**p, **s}
        kept_by_bucket[bucket].append(entry)

    selected = []
    for bucket, items in kept_by_bucket.items():
        lo, hi = bucket
        # bucket is in fraction (0-1); convert median to percent for sort key
        median_frac = (lo + hi) / 2.0
        items.sort(key=lambda x: abs(x["flood_pixel_percentage"] - median_frac * 100.0))
        selected.extend(items[: config.MAX_PATCHES_PER_BUCKET_PER_EVENT])

    return selected


def export_patch(
    stack: ee.Image, mask: ee.Image, patch: dict, epsg: str, event_id: str
) -> ee.batch.Task:
    """
    Exports one patch as a single multi-band GeoTIFF: OUTPUT_BANDS followed
    by LABEL_BAND. Keeping SAR + hydro + label in one file guarantees pixel
    alignment; split them back out at training-set build time if a separate
    label file is preferred.

    The GeoTIFF is written in the local UTM CRS at TARGET_RESOLUTION_M, so
    its geotransform is exactly the 10 m lattice used by the SAR stack.
    """
    combined = stack.select(config.OUTPUT_BANDS).addBands(mask.rename(config.LABEL_BAND))
    patch_id = f"{event_id}_r{patch['row']:03d}_c{patch['col']:03d}"

    export_kwargs = dict(
        image=combined,
        description=patch_id,
        region=patch["rect"],
        crs=epsg,
        scale=config.TARGET_RESOLUTION_M,
        maxPixels=1e9,
        fileFormat="GeoTIFF",
    )

    if config.EXPORT_DESTINATION == "drive":
        task = ee.batch.Export.image.toDrive(
            folder=config.DRIVE_FOLDER,
            fileNamePrefix=patch_id,
            **export_kwargs,
        )
    elif config.EXPORT_DESTINATION == "gcs":
        task = ee.batch.Export.image.toCloudStorage(
            bucket=config.GCS_BUCKET,
            fileNamePrefix=f"{config.GCS_PREFIX}/{patch_id}",
            **export_kwargs,
        )
    else:
        raise ValueError(f"Unknown EXPORT_DESTINATION: {config.EXPORT_DESTINATION}")
    task.start()
    return task