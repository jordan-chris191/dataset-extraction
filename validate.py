"""
validate.py
-----------
Single-event validation mode (--validate-one). Runs ONE flood event
end-to-end through the pipeline but, instead of exporting the full patch
grid, produces:

  * a JSON validation report (validation_report.json) covering:
      1. PhilSA mask geometry
      2. pre-flood Sentinel-1 date
      3. post-flood Sentinel-1 date
      4. VV/VH availability
      5. MERIT Hydro availability
      6. projection
      7. resolution
      8. spatial alignment
      9. rasterization
     10. 256x256 patch generation
     11. flood-pixel percentage
     12. invalid/no-data percentage
  * a small preview/export (config.VALIDATE_PREVIEW_PATCHES patches) so you
    can visually inspect whether the SAR imagery, hydrographic layers, and
    the flood mask actually correspond spatially.

Only after `--validate-one` passes should you run batch (`--event` one event
at a time, then full `python main.py`).

The preview is written to <OUTPUT_ROOT>/preview/<event_id>/ and uses the
SAME export path as production (single multi-band GeoTIFF) so what you
inspect is exactly what the batch would produce.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import ee

import gee_config as config
import metadata
import merit_hydro
import patching
import philsa
import quality_control
import sentinel1


def _safe(fn, default=None):
    """Run fn() and return default on any exception (report-only helper)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def validate_one_event(event: philsa.FloodEvent, dry_run: bool = False) -> dict:
    """
    Run the full pipeline for one event and return a validation dict.
    If dry_run=True, computes stats and report but does NOT start exports;
    a small preview still may be produced (see config.VALIDATE_PREVIEW_PATCHES).
    """
    report: dict = {
        "event_id": event.event_id,
        "basin": event.basin,
        "flood_date": event.flood_date,
        "checks": {},
        "failures": [],
        "warnings": [],
        "selected_patches": [],
        "preview_patches": [],
    }

    # 1. Prerequisites (shapefile exists/readable, basin known, date valid)
    prereq = quality_control.check_event_prerequisites(event)
    report["checks"]["prerequisites"] = prereq
    if not prereq["ok"]:
        report["failures"].extend(
            quality_control.classify_event_failure(Exception("; ".join(prereq["problems"])))
            for _ in prereq["problems"]
        )
        report["failures"] = [f for f in report["failures"] if f]
        report["status"] = "failed"
        return report

    # Region = flood polygon buffered, intersected with basin geometry.
    basin_geom = philsa.get_basin_geometry(event.basin)
    flood_fc = philsa.shapefile_to_ee_featurecollection(
        event.philsa_shapefile_path, report=prereq["report"]
    )
    flood_geom = flood_fc.geometry()
    work_region = (
        flood_geom.buffer(2000)
        .intersection(basin_geom, maxError=1)
    )

    # 2-5. Sentinel-1 pre/post + MERIT
    #
    # NOTE (fixed): previously this used _safe(...) which swallowed the
    # real exception from get_temporal_sar_stack (e.g. the orbit_pass
    # KeyError bug in sentinel1.py, or a genuine "no scene found" error)
    # and then ALWAYS reported "missing_pre_s1" regardless of the actual
    # cause -- `"missing_pre_s1" if True else "missing_post_s1"` is a
    # dead condition that can never take the else branch. We now capture
    # the real exception and classify it properly via
    # quality_control.classify_event_failure, so the report reflects what
    # actually happened (pre missing, post missing, a code bug, etc).
    try:
        sar = sentinel1.get_temporal_sar_stack(work_region, event.flood_date)
    except Exception as exc:  # noqa: BLE001
        failure_cat = quality_control.classify_event_failure(exc)
        report["checks"]["s1"] = {"ok": False, "detail": str(exc)}
        report["failures"].append(failure_cat)
        report["status"] = "failed"
        return report

    report["checks"]["s1"] = {
        "ok": True,
        "pre_s1_date": sar["sar_meta"]["pre_s1_date"],
        "post_s1_date": sar["sar_meta"]["post_s1_date"],
        "vv_vh_available": True,
        "orbit_pass": sar["sar_meta"]["orbit_pass"],
        "pre_relative_orbit": sar["sar_meta"]["pre_relative_orbit"],
        "post_relative_orbit": sar["sar_meta"]["post_relative_orbit"],
        "pre_selection_reason": sar["sar_meta"]["pre_selection_reason"],
        "post_selection_reason": sar["sar_meta"]["post_selection_reason"],
    }

    hydro = _safe(lambda: merit_hydro.get_merit_hydro_stack(work_region))
    report["checks"]["merit_hydro"] = {
        "ok": hydro is not None,
        "detail": "hand, elevation, flow_acc" if hydro is not None else "unavailable",
    }
    if hydro is None:
        report["failures"].append("merit_unavailable")

    # 6-9. Projection, resolution, alignment, rasterization
    mask = philsa.rasterize_flood_mask(flood_fc, work_region)
    alignment = alignment_report = None
    try:
        from alignment import verify_alignment
        alignment_report = verify_alignment(sar["image"], hydro, mask, work_region)
        report["checks"]["alignment"] = alignment_report
    except Exception as exc:  # noqa: BLE001
        report["checks"]["alignment"] = {"ok": False, "detail": str(exc)}
        report["failures"].append("quality_control_failed")

    # Build the combined stack (same as production)
    full_stack = sar["image"].addBands(hydro)

    # 10-12. Patch generation + patch stats
    grid_info = patching.generate_patch_grid(work_region)
    epsg = grid_info["epsg"]
    patches = grid_info["patches"]
    report["checks"]["patch_grid"] = {
        "epsg": epsg,
        "n_patches": len(patches),
    }

    # A grid that yields zero patches is a hard failure for the thesis dataset:
    # the event produced no training samples (pollutes the dataset if recorded
    # as "valid"). Unlike main.py's raise (which aborts the whole run),
    # validation must record it as a failed event and keep going.
    if not patches:
        report["failures"].append("zero_patches")
        report["warnings"].append(
            "No patches generated within flood extent bounds"
        )
        report["status"] = "failed"
        report["checks"]["minimum_patches"] = {
            "required": config.MIN_PATCHES_PER_EVENT,
            "selected": 0,
        }
        return report

    stats = patching.compute_patch_stats(full_stack, mask, patches, epsg)
    selected = patching.filter_and_stratify(patches, stats)

    # Collect per-patch QC numbers into the report
    for p in selected:
        report["selected_patches"].append({
            "patch_id": p["patch_id"] if "patch_id" in p else f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}",
            "row": p["row"],
            "col": p["col"],
            "flood_pixel_percentage": p.get("flood_pixel_percentage"),
            "valid_pixel_percentage": p.get("valid_pixel_percentage"),
        })

    if len(selected) < config.MIN_PATCHES_PER_EVENT:
        report["failures"].append("insufficient_coverage")
    report["checks"]["minimum_patches"] = {
        "required": config.MIN_PATCHES_PER_EVENT,
        "selected": len(selected),
    }

    # Preview export (only if not dry-run)
    if not dry_run:
        n_preview = min(config.VALIDATE_PREVIEW_PATCHES, len(selected))
        for p in selected[:n_preview]:
            task = patching.export_patch(full_stack, mask, p, epsg, event.event_id)
            patch_id = f"{event.event_id}_r{p['row']:03d}_c{p['col']:03d}"
            report["preview_patches"].append({
                "patch_id": patch_id,
                "task_id": task.id,
            })

    report["status"] = "valid" if not report["failures"] else "failed"
    return report


