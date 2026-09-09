"""
main.py
-------
Orchestrates the full pipeline one flood event at a time:

    PhilSA shapefile -> flood mask
    Sentinel-1 pre/post -> SAR stack (VV_pre,VH_pre,VV_post,VH_post)
    MERIT Hydro -> hydro stack (hand, elevation, flow_acc)
    combine -> patch grid -> stats -> filter/stratify -> export -> metadata

Usage:
    python main.py --validate-one <event_id>   # validate ONE event (report + preview), no batch
    python main.py --dry-run --validate-one <event_id>   # report only, no export
    python main.py --event <event_id>          # export a single event
    python main.py --dry-run --event <event_id> # compute patch counts, don't export
    python main.py                             # batch: run all events in the CSV

Requires `earthengine authenticate` once, gee_config.EE_PROJECT set, and
your real basin asset ids in gee_config.BASINS (see gee_config.ensure_ready
which fails loudly until they are).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import ee

import exports
import gee_config as config
import merit_hydro
import philsa
import quality_control
import sentinel1
import validate
from alignment import verify_alignment
from patching import (
    compute_patch_stats,
    export_patch,
    filter_and_stratify,
    generate_patch_grid,
)


def process_event(event: philsa.FloodEvent, dry_run: bool = False,
                  validate_only: bool = False) -> None:
    """Run the full batch pipeline for one event (export to Drive/GCS)."""
    print(f"\n=== {event.event_id} ({event.basin}) ===")

    # prerequisites (shapefile exists/readable, basin known, date valid)
    prereq = quality_control.check_event_prerequisites(event)
    if not prereq["ok"]:
        for p in prereq["problems"]:
            print(f"  PREREQ FAIL: {p}")
        raise RuntimeError("; ".join(prereq["problems"]))

    flood_fc, work_region = philsa.compute_work_region_local(
        event.philsa_shapefile_path, event.basin
    )

    print("Rasterizing PhilSA mask...")
    flood_mask = philsa.rasterize_flood_mask(flood_fc, work_region)

    print("Finding + preprocessing Sentinel-1 pre/post scenes...")
    sar = sentinel1.get_temporal_sar_stack(work_region, event.flood_date, basin=event.basin)
    sar_stack = sar["image"]
    sar_meta = sar["sar_meta"]

    pre_date = dt.date.fromisoformat(sar_meta["pre_s1_date"])
    post_date = dt.date.fromisoformat(sar_meta["post_s1_date"])

    print("Fetching MERIT Hydro...")
    hydro_stack = merit_hydro.get_merit_hydro_stack(work_region)

    full_stack = sar_stack.addBands(hydro_stack)

    if validate_only:
        use_align = verify_alignment(sar_stack, hydro_stack, flood_mask, work_region)
        print("Alignment verify:", use_align)

    print("Generating patch grid...")
    grid = generate_patch_grid(work_region)
    epsg, patches = grid["epsg"], grid["patches"]
    print(f"  {len(patches)} candidate patches")

    if not patches:
        raise RuntimeError(f"No patches intersect event {event.event_id} working region — skipping.")

    print("Computing per-patch stats (single reduceRegions call)...")
    stats = compute_patch_stats(full_stack, flood_mask, patches, epsg)

    print("Filtering + stratifying by flood coverage...")
    selected = filter_and_stratify(patches, stats)
    print(f"  {len(selected)} patches selected for export")

    if dry_run:
        return

    if validate_only:
        print("validate-only mode: NOT exporting full patch set.")
        return

    # Batch: export each selected patch + record metadata
    exports.export_event_patches(
        full_stack, flood_mask, selected, epsg, event, sar_meta
    )
    print(f"  {len(selected)} export tasks started. Monitor progress at "
          f"https://code.earthengine.google.com/tasks")


def run_validation_one(event: philsa.FloodEvent, dry_run: bool = False) -> None:
    """--validate-one: run the end-to-end single-event validation + preview."""
    report = validate.validate_one_event(event, dry_run=dry_run)
    validate.write_validation_report(report)
    status = report.get("status", "failed")
    failures = report.get("failures", [])
    print(f"Validation for {event.event_id}: {status}")
    if failures:
        print(f"  Failures: {', '.join(failures)}")
    print(f"  Selected patches: {len(report.get('selected_patches', []))}")
    if report.get("preview_patches"):
        print(f"  Preview exports started: {len(report['preview_patches'])}")
        for p in report["preview_patches"]:
            print(f"    {p['patch_id']}  task={p['task_id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Philippine flood-seg dataset extraction (GEE)")
    parser.add_argument("--event", help="Run only this event_id")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute patch counts / validation report without exporting")
    parser.add_argument("--validate-one", action="store_true",
                        help="Run single-event validation (report + small preview). Do this for "
                             "ONE event and inspect before batch.")
    args = parser.parse_args()

    # Fail loudly if config has unresolved placeholders.
    config.ensure_ready()

    ee.Initialize(project=config.EE_PROJECT)

    events = philsa.load_events()
    if args.event:
        events = [e for e in events if e.event_id == args.event]
        if not events:
            raise SystemExit(f"No event with event_id={args.event} in {config.PHILSA_EVENTS_CSV}")

    if args.validate_one:
        if len(events) != 1:
            raise SystemExit("--validate-one requires exactly one --event <event_id>")
        run_validation_one(events[0], dry_run=args.dry_run)
        return

    # Batch (or single --event) processing
    summary_counts = {}
    for event in events:
        try:
            process_event(event, dry_run=args.dry_run, validate_only=False)
            summary_counts[event.event_id] = "ok"
        except Exception as exc:  # noqa: BLE001 - continue past a bad event
            cat = quality_control.classify_event_failure(exc)
            print(f"  FAILED: {event.event_id}: {cat}: {exc}")
            summary_counts[event.event_id] = cat

    # Print a compact validation summary
    print("\n=== Batch summary ===")
    for eid, status in summary_counts.items():
        print(f"  {eid}: {status}")


if __name__ == "__main__":
    main()