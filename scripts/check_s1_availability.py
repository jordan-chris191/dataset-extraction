#!/usr/bin/env python3
"""
Extended Sentinel-1 availability scan for the Cagayan / Trami (Kristine)
flood event.

Unlike check_s1_availability.py, this script:
  * Widens the post-flood search window to 2024-10-27 -> 2024-11-10
    (covers a full S1A repeat cycle, since only S1A was operational
    in Oct 2024 -- S1B failed Dec 2021, S1C not yet operational).
  * Does NOT restrict to a single relative orbit/pass up front --
    it lists every IW/VV+VH scene over the basin in that window,
    sorted by date, so you can see all candidate post-flood scenes
    (not just ones matching the pre-flood orbit).
  * Cross-checks each candidate post scene against the known
    pre-flood scenes for orbit-pass / relative-orbit compatibility,
    and flags the best (earliest, most compatible) option.

This is a diagnostic script only. It does not modify or export data.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from gee_config import EE_PROJECT

EVENT_NAME = "Cagayan 2024-10-27 (Tropical Storm Trami / Kristine)"
FLOOD_DATE = "2024-10-27"

PRE_START = "2024-10-03"
PRE_END = "2024-10-27"  # EE filterDate end is exclusive

# Widened post window: full S1A repeat cycle (12 days) plus a margin.
POST_START = "2024-10-27"
POST_END = "2024-11-10"  # exclusive

BASIN_PATH = PROJECT_ROOT / "data" / "basins" / "cagayan.geojson"
S1_COLLECTION = "COPERNICUS/S1_GRD"


def format_date_utc(ts_millis) -> str:
    """Format an EE system:time_start (ms since epoch) as true UTC."""
    if not ts_millis:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(int(ts_millis) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError):
        return str(ts_millis)


def load_basin_geometry(ee):
    import geopandas as gpd

    if not BASIN_PATH.exists():
        raise FileNotFoundError(f"Cagayan basin boundary not found: {BASIN_PATH}")

    gdf = gpd.read_file(BASIN_PATH)
    if gdf.empty:
        raise RuntimeError("Cagayan basin GeoJSON contains no features.")
    if gdf.crs is None:
        raise RuntimeError("Cagayan basin GeoJSON has no CRS.")

    gdf = gdf.to_crs("EPSG:4326")
    geometry = gdf.geometry.union_all()
    if geometry.is_empty:
        raise RuntimeError("Cagayan basin geometry is empty.")

    return ee.Geometry(geometry.__geo_interface__)


def query_s1(ee, basin_geometry, start_date: str, end_date: str):
    """IW, VV+VH, intersecting the basin -- no orbit/pass filtering."""
    return (
        ee.ImageCollection(S1_COLLECTION)
        .filterBounds(basin_geometry)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
    )


def get_all_scenes(collection) -> list[dict]:
    info = collection.sort("system:time_start").getInfo()
    scenes = []
    for feature in info.get("features", []):
        props = feature.get("properties", {})
        scenes.append(
            {
                "id": feature.get("id", "N/A"),
                "date": format_date_utc(props.get("system:time_start")),
                "timestamp": props.get("system:time_start"),
                "pass": props.get("orbitProperties_pass", "N/A"),
                "relative_orbit": props.get("relativeOrbitNumber_start", "N/A"),
                "mode": props.get("instrumentMode", "N/A"),
                "polarization": props.get("transmitterReceiverPolarisation", "N/A"),
            }
        )
    return scenes


def print_scenes(label: str, scenes: list[dict]) -> None:
    print()
    print(f"--- {label} ---")
    print(f"Scenes found: {len(scenes)}")
    if not scenes:
        print("  No matching Sentinel-1 scenes found.")
        return
    for i, s in enumerate(scenes, start=1):
        print()
        print(f"  [{i}]")
        print(f"      Date/time:       {s['date']}")
        print(f"      Orbit pass:      {s['pass']}")
        print(f"      Relative orbit:  {s['relative_orbit']}")
        print(f"      IW mode:         {s['mode']}")
        print(f"      Polarization:    {s['polarization']}")
        print(f"      Scene ID:        {s['id']}")


def main() -> int:
    print("=" * 70)
    print("Extended Sentinel-1 Post-Flood Scan (no orbit filter, wide window)")
    print("=" * 70)
    print()
    print(f"Event:       {EVENT_NAME}")
    print(f"Flood date:  {FLOOD_DATE}")
    print(f"Basin:       {BASIN_PATH}")
    print(f"EE project:  {EE_PROJECT}")

    if not EE_PROJECT:
        print("[ERROR] EE_PROJECT is not configured.")
        return 1
    if not BASIN_PATH.exists():
        print(f"[ERROR] Basin boundary not found: {BASIN_PATH}")
        return 1

    try:
        import ee
    except ImportError:
        print("[ERROR] Earth Engine Python API is not installed.")
        return 1

    try:
        ee.Initialize(project=EE_PROJECT)
        print(f"[OK] Earth Engine initialized with project: {EE_PROJECT}")
    except Exception as exc:
        print(f"[ERROR] Earth Engine initialization failed: {exc}")
        return 1

    try:
        basin_geometry = load_basin_geometry(ee)
        print("[OK] Cagayan basin geometry loaded")
    except Exception as exc:
        print(f"[ERROR] Basin geometry failed: {exc}")
        return 1

    # ---- Pre-flood (unchanged window, for cross-reference) ----
    print()
    print("=" * 70)
    print(f"PRE-FLOOD SEARCH ({PRE_START} through 2024-10-26)")
    print("=" * 70)
    try:
        pre_scenes = get_all_scenes(query_s1(ee, basin_geometry, PRE_START, PRE_END))
    except Exception as exc:
        print(f"[ERROR] Pre-flood query failed: {exc}")
        return 1
    print_scenes("Pre-flood Sentinel-1 scenes", pre_scenes)

    # ---- Post-flood, widened, unfiltered by orbit ----
    print()
    print("=" * 70)
    print(f"POST-FLOOD SEARCH (WIDENED: {POST_START} through 2024-11-09)")
    print("=" * 70)
    try:
        post_scenes = get_all_scenes(query_s1(ee, basin_geometry, POST_START, POST_END))
    except Exception as exc:
        print(f"[ERROR] Post-flood query failed: {exc}")
        return 1
    print_scenes("Post-flood Sentinel-1 scenes (all orbits/passes)", post_scenes)

    # ---- Cross-check compatibility ----
    print()
    print("=" * 70)
    print("COMPATIBILITY CHECK vs. PRE-FLOOD SCENES")
    print("=" * 70)

    if not pre_scenes:
        print("[SKIP] No pre-flood scenes to compare against.")
    elif not post_scenes:
        print("[FAIL] Still no post-flood scenes in the widened window.")
        print("       This event may need to be marked missing_post_s1,")
        print("       or supplemented with a non-S1 source.")
    else:
        pre_passes = {(s["pass"], s["relative_orbit"]) for s in pre_scenes}
        pre_pass_only = {s["pass"] for s in pre_scenes}

        exact_matches = [
            s for s in post_scenes if (s["pass"], s["relative_orbit"]) in pre_passes
        ]
        pass_only_matches = [s for s in post_scenes if s["pass"] in pre_pass_only]

        print(f"Pre-flood scenes:  {len(pre_scenes)}")
        print(f"Post-flood scenes (widened window): {len(post_scenes)}")
        print()
        print(f"Exact orbit+pass matches:  {len(exact_matches)}")
        print(f"Pass-only matches:         {len(pass_only_matches)}")

        if exact_matches:
            best = exact_matches[0]
            print()
            print("[RESULT] Earliest orbit-compatible post-flood scene:")
            print(f"   Date:           {best['date']}")
            print(f"   Pass:           {best['pass']}")
            print(f"   Relative orbit: {best['relative_orbit']}")
            print(f"   ID:             {best['id']}")
        elif pass_only_matches:
            best = pass_only_matches[0]
            print()
            print("[RESULT] No exact orbit match, but earliest same-pass scene:")
            print(f"   Date:           {best['date']}")
            print(f"   Pass:           {best['pass']}")
            print(f"   Relative orbit: {best['relative_orbit']}")
            print(f"   ID:             {best['id']}")
            print("   Review acquisition geometry before using.")
        else:
            best = post_scenes[0]
            print()
            print("[RESULT] No pass-compatible scene at all. Earliest scene overall:")
            print(f"   Date:           {best['date']}")
            print(f"   Pass:           {best['pass']}")
            print(f"   Relative orbit: {best['relative_orbit']}")
            print(f"   ID:             {best['id']}")
            print("   This would require S1_REQUIRE_MATCHING_ORBIT_PASS=False")
            print("   and manual review of incidence-angle differences.")

    print()
    print("No dataset files were created or modified.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())