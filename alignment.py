"""
alignment.py
------------
Central place for the few helpers that enforce the "all bands share the
same grid" invariant and that the validation report uses to *check* it.

How alignment is actually achieved:
  * Export writes ONE stacked ee.Image to a single multi-band GeoTIFF at
    crs=<local UTM> and scale=config.TARGET_RESOLUTION_M. Earth Engine
    reprojects every band onto that common grid at export time, so the
    bands are pixel-aligned by construction.
  * The PhilSA mask is rasterized (ee.Image(0).paint(fc,1)) in the same
    CRS/scale as the SAR stack (see philsa.rasterize_flood_mask).
  * MERIT Hydro (native ~90 m) is pre-resampled with bilinear inside
    merit_hydro.py so its reprojection onto the 10 m grid is smooth.

There is therefore no per-modality resampling step that could drift.
"""

from __future__ import annotations

import ee

import gee_config as config


def working_projection(region: ee.Geometry) -> ee.Projection:
    """
    The UTM projection used for the working grid. Sentinel-1 scenes are
    preserved at native ~10 m; patches are exact multiples of 10 m in
    this projection so a 256 px patch is exactly 2.56 km square.
    """
    from patching import utm_epsg_for_lonlat

    lonlat = region.centroid(maxError=1).coordinates().getInfo()
    lon, lat = lonlat[0], lonlat[1]
    epsg = utm_epsg_for_lonlat(lon, lat)
    return ee.Projection(epsg)


def verify_alignment(
    sar: ee.Image, hydro: ee.Image, mask: ee.Image, region: ee.Geometry
) -> dict:
    """
    Best-effort report used by --validate-one: sample the reported
    projection of each stack and confirm they coincide, and note the
    target scale. Because alignment is by-construction (single stacked
    export), this is a belt-and-suspenders check that also surfaces a
    human-readable statement in the validation report.
    """
    scale = config.TARGET_RESOLUTION_M

    def _proj_crs(img: ee.Image) -> str:
        try:
            info = img.projection().getInfo()
            return str(info.get("crs", "unknown"))
        except Exception:  # noqa: BLE001
            return "unknown"

    sar_p = _proj_crs(sar)
    hydro_p = _proj_crs(hydro)
    mask_p = _proj_crs(mask)

    return {
        "target_resolution_m": scale,
        "working_crs": working_projection(region).getInfo()["crs"],
        "sar_projection": sar_p,
        "hydro_projection": hydro_p,
        "mask_projection": mask_p,
        "projections_consistent": bool(sar_p == hydro_p == mask_p),
        "note": (
            "Bands are written as one stacked image for export at "
            f"crs={working_projection(region).getInfo()['crs']} and "
            f"scale={scale} m; grids coincide by construction."
        ),
    }