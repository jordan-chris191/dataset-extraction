"""
merit_hydro.py
--------------
Retrieves the MERIT Hydro topographic/hydrographic stack (HAND, elevation,
upstream drainage area / flow accumulation) for a region, renamed to the
pipeline's fixed band names, and reprojected to the working 10 m grid via
bilinear resampling (these are continuous fields; nearest would introduce
blocky artifacts at patch scale).

The module is deliberately isolated and band-map-driven: add a new MERIT
layer later by adding one entry to gee_config.MERIT_BAND_MAP — no changes
needed to the rest of the pipeline.
"""

from __future__ import annotations

import ee

import gee_config as config


def get_merit_hydro_stack(
    region: ee.Geometry, resample: str = config.MERIT_RESAMPLE
) -> ee.Image:
    """
    Returns a 3-band image: hand, elevation, flow_acc, clipped to region,
    reprojected to a 10 m grid (bilinear for continuity).

    Geospatial correctness: MERIT/Hydro/v1_0_1 is native ~90 m (3-arc-sec).
    We call .resample() on the source before any reduceRegions/export so the
    grid conversion from 90 m -> 10 m uses bilinear interpolation rather
    than nearest-neighbor blocky pixels. The SAR/mask bands remain at their
    near-native grid; only the continuous hydrographic fields are smoothed.
    """
    src = ee.Image(config.MERIT_HYDRO_IMAGE)
    native_names = list(config.MERIT_BAND_MAP.values())
    target_names = list(config.MERIT_BAND_MAP.keys())

    stack = src.select(native_names, target_names)

    if resample == "bilinear":
        # .resample() sets the request's interpolation for the subsequent
        # reduceRegions / export; we do not force an explicit reproject here
        # because EE applies it lazily at the export scale.
        stack = stack.resample("bilinear")

    return stack.clip(region)