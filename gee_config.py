"""
gee_config.py
-------------
Central configuration for the Philippine multi-basin flood segmentation
dataset pipeline (Earth Engine backend).

All tunable parameters live here. The other modules import from this file
and never hard-code pipeline parameters. Values that *must* be provided by
you (your real GEE project, real basin asset ids, real PhilSA shapefile
paths, real flood dates) are validated at import time so the pipeline fails
loudly instead of silently producing a wrong dataset.

Edit `config.REQUIRED_PLACEHOLDERS` behavior / `validate` if your project
uses different identifiers.
"""

from __future__ import annotations

import os
from typing import Optional

# ---------------------------------------------------------------------------
# Earth Engine project (Cloud project used for API quota / exports)
# ---------------------------------------------------------------------------
# Leave as None until you have your real GEE-enabled Cloud project id.
# The pipeline refuses to run with a literal placeholder like
# "your-gee-project-id".
EE_PROJECT: Optional[str] = "flood-thesis-507015"

# Basin boundary sources. Each entry is a dict that supplies the basin
# geometry in ONE of two ways:
#
#   A. "asset_id": an Earth Engine table asset id,
#        e.g. "projects/<your-project>/assets/cagayan_basin".
#   B. "local_path": a local shapefile/GeoJSON boundary file (geopandas
#        readable), e.g. "data/basins/cagayan.geojson".
#
# Use whichever you have. If you pass both, "local_path" wins (no EE
# dependency for the boundary). The key in the events CSV (`basin` column)
# must match the dict key here.
#
#   Key  = short basin label used in the events CSV (must match `basin`).
BASINS = {
    "cagayan":  {"local_path": "data/basins/cagayan_bbox.geojson"},
    "pampanga": {"local_path": "data/basins/pampanga.geojson"},
    "agusan":   {"local_path": "data/basins/agusan.geojson"},
}

# ---------------------------------------------------------------------------
# Export destination
# ---------------------------------------------------------------------------
EXPORT_DESTINATION = "drive"          # "drive" or "gcs"
DRIVE_FOLDER = "flood_seg_dataset"    # used if EXPORT_DESTINATION == "drive"
GCS_BUCKET: Optional[str] = None      # used only if EXPORT_DESTINATION == "gcs"
GCS_PREFIX = "flood_seg_dataset"

# ---------------------------------------------------------------------------
# PhilSA flood events index (CSV, one row per event)
# Required columns:
#   event_id, basin, philsa_shapefile_path, flood_date
#   - event_id   : unique string, e.g. "pampanga_2024-07-24"
#   - basin      : must match a key in BASINS (basin label in the CSV)
#   - path       : local path to the PhilSA flood-extent shapefile (.shp with
#                  sibling .shx/.dbf/.prj). The pipeline reads this locally
#                  and converts to an EE FeatureCollection via GeoJSON; it
#                  does NOT require uploading each shapefile as an EE asset.
#   - flood_date : ISO "YYYY-MM-DD" — the PhilSA reference max-flood date.
# ---------------------------------------------------------------------------
PHILSA_EVENTS_CSV = "data/philsa_events.csv"

# ---------------------------------------------------------------------------
# Sentinel-1 search parameters (see sentinel1.py for the selection policy)
# ---------------------------------------------------------------------------
S1_COLLECTION = "COPERNICUS/S1_GRD"
S1_POLARIZATIONS = ["VV", "VH"]
S1_INSTRUMENT_MODE = "IW"

# Force pre/post to share the same orbit pass (ASCENDING/DESCENDING) so
# incidence-angle geometry doesn't confound the temporal-difference signal.
# Set to False to allow either pass (matcher then picks closest-in-time).
S1_REQUIRE_MATCHING_ORBIT_PASS = True

# Temporal selection windows (applied around flood_date):
#   pre  : look back this many days for the latest valid scene strictly
#          BEFORE (flood_date - PRE_FLOOD_MIN_GAP_DAYS).
#   post : look forward this many days for the earliest scene at-or-after
#          flood_date (minimizes floodwater recession before observation).
PRE_FLOOD_SEARCH_WINDOW_DAYS = 24
PRE_FLOOD_MIN_GAP_DAYS = 1

