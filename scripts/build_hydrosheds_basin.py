#!/usr/bin/env python3
"""
build_hydrosheds_basin.py
-------------------------
Reusable builder of river-basin boundary polygons from HydroBASINS
(HydroSHEDS) lev12 data, mirroring how `data/basins/cagayan.geojson` was
produced manually — but automated and parameterized.

The reverse (upstream) walk via NEXT_DOWN yields exactly the set of lev12
sub-basins that drain to the chosen outlet, i.e. the full river basin. The
result is dissolved to a single geometry and written to
data/basins/<name>.geojson in the same schema as cagayan.geojson (CRS84,
one feature, the OUTLET polygon's HydroBASINS property bag preserved for
provenance).

Usage (run from the repo root; the au lev12 zip must already be downloaded
and extracted under data/hydrosheds/):
  # Resolve the outlet by anchor lat/lon (nearest lev12 polygon):
  python scripts/build_hydrosheds_basin.py --outlet-lat 15.0 --outlet-lon 120.5 --name pampanga
  python scripts/build_hydrosheds_basin.py --hybas-id 5120029740 --name agno

  # Or by an exact HydroBASINS outlet id (regression / well-known basins):
  python scripts/build_hydrosheds_basin.py --hybas-id 5120030230 --name cagayan

  # Optionally verify the basin contains a staging flood shapefile's extent
  # (guards against the silent work_region clip when the basin is too tight):
  python scripts/build_hydrosheds_basin.py --outlet-lat 15.0 --outlet-lon 120.5 \
      --name pampanga --verify-shp data/philsa_shapefiles/pampanga_2024-07-23.shp
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow running as `python scripts/build_hydrosheds_basin.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd


SHAPEFILE = os.path.join(
    "data", "hydrosheds", "hybas_au_lev12_v1c", "hybas_au_lev12_v1c.shp"
)
OUT_DIR = os.path.join("data", "basins")


def load_lev12() -> gpd.GeoDataFrame:
    if not os.path.exists(SHAPEFILE):
        raise SystemExit(
            f"HydroBASINS shapefile not found: {SHAPEFILE}\n"
            "Download and extract it first, e.g.:\n"
            "  curl -L -o data/hydrosheds/hybas_au_lev12_v1c.zip "
            "https://data.hydrosheds.org/file/HydroBASINS/standard/hybas_au_lev12_v1c.zip\n"
            "  python -c \"import zipfile; zipfile.ZipFile('data/hydrosheds/hybas_au_lev12_v1c.zip').extractall('data/hydrosheds/hybas_au_lev12_v1c')\""
        )
    gdf = gpd.read_file(SHAPEFILE)
    if gdf.crs is None:
        raise SystemExit("HydroBASINS shapefile has no CRS.")
    return gdf.to_crs("EPSG:4326")


def resolve_outlet(gdf: gpd.GeoDataFrame, hybas_id=None, lat=None, lon=None):
    """Return the lev12 feature that is the basin outlet."""
    if hybas_id is not None:
        sub = gdf[gdf["HYBAS_ID"] == int(hybas_id)]
        if sub.empty:
            raise SystemExit(f"No lev12 polygon with HYBAS_ID={hybas_id}")
        return sub.iloc[0]
    if lat is None or lon is None:
        raise SystemExit("Provide --hybas-id OR both --outlet-lat and --outlet-lon")
    # Nearest polygon centroid to the anchor.
    from shapely.geometry import Point
    anchor = Point(lon, lat)
    dists = gdf.geometry.distance(anchor)
    return gdf.loc[dists.idxmin()]


def upstream_closure(gdf: gpd.GeoDataFrame, outlet_hybas_id: int, max_iter=2000):
    """Set of HYBAS_IDs draining to the outlet (reverse NEXT_DOWN walk)."""
    next_down = dict(zip(gdf["HYBAS_ID"], gdf["NEXT_DOWN"]))
    ids = list(gdf["HYBAS_ID"])
    # Reverse map: downstream_id -> list of upstream ids whose NEXT_DOWN == it.
    reverse: dict[int, list[int]] = {}
    for i, nd in next_down.items():
        if nd:
            reverse.setdefault(int(nd), []).append(int(i))
    basin = {int(outlet_hybas_id)}
    frontier = {int(outlet_hybas_id)}
    for _ in range(max_iter):
        new = set()
        for f in frontier:
            new.update(reverse.get(f, ()))
        added = new - basin
        if not added:
            break
        basin |= added
        frontier = added
    return basin, len(ids)


def upstream_area(gdf, basin_ids) -> float:
    """Sum of SUB_AREA over the basin (km^2), to sanity-check the closure."""
    return float(gdf[gdf["HYBAS_ID"].isin(basin_ids)]["SUB_AREA"].sum())


def build(name: str, hybas_id=None, lat=None, lon=None, verify_shp=None) -> None:
    gdf = load_lev12()
    outlet = resolve_outlet(gdf, hybas_id=hybas_id, lat=lat, lon=lon)
    out_id = int(outlet["HYBAS_ID"])
    basin_ids, total = upstream_closure(gdf, out_id)

    sub = gdf[gdf["HYBAS_ID"].isin(basin_ids)].copy()
    sub = sub[~sub.geometry.is_empty]

    area_km2 = upstream_area(sub, basin_ids)
    print(
        f"basin {name}: outlet HYBAS_ID={out_id} -> {len(basin_ids)} lev12 "
        f"sub-basins ({len(basin_ids)/total*100:.2f}% of region), SUM_SUB_AREA={area_km2:.1f} km^2"
    )
    if area_km2 < 1:
        raise SystemExit(f"Suspiciously small basin area for {name}; aborting.")

    dissolved = sub.dissolve()
    geom = dissolved.geometry.iloc[0]
    if geom.is_empty:
        raise SystemExit(f"Dissolved {name} to an empty geometry.")

    # Enclosure sanity-check: the work_region clips flood extent to the basin,
    # so the basin must capture the flood area that lies within/near it. PhilSA
    # packages are often NATIONWIDE (they span all of Luzon/Mindanao), so a
    # strict 'contains the whole extent' check is the wrong guard. Instead
    # report the fraction of total flood area that falls inside the basin — we
    # want it high, so the mask isn't truncated for THIS basin's flood.
    if verify_shp:
        if not os.path.exists(verify_shp):
            print(f"  !! verify-shp not found (skipping): {verify_shp}")
        else:
            ext = gpd.read_file(verify_shp).to_crs("EPSG:4326")
            ext = ext[~ext.geometry.is_empty]
            # Repair any invalid ring winding in the flood polys (PhilSA SHP
            # can trip GEOS with 'side location conflict' on intersection).
            ext = ext.buffer(0)
            inside = ext.geometry.intersection(geom).area.sum()
            total = ext.geometry.area.sum()
            frac = inside / total if total else float("nan")
            print(
                f"  flood coverage: {frac*100:.2f}% of flood area inside {name} basin "
                f"(({inside:,.3f} inside / {total:,.3f} total, in deg^2)). "
                f"{'OK' if frac > 0.8 else 'WARNING: check the outlet anchor — basin may be mis-resolved.'}"
            )

    # Output: one feature with the OUTLET polygon's property bag (matches
    # cagayan.geojson's schema), dissolved geometry.
    props = {k: v for k, v in outlet.items() if k != "geometry"}
    out = gpd.GeoDataFrame(
        [props | {"geometry": geom}],
        crs="EPSG:4326",
        columns=[c for c in outlet.index if c != "geometry"] + ["geometry"],
    )
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{name}.geojson")
    out.to_file(path, driver="GeoJSON")
    print(f"wrote {path}  (geom type {geom.geom_type})")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build a river-basin boundary geojson from HydroBASINS au lev12.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--name", required=True, help="Output basin label -> data/basins/<name>.geojson")
    ap.add_argument("--hybas-id", type=int, default=None, help="Exact outlet HYBAS_ID")
    ap.add_argument("--outlet-lat", type=float, default=None)
    ap.add_argument("--outlet-lon", type=float, default=None)
    ap.add_argument("--verify-shp", default=None, help="Flood extent shp to check is inside the basin.")
    args = ap.parse_args()

    try:
        build(args.name, hybas_id=args.hybas_id, lat=args.outlet_lat,
              lon=args.outlet_lon, verify_shp=args.verify_shp)
    except SystemExit as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
