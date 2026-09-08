#!/usr/bin/env python3
"""
Inspect the PhilSA Cagayan (Trami/Kristine, 2024-10-27) flood shapefile:
  * lists all attribute fields and their values (so we can see if it
    carries municipality/city names -- PhilSA shapefiles commonly do)
  * reports the overall bounding box and centroid
  * computes distance from the flood extent to Santa Ana, Cagayan
    (Typhoon Marce/Yinxing's Nov 7 landfall point) and to Tuguegarao
    City (the area most associated with Trami's worst flooding), so
    we can sanity-check whether the mapped flood extent plausibly
    reflects Trami alone or likely reaches the area later hit by Marce.

This does NOT touch Earth Engine or your dataset -- it's a local,
read-only inspection of the shapefile only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHAPEFILE_PATH = PROJECT_ROOT / "data" / "philsa_shapefiles" / "cagayan_2024-10-27.shp"

# Reference points (approximate, WGS84 lat/lon)
SANTA_ANA_CAGAYAN = (18.4667, 122.1333)   # Marce/Yinxing Nov 7 landfall
TUGUEGARAO_CITY = (17.6132, 121.7270)     # Cagayan capital, worst-hit by Trami
APARRI = (18.3585, 121.6390)              # river mouth, also relevant


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    from math import radians, sin, cos, sqrt, atan2

    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * atan2(sqrt(a), sqrt(1 - a))


def main() -> int:
    if not SHAPEFILE_PATH.exists():
        print(f"[ERROR] Shapefile not found: {SHAPEFILE_PATH}")
        return 1

    print("=" * 70)
    print("PhilSA Cagayan Flood Shapefile Inspection")
    print("=" * 70)
    print(f"File: {SHAPEFILE_PATH}")

    gdf = gpd.read_file(SHAPEFILE_PATH)
    print(f"\n[OK] Loaded {len(gdf)} feature(s)")
    print(f"CRS: {gdf.crs}")

    print("\n--- Attribute fields ---")
    for col in gdf.columns:
        if col == "geometry":
            continue
        print(f"  {col}")

    print("\n--- Sample attribute values (first 10 rows) ---")
    non_geom_cols = [c for c in gdf.columns if c != "geometry"]
    if non_geom_cols:
        print(gdf[non_geom_cols].head(10).to_string())
    else:
        print("  (no non-geometry attribute fields found)")

    # Look for anything that smells like a place-name field and list uniques
    name_like_cols = [
        c for c in non_geom_cols
        if any(k in c.lower() for k in ["mun", "city", "brgy", "barangay", "name", "adm"])
    ]
    if name_like_cols:
        print("\n--- Distinct place-name-like values found ---")
        for col in name_like_cols:
            uniques = sorted(set(gdf[col].dropna().astype(str)))
            print(f"\n  Field: {col}  ({len(uniques)} distinct values)")
            for v in uniques[:50]:
                print(f"    - {v}")
            if len(uniques) > 50:
                print(f"    ... and {len(uniques) - 50} more")
    else:
        print("\n[NOTE] No obvious municipality/city/barangay name field found.")
        print("       Overlap will be assessed geometrically instead.")

    # Reproject to WGS84 for lat/lon-based distance checks
    gdf_wgs84 = gdf.to_crs("EPSG:4326")
    union_geom = gdf_wgs84.geometry.union_all()

    minx, miny, maxx, maxy = union_geom.bounds
    centroid = union_geom.centroid

    print("\n--- Geometry extent (EPSG:4326) ---")
    print(f"  Bounding box: lon [{minx:.4f}, {maxx:.4f}], lat [{miny:.4f}, {maxy:.4f}]")
    print(f"  Centroid:     lat {centroid.y:.4f}, lon {centroid.x:.4f}")
    print(f"  Total area:   {gdf_wgs84.to_crs(gdf_wgs84.estimate_utm_crs()).geometry.area.sum() / 1e6:.1f} km^2")

    print("\n--- Distance from flood extent to reference points ---")
    for label, (lat, lon) in [
        ("Santa Ana, Cagayan (Marce/Yinxing Nov 7 landfall)", SANTA_ANA_CAGAYAN),
        ("Tuguegarao City (Trami's worst-hit area)", TUGUEGARAO_CITY),
        ("Aparri (Cagayan River mouth)", APARRI),
    ]:
        pt = Point(lon, lat)
        dist_to_boundary_km = union_geom.distance(pt) * 111.0  # rough deg->km at this latitude
        centroid_dist_km = haversine_km(centroid.y, centroid.x, lat, lon)
        inside = union_geom.contains(pt)
        print(f"\n  {label}")
        print(f"    Inside flood polygon:            {inside}")
        print(f"    Distance to nearest flood edge:  ~{dist_to_boundary_km:.1f} km")
        print(f"    Distance to flood centroid:      ~{centroid_dist_km:.1f} km")

    print()
    print("=" * 70)
    print("INTERPRETATION GUIDE")
    print("=" * 70)
    print(
        "If the flood extent is at or near Santa Ana / the northern coast,\n"
        "the mapped area overlaps where Marce made landfall on Nov 7, and any\n"
        "patches there are at real risk of capturing Marce's flood signal\n"
        "instead of / in addition to Trami's.\n"
        "\n"
        "If the flood extent is concentrated well south of Santa Ana (e.g.\n"
        "around Tuguegarao or further south/inland), it's more plausible the\n"
        "mapped area reflects Trami's flooding specifically -- but note Marce's\n"
        "heavy rainfall affected the *whole* Cagayan Valley (upstream too), so\n"
        "river-driven flooding from Marce is not limited to the landfall point.\n"
        "This distance check is a first-pass sanity check, not a guarantee."
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())