# NOTE (2024-10-27 Cagayan / Trami event): default of 5 days found ZERO
# post-flood Sentinel-1 scenes for this basin -- only S1A was operational
# (S1B failed Dec 2021, S1C not yet up), so the repeat cycle for the one
# relative orbit covering this basin (orbit 32, descending) is 12 days.
# The next available scene was 2024-11-07, which unfortunately coincides
# with Typhoon Marce/Yinxing's landfall in northern Cagayan the same day.
# Widened to 14 days so the pipeline's own selection logic can find that
# scene (previously it would silently report missing_post_s1). Geometric
# check confirmed the PhilSA flood mask for this event sits ~55 km south
# of Marce's confirmed impact zone, but confirm via --validate-one preview
# before trusting this event -- see validation_report.json / preview tifs.
POST_FLOOD_SEARCH_WINDOW_DAYS = 14

# ---------------------------------------------------------------------------
# Speckle filtering (applied to VV/VH before stacking; also for preview)
# ---------------------------------------------------------------------------
SPECKLE_FILTER: Optional[str] = "focal_median"   # "focal_median" | None
SPECKLE_KERNEL_RADIUS_M = 50

# ---------------------------------------------------------------------------
# MERIT Hydro
# Native ~90 m (3 arc-sec); reprojected to the working grid at export.
# Band mapping: pipeline name -> MERIT asset band name.
# Extend this dict to add more layers later without touching other modules.
# ---------------------------------------------------------------------------
MERIT_HYDRO_IMAGE = "MERIT/Hydro/v1_0_1"
MERIT_BAND_MAP = {
    "hand": "hnd",          # Height Above Nearest Drainage
    "elevation": "elv",     # elevation, EGM96-adjusted
    "flow_acc": "upa",      # upstream drainage area (km^2) — flow accumulation proxy
}

# ---------------------------------------------------------------------------
# Hydro rescaling choice.
# MERIT is native ~90 m. When reprojected to the 10 m grid we offer a
# bilinear resample for continuous fields (HAND / elevation / flow_acc) to
# avoid blocky artifacts at patch scale. nearest is the EE default.
#   "bilinear" | "nearest"
# ---------------------------------------------------------------------------
MERIT_RESAMPLE = "bilinear"

# ---------------------------------------------------------------------------
# Grid / patching
# ---------------------------------------------------------------------------
TARGET_RESOLUTION_M = 10
PATCH_SIZE_PX = 256
PATCH_SIZE_M = PATCH_SIZE_PX * TARGET_RESOLUTION_M   # 2560 m
PATCH_STRIDE_PX = 256          # == PATCH_SIZE_PX for non-overlapping tiling

# ---------------------------------------------------------------------------
# Output bands, fixed order. This order must match model input expectations
# AND the order used for the single multi-band GeoTIFF export.
# ---------------------------------------------------------------------------
OUTPUT_BANDS = [
    "VV_pre", "VH_pre",
    "VV_post", "VH_post",
    "hand", "elevation", "flow_acc",
]
LABEL_BAND = "flood_mask"
# Full band list in the exported GeoTIFF = OUTPUT_BANDS + [LABEL_BAND].

# ---------------------------------------------------------------------------
# Quality control (see quality_control.py)
# ---------------------------------------------------------------------------
# Maximum allowed no-data fraction per patch (max across all bands).
MAX_NODATA_FRACTION = 0.02
# Minimum valid-pixel percentage per patch (0-100). If a patch has fewer
# valid pixels than this, it is rejected. Set to 0 to disable.
MIN_VALID_PIXEL_PERCENT = 1.0
# Optional minimum flood coverage for a patch to survive the QC (0-100).
# Set to 0 to keep all flood fractions subject to stratification (recommended:
# the dataset needs dry/partial examples too — keep this at 0 and rely on
# FLOOD_COVERAGE_BUCKETS + MAX_PATCHES_PER_BUCKET_PER_EVENT).
MIN_FLOOD_PIXEL_PERCENT = 0.0
# Minimum number of patches a validated event should yield before it is
# treated as "insufficient coverage" in the failure report.
# NOTE (thesis requirement): an event that yields ZERO patches is a failure —
# it produces no training samples and would pollute the dataset as a "valid"
# event with no data. Keep this >= 1 so an empty grid marks the event failed.
# Set to 0 only to deliberately disable the check.
MIN_PATCHES_PER_EVENT = 1

# Stratified cap: buckets patch flood-coverage fractions (0-1) and caps the
# number of patches kept per bucket per event, so an event dominated by dry
# background — or by flood — doesn't skew the dataset.
FLOOD_COVERAGE_BUCKETS = [
    (0.00, 0.01),   # effectively no flood (dry / non-flood hard negatives)
    (0.01, 0.10),
    (0.10, 0.30),
    (0.30, 0.60),
    (0.60, 1.00),
]
MAX_PATCHES_PER_BUCKET_PER_EVENT = 40   # soft cap; tune after first run