def write_validation_report(report: dict, path: str = config.VALIDATION_REPORT_PATH,
                            events_total: int = 0) -> None:
    """Append a single-event validation report to a cumulative JSON, plus a
    compact one-line summary per event in validation_brief.txt."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    combined = []
    brief_path = path.replace(".json", "_brief.txt")

    # Load any existing cumulative report
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                combined = json.load(f)
        except Exception:  # noqa: BLE001
            combined = []

    if isinstance(combined, dict):
        combined = combined.get("events", [])

    combined.append(report)

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"events": combined}, f, indent=2, default=str)

    # Brief one-liner
    line = (
        f"{report['event_id']}\t{report.get('status','?')}\t"
        f"{','.join(report.get('failures', []))}\t"
        f"selected={report['checks'].get('minimum_patches', {}).get('selected', 0)}"
    )
    with open(brief_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def summarize_report(events: list[dict]) -> dict:
    """Aggregate a list of per-event reports into the summary asked for in
    the thesis spec: Total / Valid / per-failure-counts."""
    from collections import Counter
    counts = Counter()
    n_valid = 0
    n_valid_warn = 0
    for ev in events:
        status = ev.get("status", "failed")
        if status in ("valid", "valid_with_warnings"):
            n_valid += 1
            if status == "valid_with_warnings":
                n_valid_warn += 1
        else:
            for f in ev.get("failures", []):
                counts[f] += 1
    return {
        "total_events": len(events),
        "valid_events": n_valid,
        "valid_with_warnings": n_valid_warn,
        "failure_counts": dict(counts),
    }