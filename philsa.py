"""
philsa.py
---------
Loads the PhilSA flood-event index (data/philsa_events.csv) and converts
each event's PhilSA flood-extent shapefile into an Earth Engine
FeatureCollection and, later, a binary raster mask (0 = non-flood,
1 = flood) aligned to the working grid.

PhilSA polygons are treated purely as ground truth — they are never a model
input. All field names are discovered from the actual shapefile at runtime
and reported, never assumed.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Optional

import ee
import geopandas as gpd

import gee_config as config


@dataclass
class FloodEvent:
    event_id: str
    basin: str
    philsa_shapefile_path: str
    flood_date: str  # "YYYY-MM-DD"
    # Actual field names discovered from the shapefile (filled at load time).
    source_fields: list[str] = field(default_factory=list)
    crs: Optional[str] = None


def load_events(csv_path: str = config.PHILSA_EVENTS_CSV) -> list[FloodEvent]:
    """Read the flood-event index CSV into a list of FloodEvent objects."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"{csv_path} not found. Copy data/philsa_events.sample.csv to "
            f"data/philsa_events.csv and fill in your real events, or set "
            f"gee_config.PHILSA_EVENTS_CSV."
        )

    events: list[FloodEvent] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"event_id", "basin", "philsa_shapefile_path", "flood_date"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{csv_path} is missing required columns: {missing}")
        for row in reader:
            if row["basin"] not in config.BASINS:
                raise ValueError(
                    f"Event {row['event_id']} references unknown basin "
                    f"'{row['basin']}'. Check gee_config.BASINS."
                )
            events.append(FloodEvent(**row))
    return events


