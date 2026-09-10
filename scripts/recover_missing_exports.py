"""
recover_missing_exports.py
--------------------------
Manifest-driven recovery exporter. Submits GEE export tasks for the validated
patches that are missing locally, WITHOUT re-running filter_and_stratify.

The authoritative 305-patch set lives in work/_auth_manifest.json (built from
the git-tracked data/validation_report.json). This tool:

  1. Loads the authoritative manifest.
  2. Computes the "missing" set = authoritative patches with no local TIFF.
  3. Rebuilds exact patch geometry per event by recomputing the *deterministic*
     patch grid (generate_patch_grid) and selecting only the manifest (row,col)
     pairs -- NO filter_and_stratify, NO new subset.
  4. Idempotently submits a GEE export task for each missing patch, skipping
     any that already have a local TIFF, a COMPLETED task, a READY/RUNNING
     task, or an existing metadata record for the same patch_id.

Usage (do NOT auto-submit; run --dry-run first):
  python scripts/recover_missing_exports.py --dry-run
  python scripts/recover_missing_exports.py            # requires --confirm

Safety:
  - --confirm is required to actually start any task.
  - No patch is submitted if its task id already exists (duplicate check).
  - The 14 non-authoritative extra Cagayan TIFFs are never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Must be on the path BEFORE importing the top-level pipeline modules.
sys.path.insert(0, ROOT)

import ee

import gee_config as config

PATCH_ID_RE = re.compile(r"^(?P<event>[A-Za-z0-9_-]+)_r(?P<row>\d+)_c(?P<col>\d+)$")
MANIFEST = os.path.join(ROOT, "work", "_auth_manifest.json")
EXPECTED_COUNTS = {"cagayan_2024-10-27": 44, "pampanga_2024-07-23": 101,
                   "agno_2023-09-03": 69}


def _local_tifs() -> set[str]:
    local = set()
    for ev in os.listdir(os.path.join(ROOT, "dataset")):
        p = os.path.join(ROOT, "dataset", ev, "patches")
        if not os.path.isdir(p):
            continue
        for f in os.listdir(p):
            if f.endswith(".tif"):
                local.add(f[:-4])
    return local


def _load_authoritative() -> list[dict]:
    if not os.path.exists(MANIFEST):
        sys.exit(f"Manifest not found: {MANIFEST}. Run work/_build_manifest.py first.")
    data = json.load(open(MANIFEST))
    au = data["authoritative"]
    # sanity: authoritative must be exactly the 305
    assert len(au) == 305, f"authoritative manifest != 305 ({len(au)})"
    return au


def _live_task_state() -> dict:
    """Return {state: {patch_id: taskid}} from the live GEE task list."""
    snap: dict = {"COMPLETED": {}, "RUNNING": {}, "READY": {}, "FAILED": {}}
    for t in ee.batch.Task.list():
        st = (t.status() or {}).get("state", "")
        cfg = t.config or {}
        desc = cfg.get("description", "") or ""
        if not PATCH_ID_RE.match(desc):
            continue
        if st in snap:
            snap[st][desc] = t.id
    return snap


def _metadata_patch_ids() -> set[str]:
    path = os.path.join(ROOT, "data", "patch_metadata.csv")
    if not os.path.exists(path):
        return set()
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return {r["patch_id"] for r in csv.DictReader(f)}


def build_missing(authoritative: list[dict], local: set[str]) -> list[dict]:
    """Authoritative patches with no local TIFF, preserving manifest order."""
    return [m for m in authoritative if m["patch_id"] not in local]


def check_counts(missing: list[dict]) -> None:
    """Verify the dry-run numbers; exit non-zero if they don't match."""
    from collections import defaultdict
    by_ev = defaultdict(int)
    for m in missing:
        by_ev[m["event_id"]] += 1
    total = len(missing)
    ok = True
    if total != 214:
        print(f"[E] total missing = {total}, expected 214"); ok = False
    for ev, n in EXPECTED_COUNTS.items():
        if by_ev[ev] != n:
            print(f"[E] {ev} missing = {by_ev[ev]}, expected {n}"); ok = False
    if not ok:
        sys.exit("Dry-run count mismatch — aborting. Inspect manifest.")


