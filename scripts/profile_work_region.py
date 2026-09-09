"""
Stage-by-stage profiler for philsa.compute_work_region_local on the
Pampanga flood shapefile (the confirmed PP bottleneck that timed out).

Replicates each stage of compute_work_region_local with timing so we can
see exactly where the seconds go before choosing an optimization.

    python scripts/profile_work_region.py
"""
from __future__ import annotations

import sys
import time

import geopandas as gpd
from pyproj import Transformer
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

FLOOD = "data/philsa_shapefiles/pampanga_2024-07-23.shp"
BASIN = "data/basins/pampanga.geojson"

SIMPLIFY_TOLERANCE_M = 500.0
BUFFER_M = 2000


def _utm_epsg_for_lonlat(lon, lat):
    zone = int((lon + 180) / 6) + 1
    return f"EPSG:{32600 + zone}" if lat >= 0 else f"EPSG:{32700 + zone}"


def stage(name):
    """Decorator-free timing helper: returns a timed context manager."""
    import contextlib

    @contextlib.contextmanager
    def _tm():
        t0 = time.perf_counter()
        yield
        print(f"  {name:<42} {time.perf_counter() - t0:8.2f}s")
    return _tm()


def main():
    # ---- 1. read_file ----------------------------------------------------
    with stage("1. gpd.read_file(flood)"):
        flood = gpd.read_file(FLOOD)
    print(f"      features={len(flood)} bounds={flood.total_bounds.round(3).tolist()}")
    with stage("1b. cast to 4326"):
        flood_wgs = flood.to_crs("EPSG:4326")

    # ---- 2. basin --------------------------------------------------------
    with stage("2. gpd.read_file(basin)"):
        basin = gpd.read_file(BASIN).to_crs("EPSG:4326")
    with stage("2b. basin.dissolve()"):
        basin_geom_wgs = basin.dissolve().geometry.iloc[0]
    print(f"      basin is multipolygon={basin_geom_wgs.geom_type}, parts={len(getattr(basin_geom_wgs,'geoms',[]))}")

    # ---- 3. UTM setup ----------------------------------------------------
    with stage("3. UTM zone pick + transformers"):
        rep = flood_wgs.geometry.iloc[0].representative_point()
        epsg = _utm_epsg_for_lonlat(rep.x, rep.y)
        to_utm = Transformer.from_crs("EPSG:4326", epsg, always_xy=True).transform
        to_wgs = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True).transform
    print(f"      epsg={epsg}")

    # ---- 4. per-polygon simplify (WGS84) ---------------------------------
    tol_deg = SIMPLIFY_TOLERANCE_M / 111_000.0
    with stage("4. per-polygon simplify (WGS84)"):
        simplified = [g.simplify(tolerance=tol_deg, preserve_topology=True)
                      for g in flood_wgs.geometry]
    print(f"      simplified n_part_features={len(simplified)}")

    # ---- 5. per-polygon transform->UTM + buffer --------------------------
    with stage("5. per-polygon transform+UTM buffer"):
        buffered = [shapely_transform(to_utm, g).buffer(BUFFER_M) for g in simplified]
    print(f"      buffered n={len(buffered)}")

    # ---- 6. basin to UTM -------------------------------------------------
    with stage("6. basin transform to UTM"):
        basin_utm = shapely_transform(to_utm, basin_geom_wgs)

    # ---- 7. unary_union of buffered --------------------------------------
    with stage("7. unary_union(buffered)"):
        merged = unary_union(buffered)

    # ---- 8. intersection with basin --------------------------------------
    with stage("8. merged INTERSECT basin_utm"):
        intersected = merged.intersection(basin_utm)

    # ---- 9. final simplify ------------------------------------------------
    with stage("9. final simplify(200)"):
        final = intersected.simplify(tolerance=200, preserve_topology=True)

    # ---- 10. back to WGS84 ------------------------------------------------
    with stage("10. transform back to WGS84"):
        final_wgs = shapely_transform(to_wgs, final)
    print(f"  FINAL work_region geom_type={final_wgs.geom_type}")
    npts = sum(len(g.exterior.coords) for g in (final_wgs.geoms if hasattr(final_wgs, 'geoms') else [final_wgs]))
    print(f"  FINAL total ring vertices≈{npts}")


if __name__ == "__main__":
    main()