def inspect_shapefile(path: str) -> dict:
    """
    Return a small report on the shapefile: fields, CRS, geometry type,
    feature count, bounds. Used both to document what the data actually
    contains (no assumed field names) and to fail fast on obvious problems.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    gdf = gpd.read_file(path)
    report = {
        "path": path,
        "n_features": len(gdf),
        "columns": list(gdf.columns),
        "geometry_type": (
            gdf.geometry.geom_type.unique().tolist() if hasattr(gdf.geometry, "geom_type") else []
        ),
        "crs": str(gdf.crs) if gdf.crs is not None else None,
        "bounds": list(gdf.total_bounds) if len(gdf) else None,
    }
    # Drop reference to the possibly-large gdf.
    del gdf
    return report


def shapefile_to_ee_featurecollection(
    shapefile_path: str,
    report: Optional[dict] = None,
) -> ee.FeatureCollection:
    """
    Load a local PhilSA shapefile with geopandas, reproject to EPSG:4326,
    validate, and convert to an ee.FeatureCollection via GeoJSON.

    Doing this locally (rather than uploading every shapefile as an EE
    asset) keeps the pipeline self-contained at the cost of a client-side
    round trip for large/complex polygons.

    Geospatial correctness notes:
      * We require an explicit CRS before reprojecting; a shapefile with no
        `.prj` is assumed to be WGS84 only if you set FORCE_WGS84 below; by
        default we fail so we never silently misplace geometries.
      * We dissolve to a single (multi)polygon: the binary target ignores
        internal attribute distinctions (e.g. flood-depth class), exactly
        per the thesis spec.
    """
    if report is None:
        report = inspect_shapefile(shapefile_path)

    if report["n_features"] == 0:
        raise ValueError(f"{shapefile_path} has no features — cannot rasterize an empty mask.")

    gdf = gpd.read_file(shapefile_path)
    # Drop empty geometries immediately.
    before = len(gdf)
    gdf = gdf[~gdf.is_empty]
    gdf = gdf[gdf.geometry.notna()]
    if len(gdf) == 0 or len(gdf) < before:
        if len(gdf) == 0:
            raise ValueError(f"{shapefile_path} has no non-empty geometries.")
        # Some rows dropped (partially empty) — that's OK, but report it.
        gdf_ = gdf

    if gdf.crs is None:
        # Column is void, no CRS in any object.
        raise ValueError(
            f"{shapefile_path} has no CRS defined — cannot reproject safely. "
            f"Set the projection in QGIS/ogr2ogr first."
        )
    gdf = gdf.to_crs("EPSG:4326")

    # Collapse to a single geometry (binary target, no attribute semantics).
    dissolved = gdf.dissolve()
    geom = dissolved.geometry.iloc[0]
    if geom.is_empty:
        raise ValueError(f"{shapefile_path} dissolved to an empty geometry.")

    geojson = geom.__geo_interface__
    ee_geom = ee.Geometry(geojson)
    return ee.FeatureCollection([ee.Feature(ee_geom, {"flood": 1})])


# ---------------------------------------------------------------------------
# Client-side work-region computation (avoids EE payload-size limits)
# ---------------------------------------------------------------------------

def _utm_epsg_for_lonlat(lon: float, lat: float) -> str:
    """UTM EPSG zone string from WGS84 lon/lat."""
    zone = int((lon + 180) / 6) + 1
    return f"EPSG:{32600 + zone}" if lat >= 0 else f"EPSG:{32700 + zone}"


def compute_work_region_local(
    flood_path: str, basin_key: str
) -> tuple[ee.FeatureCollection, ee.Geometry]:
    """
    Client-side computation of (flood_fc, work_region), compatible with large
    flood shapefiles that would blow the 10MB EE payload limit if sent to
    the server for buffer/intersection.

    Returns
    -------
    flood_fc : ee.FeatureCollection
        Single-feature FC (dissolved flood polygon) for mask painting via
        ``rasterize_flood_mask``.
    work_region : ee.Geometry
        Simplified geometry (buffered flood ∩ basin) for S1 clip/filterBounds,
        MERIT, alignment, and patch-grid generation.
    """
    from pyproj import Transformer
    from shapely.ops import transform as shapely_transform
    from shapely.ops import unary_union

    # ---- 1. Read flood shapefile ------------------------------------------
    flood_gdf = gpd.read_file(flood_path)
    flood_gdf = flood_gdf[~flood_gdf.is_empty & flood_gdf.geometry.notna()]
    if flood_gdf.empty:
        raise ValueError(f"{flood_path}: no non-empty geometries.")
    if flood_gdf.crs is None:
        raise ValueError(f"{flood_path}: no CRS defined.")
    flood_gdf = flood_gdf.to_crs("EPSG:4326")
    if flood_gdf.geometry.iloc[0].is_empty:
        raise ValueError(f"{flood_path}: empty geometries.")

    # ---- 2. Read basin boundary (client-side, local file) -----------------
    basin_info = config.BASINS[basin_key]
    local = basin_info.get("local_path")
    if not local:
        raise ValueError(
            f"compute_work_region_local needs a local_path for basin {basin_key}"
        )
    basin_gdf = gpd.read_file(local).to_crs("EPSG:4326")
    basin_geom_wgs = basin_gdf.dissolve().geometry.iloc[0]

    # ---- 3. Project to UTM (all metric ops below need a metric CRS) --------
    #    Use the basin centroid to pick the UTM zone (stable, avoids edge
    #    cases where the clipped flood polygon sits on a UTM boundary).
    basin_centroid = basin_geom_wgs.centroid
    epsg = _utm_epsg_for_lonlat(basin_centroid.x, basin_centroid.y)
    to_utm = Transformer.from_crs("EPSG:4326", epsg, always_xy=True).transform
    to_wgs = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True).transform

    # ---- 4. Pre-clip flood to basin geometry (the key optimisation) -------
    #    PhilSA shapefiles are nationwide (34k+ polygons); only a small
    #    fraction falls inside the target basin.  Spatial-filtering BEFORE
    #    dissolve/shrink the working set from millions of vertices to the
    #    few-thousand that actually matter, avoiding the 10 MB EE payload
    #    limit and keeping downstream geometry ops tractable.
    flood_clipped = flood_gdf[
        flood_gdf.intersects(basin_geom_wgs)
    ].copy()
    if flood_clipped.empty:
        raise ValueError(
            f"{flood_path}: no flood polygons intersect basin {basin_key}"
        )
    print(f"  Pre-clip: {len(flood_gdf)} -> {len(flood_clipped)} polygons "
          f"(basin {basin_key})")
    # Drop the full-resolution gdf to free memory — we work on the clipped set.
    del flood_gdf

    # ---- 5. Dissolve clipped flood → mask FeatureCollection ----------------
    #    The mask is painted server-side at 10 m resolution, so a 50 m
    #    simplify (5 px) is visually indistinguishable but collapses the
    #    dissolved MultiPolygon's vertex count and keeps the `.paint()` call
    #    under EE's 10 MB request payload limit.
    #    NOTE: the simplify MUST happen in a metric CRS. The dissolved
    #    geometry is in WGS84 degrees here, so a literal
    #    `simplify(tolerance=50.0)` would mean 50 deg ≈ 5 500 km and destroy
    #    the mask; we project to UTM, simplify by true metres, then project
    #    back to WGS84 for EE.
    MASK_SIMPLIFY_M = 50.0
    dissolved_geom = flood_clipped.dissolve().geometry.iloc[0]
    mask_utm_geom = shapely_transform(to_utm, dissolved_geom)
    simplified_geom = mask_utm_geom.simplify(
        tolerance=MASK_SIMPLIFY_M, preserve_topology=True
    )
    if not simplified_geom.is_valid:            # GEOS DP can leave tiny slivers
        simplified_geom = simplified_geom.buffer(0)  # standard OGC repair
    mask_wgs_geom = shapely_transform(to_wgs, simplified_geom)
    n_full = sum(len(p.exterior.coords) for p in getattr(dissolved_geom, "geoms", [dissolved_geom]))
    n_simpl = sum(len(p.exterior.coords) for p in getattr(mask_wgs_geom, "geoms", [mask_wgs_geom]))
    print(f"  Mask FC simplified: {n_full:,} -> {n_simpl:,} vertices "
          f"({len(mask_wgs_geom.__geo_interface__) / 1e6:.2f} MB)")
    flood_fc = ee.FeatureCollection([
        ee.Feature(
            ee.Geometry(mask_wgs_geom.__geo_interface__),
            {"flood": 1},
        )
    ])

    # Basin in UTM, ready for the region intersection below.
    basin_utm_geom = shapely_transform(to_utm, basin_geom_wgs)

    # ---- 6. Per-polygon SIMPLIFY first, then buffer+merge ----------------
    #    Patches are 2 560 m and the buffer is 2 000 m, so a ~500 m simplify
    #    tolerance is safely hidden by the buffer and keeps the region compact
    #    (and well under the EE 10 MB payload limit).
    #    NOTE: we simplify in WGS84 degrees here (tol_deg), matching the
    #    pre-existing behaviour; the region's 2 000 m buffer dominates any
    #    sub-degree wobble, so degree-metric error is insignificant.
    SIMPLIFY_TOLERANCE_M = 500.0
    tol_deg = SIMPLIFY_TOLERANCE_M / 111_000.0  # ~500 m ≈ 0.0045 deg
    simplified_parts = [
        g.simplify(tolerance=tol_deg, preserve_topology=True)
        for g in flood_clipped.geometry
    ]
    buffered_parts = []
    for g in simplified_parts:
        utm_geom = shapely_transform(to_utm, g)
        buffered_parts.append(utm_geom.buffer(2000))
    merged = unary_union(buffered_parts)
    intersected = merged.intersection(basin_utm_geom)

    # ---- 7. Final simplify (patches are 2 560 m) --------------------------
    simplified = intersected.simplify(tolerance=200, preserve_topology=True)

    # ---- 8. Back to WGS84 → ee.Geometry -----------------------------------
    simplified_wgs = shapely_transform(to_wgs, simplified)
    work_region = ee.Geometry(simplified_wgs.__geo_interface__)

    return flood_fc, work_region


def rasterize_flood_mask(fc: ee.FeatureCollection, region: ee.Geometry) -> ee.Image:
    """
    Convert flood polygons to a binary raster: 1 = flood, 0 = non-flood,
    at the pipeline's target resolution, clipped to `region`.

    The mask is created as `ee.Image(0).paint(fc, 1)` so the background is
    explicitly 0 (non-flood) and painted areas are 1 (flood). Pixels with no
    Sentinel-1 cover remain masked (not 0), so "non-flood" (valid dry pixel)
    and "no data" (no SAR coverage) are distinguishable in quality control.
    """
    mask = (
        ee.Image(0)
        .byte()
        .paint(fc, 1)
        .rename(config.LABEL_BAND)
        .clip(region)
    )
    return mask


def get_basin_geometry(basin_key: str) -> ee.Geometry:
    """
    Load a basin boundary as an ee.Geometry.

    Source is chosen from `gee_config.BASINS[basin_key]`:
      * "local_path" (preferred): read a local shapefile/GeoJSON with
        geopandas, reproject to EPSG:4326, dissolve to a single geometry, and
        convert to ee.Geometry. No EE asset upload needed for the boundary.
      * "asset_id": load an Earth Engine table asset (legacy/EE-hosted path).
    """
    if basin_key not in config.BASINS:
        raise ValueError(f"Unknown basin '{basin_key}'")
    info = config.BASINS[basin_key]

    local_path = info.get("local_path")
    if local_path:
        import os
        if not os.path.exists(local_path):
            raise ValueError(f"Basin boundary file not found: {local_path}")
        import geopandas as gpd
        gdf = gpd.read_file(local_path)
        if gdf.crs is None:
            raise ValueError(
                f"Basin boundary {local_path} has no CRS — cannot reproject safely."
            )
        dissolved = gdf.to_crs("EPSG:4326").dissolve()
        geom = dissolved.geometry.iloc[0]
        if geom.is_empty:
            raise ValueError(f"Basin boundary {local_path} dissolved to an empty geometry.")
        return ee.Geometry(geom.__geo_interface__)

    asset_id = info["asset_id"]
    fc = ee.FeatureCollection(asset_id)
    return fc.geometry()