def _reconstruct_patch_id(event_id: str, row: int, col: int) -> str:
    return f"{event_id}_r{row:03d}_c{col:03d}"


def _manifest_epsg(event_id: str) -> str | None:
    """Authoritative per-event EPSG recorded by the validation run."""
    data = json.load(open(MANIFEST))
    return data.get("event_epsg", {}).get(event_id)


def preflight(missing: list[dict]) -> None:
    """Read-only: verify every missing (event,row,col) reconstructs to a real
    patch rect in the deterministic grid and to the exact authoritative
    patch_id. Submits NOTHING."""
    from collections import defaultdict

    by_ev = defaultdict(list)
    for m in missing:
        by_ev[m["event_id"]].append(m)

    author = _load_authoritative()
    local = _local_tifs()
    print("=== RECOVERY PREFLIGHT ===")
    print(f"  Authoritative patches: {len(author)}")
    print(f"  Already-local authoritative: {len(set(m['patch_id'] for m in author) & local)}")
    print(f"  Missing authoritative: {len(missing)}")

    total_resolved = total_failed = 0
    failures: list[tuple[str, str]] = []
    for event_id in ["cagayan_2024-10-27", "pampanga_2024-07-23", "agno_2023-09-03"]:
        items = by_ev.get(event_id, [])
        resolved = failed = 0
        manifest_epsg = _manifest_epsg(event_id)
        try:
            epsg, mapping = rebuild_grid_mapping(event_id)
        except Exception as exc:  # noqa: BLE001
            epsg = None
            mapping = {}
            for m in items:
                failed += 1
                failures.append((m["patch_id"], f"grid rebuild error: {exc}"))

        for m in items:
            rc = (m["row"], m["col"])
            rect = mapping.get(rc)
            recon = _reconstruct_patch_id(event_id, m["row"], m["col"])
            problems = []
            if rect is None:
                problems.append("(row,col) has no rect in recomputed grid")
            if recon != m["patch_id"]:
                problems.append(f"reconstructed id {recon!r} != manifest {m['patch_id']!r}")
            if manifest_epsg is not None and epsg != manifest_epsg:
                problems.append(f"CRS {epsg} != manifest {manifest_epsg}")
            if problems:
                failed += 1
                failures.append((m["patch_id"], "; ".join(problems)))
            else:
                resolved += 1
        total_resolved += resolved
        total_failed += failed
        print(f"\n{event_id}:")
        print(f"  Missing: {len(items)}")
        print(f"  Resolved: {resolved}")
        print(f"  Failed: {failed}")

    print(f"\nTOTAL:")
    print(f"  Missing: {len(missing)}")
    print(f"  Resolved: {total_resolved}")
    print(f"  Failed: {total_failed}")
    print(f"\nGEE tasks submitted: 0")

    if total_failed == 0 and total_resolved == len(missing):
        print("\nPREFLIGHT PASSED")
        print(f"{total_resolved}/{len(missing)} missing authoritative patches resolved.")
        print("0 GEE tasks submitted.")
        print("Safe to proceed with --confirm.")
    else:
        print("\nPREFLIGHT FAILED — do NOT proceed with --confirm.")
        for pid, reason in failures:
            print(f"  {pid}: {reason}")
        sys.exit(1)


def rebuild_grid_mapping(event_id: str) -> tuple[str, dict]:
    """Recompute the deterministic patch grid for one event.

    Returns (epsg, {(row,col): rect}). The grid is the SAME deterministic
    mapping the validation run used (see patching.generate_patch_grid). We do
    NOT run filter_and_stratify — we only reconstruct (row,col) -> rect.
    """
    import philsa
    from patching import generate_patch_grid

    event = next(e for e in philsa.load_events() if e.event_id == event_id)
    flood_fc, work_region = philsa.compute_work_region_local(
        event.philsa_shapefile_path, event.basin
    )
    grid = generate_patch_grid(work_region)
    epsg = grid["epsg"]
    mapping = {(p["row"], p["col"]): p["rect"] for p in grid["patches"]}
    return epsg, mapping


