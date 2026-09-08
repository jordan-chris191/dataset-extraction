"""
metadata.py
-----------
Appends one row per exported patch to a running master CSV (so the dataset
is reproducible and analyzable — e.g. slicing error analysis by basin, orbit,
or flood coverage) and writes a per-event metadata.json capturing the scene
selection decisions (the "reproducibility ledger": which S1 scenes, which
dates, orbits, parameters) alongside that event's patches/.

Master CSV fields (per thesis spec + useful extras):
    event_id, basin, patch_id,
    flood_date, pre_s1_date, post_s1_date, pre_post_days,
    orbit_pass, relative_orbit, pre_relative_orbit, post_relative_orbit,
    min_lat, max_lat, min_lon, max_lon,
    flood_pixel_percentage, valid_pixel_percentage, nodata_fraction,
    epsg, row, col,
    export_task_id, selection_reasons
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os

import gee_config as config


FIELDNAMES = [
    # identity / grouping
    "event_id",
    "basin",
    "patch_id",
    # dates
    "flood_date",
    "pre_s1_date",
    "post_s1_date",
    "pre_post_days",
    # orbit / scene
    "orbit_pass",
    "relative_orbit",
    "pre_relative_orbit",
    "post_relative_orbit",
    "pre_absolute_orbit",
    "post_absolute_orbit",
    "mission",
    "orbit_pass_matched",
    # spatial
    "epsg",
    "min_lat",
    "max_lat",
    "min_lon",
    "max_lon",
    # QC
    "flood_pixel_percentage",
    "valid_pixel_percentage",
    "nodata_fraction",
    # export bookkeeping
    "row",
    "col",
    "export_task_id",
    # provenance / reproducibility
    "pre_selection_reason",
    "post_selection_reason",
]


def pre_post_days(flood_date: str, pre_s1_date: str, post_s1_date: str) -> int:
    """Days from pre-S1 to post-S1, a useful proxy for flood-change window."""
    try:
        pre = dt.date.fromisoformat(pre_s1_date)
        post = dt.date.fromisoformat(post_s1_date)
        return (post - pre).days
    except Exception:  # noqa: BLE001
        return -1


def init_metadata_csv(path: str = config.METADATA_CSV_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()


def append_patch_record(record: dict, path: str = config.METADATA_CSV_PATH):
    """Append one patch's metadata row. `record` may omit fields; missing ones
    are left blank rather than guessed."""
    init_metadata_csv(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerow({k: record.get(k, "") for k in FIELDNAMES})


def write_event_metadata(
    event_id: str,
    event_info: dict,
    out_root: str = config.OUTPUT_ROOT,
):
    """
    Write <out_root>/<event_id>/metadata.json capturing the full selection
    ledger for an event (scene ids/dates/orbits, params, patch stats, reasons)
    so the dataset is reproducible and the choices are auditable.
    """
    out_dir = os.path.join(out_root, event_id)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "metadata.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(event_info, f, indent=2, default=str)
    return path