"""
quality_control.py
------------------
Configurable quality-control rules for validated flood events and the
patches produced for them. Implements the "explicit failure buckets" from
the thesis spec so failed events are reported loudly, not silently skipped.

Each check returns structured info so validation_report.json can classify
an event as e.g. "missing pre-S1", "invalid geometry", "insufficient
coverage", etc.

The distinction between "flood = 0" (valid dry pixel) and "no data"
(masked) is preserved throughout: QC uses *valid-pixel* percentage, not
merely flood coverage, to avoid discarding hard negatives that are valid
but dry.
"""

from __future__ import annotations

import ee

import gee_config as config

# Ordered list of failure categories used in the validation report.
# The order matters: we stop reporting at the first failing category that
# makes the event unusable (e.g. no S1 scenes -> everything downstream is
# moot).
FAILURE_CATEGORIES = [
    "missing_pre_s1",
    "missing_post_s1",
    "vv_vh_unavailable",
    "invalid_geometry",
    "merit_unavailable",
    "rasterization_failed",
    "insufficient_valid_pixels",
    "insufficient_coverage",
    "zero_patches",
    "quality_control_failed",
]


def classify_event_failure(exc: Exception) -> str:
    """
    Best-effort classification of a raised exception into one of the
    FAILURE_CATEGORIES, so a batch run can produce a deterministic summary.
    Messages raised by sentinel1 / philsa / merit_hydro carry recognizable
    substrings (inspect the raise sites in those modules when editing this).

    Ordering matters and is deliberate:
      * S1 scene-missing cases are checked first (pre vs post), since "pre"
        and "post" are unambiguous markers of which temporal slot failed.
      * Coverage/per-frame signals ("coverage", "valid pixel", "mask pixel
        fraction too low") are matched BEFORE anything that says "mask",
        because those are about pixel statistics, not rasterization errors:
        a low flood-pixel fraction is an *insufficient-coverage* event, not a
        failed rasterization. Rasterization only fires on messages about
        actually building the mask (empty geometry / no features / rasteriz).
    """
    msg = str(exc)
    msg_l = msg.lower()

    # Temporality markers first (pre vs post) — unambiguous.
    if "pre-flood" in msg_l or "pre_s1" in msg_l:
        return "missing_pre_s1"
    if "post-flood" in msg_l or "post_s1" in msg_l:
        return "missing_post_s1"

    # Pixel-statistics / coverage signals BEFORE any bare "mask" match so a
    # "mask pixel fraction too low" message is not misread as a rasterization
    # failure.
    if "valid pixel" in msg_l or "coverage" in msg_l or "mask pixel fraction" in msg_l:
        return "insufficient_valid_pixels"

    if "vv" in msg_l and "vh" in msg_l:
        return "vv_vh_unavailable"

    # Rasterization: only fires on messages that are actually about building
    # the mask from geometry, not about pixel fractions in a finished mask.
    if (
        "no features" in msg_l
        or "non-empty geometries" in msg_l
        or "dissolved to an empty" in msg_l
        or "rasteriz" in msg_l
    ):
        return "rasterization_failed"

    # Geometry / CRS / input-shapefile problems.
    if "geometry" in msg_l or "crs" in msg_l or "empty" in msg_l:
        return "invalid_geometry"

    if "merit" in msg_l:
        return "merit_unavailable"

    return "quality_control_failed"


def check_event_prerequisites(event: object) -> dict:
    """
    Fast, local (client-side) pre-checks that need no heavy EE compute:
      * shapefile exists, is readable, has a geometry + CRS
      * basin asset key is known
      * flood_date is a plausible ISO date
    Returns {"ok": bool, "problems": [...]}.
    """
    import os
    import philsa

    problems: list[str] = []

    if event.basin not in config.BASINS:
        problems.append(f"unknown basin '{event.basin}' (must be a key in gee_config.BASINS)")

    # flood_date parse check
    import datetime as dt
    try:
        dt.date.fromisoformat(event.flood_date)
    except ValueError:
        problems.append(f"flood_date {event.flood_date!r} is not a valid ISO date (YYYY-MM-DD)")

    path = event.philsa_shapefile_path
    if not path or not os.path.exists(path):
        problems.append(f"shapefile not found: {path}")
    else:
        try:
            report = philsa.inspect_shapefile(path)
            event.source_fields = report["columns"]
            event.crs = report["crs"]
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not read shapefile: {exc}")
            report = None
        else:
            if report["n_features"] == 0:
                problems.append("shapefile has zero features")
            if report["crs"] is None:
                problems.append("shapefile has no CRS defined — cannot reproject safely")

    return {"ok": not problems, "problems": problems, "report": report if not problems else None}


def summarize_event_status(
    event_id: str,
    failures: list[str],
    warnings: list[str],
    patch_counts: dict | None = None,
) -> dict:
    """
    Structured status for an event used in validation_report.json:
      * failures: categories that make the event unusable (from
        FAILURE_CATEGORIES).
      * warnings: non-fatal notes (e.g. orbit pass forced, few scenes).
      * patch_counts: number of candidate / selected / exported patches.
    """
    status = "valid" if not failures else "failed"
    if warnings and not failures:
        status = "valid_with_warnings"
    return {
        "event_id": event_id,
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "patch_counts": patch_counts or {},
    }