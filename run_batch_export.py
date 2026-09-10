"""
run_batch_export.py
-------------------
Thin launch driver for the validated multi-basin GEE batch export to Google
Drive. It does NOT change any extraction logic: it reuses the exact same
validated functions as `main.py` (work region, S1, MERIT, grid, stats,
filter/stratify) and the exact same export path (`exports.export_event_patches`).

Differences from `python main.py` (launch control only):

  * Optional `--only events.csv` : skip any patch already successfully exported.
        A patch whose description is COMPLETED in the live GEE task list is
        treated as already-exported and is skipped, so we avoid duplicate tasks.
        FAILED historical tasks are NOT skipped (they are re-submitted).
  * `--dry-run` : compute the full pipeline (exactly as `main.py --dry-run`
        `--event` does) but do NOT start any export tasks. Reports exactly what
        would be submitted/skipped. Does not write metadata.
  * Reports per-event counts and per-patch task IDs for every submission.

Nothing about QC thresholds, band order, patch grid, S1 selection, MERIT
processing, or basin/event definitions is changed. The exported GeoTIFF band spec
remains OUTPUT_BANDS + [LABEL_BAND] in the same order.

Usage:
  python run_batch_export.py --dry-run
  python run_batch_export.py --only work/batch_skip_list.csv
  python run_batch_export.py --only work/batch_dedup_snapshot.json --confirmation
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

import ee

import gee_config as config
import philsa
import quality_control
from main import process_event  # import AFTER ee, config (no side effects)

PATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]+_r\d+_c\d+$")


# --------------------------------------------------------------------------
# Skip-set builders (launch control only; does not alter science)
# --------------------------------------------------------------------------

def _live_completed_descriptions() -> set[str]:
    """Descriptions of patch tasks currently COMPLETED in the live EE task list."""
    completed: set[str] = set()
    for t in ee.batch.Task.list():
        cfg = t.config or {}
        desc = cfg.get("description", "") or ""
        if not PATCH_ID_RE.match(desc):
            continue
        st = (t.status() or {}).get("state", "")
        if st == "COMPLETED":
            completed.add(desc)
    return completed


def _skip_set_from_csv(path: str) -> set[str]:
    """Read a 1-column (unheaded) list of patch_ids to skip, or a CSV with a
    'patch_id' column."""
    skip: set[str] = set()
    with open(path, newline="", encoding="utf-8") as f:
        sample = f.read(4096)
    is_csv = "patch_id" in sample.lower()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if is_csv:
                if row[0].strip().lower() == "patch_id":
                    continue
            skip.add(row[0].strip())
    return {s for s in skip if PATCH_ID_RE.match(s)}


def _skip_set_from_snapshot(path: str) -> set[str]:
    """Read a batch_dedup_snapshot.json {'completed': [...], ...} and skip the
    'completed' patches."""
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    return set(snap.get("completed", []))


# --------------------------------------------------------------------------
# Preflight report
# --------------------------------------------------------------------------

def preflight_report() -> list[str]:
    lines = []
    lines.append("ACTIVE BASINS (gee_config.BASINS): " + ", ".join(sorted(config.BASINS)))
    lines.append("EVENT IDS (philsa_events.csv): " + ", ".join(e.event_id for e in philsa.load_events()))
    lines.append("EXPORT_DESTINATION: %s  DRIVE_FOLDER: %s" % (config.EXPORT_DESTINATION, config.DRIVE_FOLDER))
    lines.append("EE_PROJECT: %s" % config.EE_PROJECT)
    lines.append("OUTPUT_BANDS: %s + [%s]" % (config.OUTPUT_BANDS, config.LABEL_BAND))
    lines.append("TARGET_RESOLUTION_M: %s  PATCH_SIZE_PX: %s" % (config.TARGET_RESOLUTION_M, config.PATCH_SIZE_PX))
    lines.append("MAX_NODATA_FRACTION: %s  MIN_VALID_PIXEL_PERCENT: %s  MAX_PATCHES_PER_BUCKET: %s" % (
        config.MAX_NODATA_FRACTION, config.MIN_VALID_PIXEL_PERCENT, config.MAX_PATCHES_PER_BUCKET_PER_EVENT))
    return lines


# --------------------------------------------------------------------------
# Per-event processing with skip support
# --------------------------------------------------------------------------

def process_event_skipping(
    event: philsa.FloodEvent,
    skip: set[str],
    dry_run: bool,
) -> dict:
    """Run full `main.process_event` for one event but, before exporting, prune
    `selected` to only patches NOT in `skip`. Uses the exact validated functions.
    Returns a submission report dict."""
    report = {"event_id": event.event_id, "basin": event.basin,
              "submitted": [], "skipped": [], "failures": []}

    # --- replicate main.process_event up to grid+stats+filter (identical code) ---
    prereq = quality_control.check_event_prerequisites(event)
    if not prereq["ok"]:
        raise RuntimeError("; ".join(prereq["problems"]))

    flood_fc, work_region = philsa.compute_work_region_local(
        event.philsa_shapefile_path, event.basin
    )
    import sentinel1, merit_hydro
    from patching import generate_patch_grid, compute_patch_stats, filter_and_stratify
    flood_mask = philsa.rasterize_flood_mask(flood_fc, work_region)
    sar = sentinel1.get_temporal_sar_stack(work_region, event.flood_date, basin=event.basin)
    sar_stack, sar_meta = sar["image"], sar["sar_meta"]
    hydro_stack = merit_hydro.get_merit_hydro_stack(work_region)
    full_stack = sar_stack.addBands(hydro_stack)
    grid = generate_patch_grid(work_region)
    epsg, patches = grid["epsg"], grid["patches"]
    if not patches:
        raise RuntimeError(f"No patches intersect event {event.event_id} working region — skipping.")
    stats = compute_patch_stats(full_stack, flood_mask, patches, epsg)
    selected = filter_and_stratify(patches, stats)

    selected_ids = [
        f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}" for p in selected
    ]
    to_submit = [p for p in selected
                 if f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}" not in skip]
    to_skip = [p for p in selected
               if f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}" in skip]

    report["selected_total"] = len(selected)
    for p in to_skip:
        report["skipped"].append(f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}")

    if dry_run:
        for p in to_submit:
            report["submitted"].append(f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}")
        report["dry_run"] = True
        return report

    # Exact validated export path (writes metadata CSV + per-event JSON)
    import exports
    exports.export_event_patches(
        full_stack, flood_mask, to_submit, epsg, event, sar_meta,
        event_extra={"batch_driver": "run_batch_export.py",
                     "skipped_patches": report["skipped"]},
    )
    for p in to_submit:
        pid = f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}"
        report["submitted"].append(pid)

    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Launch the validated full-batch GEE export to Drive.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute the full pipeline but do NOT start exports; report only.")
    ap.add_argument("--only", default=None,
                    help="Skip-set source: a CSV of patch_ids (no header) or a "
                         "batch_dedup_snapshot.json with 'completed'. Patches listed are NOT exported.")
    ap.add_argument("--confirmation", action="store_true",
                    help="Required to actually launch (no exports without this).")
    args = ap.parse_args()

    config.ensure_ready()
    ee.Initialize(project=config.EE_PROJECT)

    print("=== PREFLIGHT ===")
    for ln in preflight_report():
        print("  " + ln)

    # Build skip set
    skip: set[str] = set()
    if args.only:
        if args.only.endswith(".json"):
            skip = _skip_set_from_snapshot(args.only)
        else:
            skip = _skip_set_from_csv(args.only)
        print(f"  skip-set ({args.only}): {len(skip)} patch_ids "
              f"(already COMPLETED in GEE -> will not re-submit)")
    else:
        live = _live_completed_descriptions()
        print(f"  live-task completed scan: {len(live)} already-exported patch_ids "
              f"-> will be skipped automatically")
        skip = set(live)

    events = philsa.load_events()
    if not args.confirmation and not args.dry_run:
        print("\nNOTHING LAUNCHED: pass --confirmation to actually create export tasks.")
        print("Run with --dry-run first to preview counts; use --only to dedup.")
        return

    print("\n=== PROCESSING ===")
    totals = {"selected": 0, "submitted": 0, "skipped": 0, "failed": 0}
    for ev in events:
        try:
            rep = process_event_skipping(ev, skip, dry_run=args.dry_run)
            totals["selected"] += rep.get("selected_total", 0)
            totals["submitted"] += len(rep["submitted"])
            totals["skipped"] += len(rep["skipped"])
            print(f"\n[{ev.event_id}] selected={rep.get('selected_total')} "
                  f"submitted={len(rep['submitted'])} skipped={len(rep['skipped'])}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"\n[{ev.event_id}] EVENT FAILED: {exc}")
            traceback.print_exc()
            totals["failed"] += 1

    print("\n=== SUMMARY ===")
    for k, v in totals.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        print("  (dry run — no export tasks were created)")


if __name__ == "__main__":
    main()