# ---------------------------------------------------------------------------
# Production/validation run controls
# ---------------------------------------------------------------------------
# When running `--validate-one`, this controls the number of preview patches
# (GeoTIFFs) exported so you can visually inspect alignment before batch.
VALIDATE_PREVIEW_PATCHES = 4
# preview files are written to <out>/preview/<event_id>/
PREVIEW_DIR = "preview"

# ---------------------------------------------------------------------------
# Local paths / output structure
# ---------------------------------------------------------------------------
# Root output directory. Layout:
#   <OUTPUT_ROOT>/<event_id>/metadata.json
#   <OUTPUT_ROOT>/<event_id>/patches/<patch_id>.tif
# plus <METADATA_CSV_PATH> (master CSV, one row per patch).
OUTPUT_ROOT = "dataset"
METADATA_CSV_PATH = "data/patch_metadata.csv"
VALIDATION_REPORT_PATH = "data/validation_report.json"
WORK_DIR = "work"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
# Substrings that we treat as "you still need to replace this". If a
# required string (EE_PROJECT, BASINS asset ids) contains any of these, we
# raise at import.
_PLACEHOLDER_MARKERS = ("your-gee-project-id", "your-project", "your-bucket",
                         "basin_a", "basin_b", "basin_c", "basin_d", "basin_e")


def _looks_placeholder(value: str) -> bool:
    return any(m in (value or "").lower() for m in _PLACEHOLDER_MARKERS)


def validate() -> list[str]:
    """Return a list of user-action-required messages. Empty == ready to run."""
    problems: list[str] = []

    if not EE_PROJECT or _looks_placeholder(EE_PROJECT):
        problems.append(
            "EE_PROJECT is not set. Set your real GEE-enabled Cloud project id "
            "(see https://coders.dev or the GEE docs) in gee_config.py."
        )

    if not BASINS:
        problems.append(
            "BASINS is empty. Add each basin boundary either as an EE table asset "
            "(`asset_id`) or a local shapefile/GeoJSON (`local_path`) in "
            "gee_config.BASINS (keys must match the `basin` column of your events CSV)."
        )
    else:
        for label, info in BASINS.items():
            if not isinstance(info, dict) or not (info.get("asset_id") or info.get("local_path")):
                problems.append(
                    f"BASINS['{label}'] must be a dict with an 'asset_id' or 'local_path'."
                )
                continue
            if _looks_placeholder(info.get("asset_id", "")) and _looks_placeholder(
                info.get("local_path", "")
            ):
                problems.append(
                    f"BASINS['{label}'] is still a placeholder. Provide a real "
                    f"EE asset id or a real local boundary file path."
                )

    if EXPORT_DESTINATION not in ("drive", "gcs"):
        problems.append(f"EXPORT_DESTINATION must be 'drive' or 'gcs', got {EXPORT_DESTINATION!r}.")
    if EXPORT_DESTINATION == "gcs" and (not GCS_BUCKET or _looks_placeholder(GCS_BUCKET)):
        problems.append("EXPORT_DESTINATION='gcs' requires a real GCS_BUCKET.")

    if SPECKLE_FILTER not in (None, "focal_median", "refined_lee"):
        problems.append(f"Unsupported SPECKLE_FILTER: {SPECKLE_FILTER!r}.")
    if SPECKLE_FILTER == "refined_lee":
        # Not implemented in sentinel1.py yet.
        problems.append("SPECKLE_FILTER='refined_lee' is not implemented; use 'focal_median' or None.")

    if MERIT_RESAMPLE not in ("bilinear", "nearest"):
        problems.append(f"MERIT_RESAMPLE must be 'bilinear' or 'nearest', got {MERIT_RESAMPLE!r}.")

    if not 0 <= MAX_NODATA_FRACTION <= 1:
        problems.append("MAX_NODATA_FRACTION must be in [0, 1].")
    if not 0 <= MIN_VALID_PIXEL_PERCENT <= 100:
        problems.append("MIN_VALID_PIXEL_PERCENT must be in [0, 100].")
    if not 0 <= MIN_FLOOD_PIXEL_PERCENT <= 100:
        problems.append("MIN_FLOOD_PIXEL_PERCENT must be in [0, 100].")

    return problems


def ensure_ready():
    """Raise RuntimeError with all outstanding config problems, if any."""
    problems = validate()
    if problems:
        raise RuntimeError(
            "gee_config is not ready to run:\n  - " + "\n  - ".join(problems)
        )