def prepare_export(missing: list[dict], dry_run: bool, confirm: bool):
    """Group missing by event; resolve idempotency; submit or report."""
    from collections import defaultdict
    import philsa
    from patching import export_patch
    import merit_hydro
    import sentinel1

    events = {e.event_id: e for e in philsa.load_events()}
    local = _local_tifs()
    metadata_ids = _metadata_patch_ids()

    # Idempotency: recompute = missing minus those that already exist now.
    live = _live_task_state()
    existing = set(local) | set(live["COMPLETED"]) | set(live["RUNNING"]) | set(live["READY"])
    existing |= metadata_ids

    to_export = [m for m in missing if m["patch_id"] not in existing]
    already = [m for m in missing if m["patch_id"] in existing]

    if not confirm and not dry_run:
        sys.exit("Refusing to submit without --confirm.")

    by_ev = defaultdict(list)
    for m in to_export:
        by_ev[m["event_id"]].append(m)

    for event_id, items in sorted(by_ev.items()):
        event = events[event_id]
        print(f"\n=== {event_id} ({len(items)} to export) ===")
        epsg, mapping = rebuild_grid_mapping(event_id)
        flood_fc, work_region = philsa.compute_work_region_local(
            event.philsa_shapefile_path, event.basin
        )
        flood_mask = philsa.rasterize_flood_mask(flood_fc, work_region)
        sar = sentinel1.get_temporal_sar_stack(work_region, event.flood_date, basin=event.basin)
        sar_stack = sar["image"]
        hydro = merit_hydro.get_merit_hydro_stack(work_region)
        full_stack = sar_stack.addBands(hydro)

        for m in items:
            rc = (m["row"], m["col"])
            if rc not in mapping:
                print(f"  [E] {m['patch_id']} has no rect in recomputed grid — skip")
                continue
            rect = mapping[rc]
            patch = {"row": m["row"], "col": m["col"], "rect": rect}
            if dry_run:
                print(f"  ~ {m['patch_id']} (would export, row={m['row']} col={m['col']})")
                continue
            task = export_patch(full_stack, flood_mask, patch, epsg, event_id)
            print(f"  + {m['patch_id']}  task={task.id}")

    print(f"\n=== {('DRY-RUN' if dry_run else 'EXPORT')} RESULT ===")
    print(f"  to_export: {len(to_export)}")
    print(f"  already_handled (local/completed/running/metadata): {len(already)}")
    for a in already:
        print(f"    skip {a}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute counts + full list, submit nothing")
    ap.add_argument("--preflight", action="store_true",
                    help="Read-only: verify every missing (event,row,col) reconstructs "
                         "to a real patch rect and to the exact manifest patch_id. "
                         "Submits nothing.")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually start the GEE export tasks (required, along with absence of --dry-run/--preflight)")
    args = ap.parse_args()

    config.ensure_ready()
    ee.Initialize(project=config.EE_PROJECT)

    author = _load_authoritative()
    local = _local_tifs()
    missing = build_missing(author, local)

    print("=== RECOVERY EXPRESS ===")
    print(f"  authoritative: {len(author)}")
    print(f"  already-local authoritative: {len(set(m['patch_id'] for m in author) & local)}")
    print(f"  missing authoritative: {len(missing)}")
    from collections import defaultdict
    mc = defaultdict(int)
    for m in missing:
        mc[m["event_id"]] += 1
    for ev in ["cagayan_2024-10-27", "pampanga_2024-07-23", "agno_2023-09-03"]:
        print(f"    {ev}: {mc[ev]}")

    check_counts(missing)

    if args.preflight:
        preflight(missing)
        return

    if args.dry_run:
        print("\n=== FULL MISSING LIST (214) ===")
        for m in missing:
            print(f"  {m['patch_id']}")
        print("\nDry-run verified. 0 GEE tasks submitted.")
        return

    if not args.confirm:
        sys.exit("Not dry-run and no --confirm — refusing to submit. Pass --confirm to proceed.")

    prepare_export(missing, dry_run=False, confirm=True)


if __name__ == "__main__":
    main()