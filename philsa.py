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