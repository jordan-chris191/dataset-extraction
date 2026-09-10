# Pipeline Documentation

> **This file documents the actual implementation**, verified by reading every source module.
> If the README and this document disagree, trust this document.

---

## 1. Pipeline Overview

This is an **extraction pipeline** (not a training pipeline). It extracts aligned multi-band GeoTIFF
patches from Google Earth Engine for a downstream Siamese Multi-Encoder U-Net flood segmentation
model. It combines three data sources — Sentinel-1 SAR, MERIT Hydro topography, and PhilSA
flood-extent polygons — into a single 8-band raster per patch, with a binary flood mask as the
reference label.

The extraction repository itself does **not** perform model training, evaluation, train/val/test
splitting, normalization, augmentation, or HAND threshold selection. Those are downstream modeling
operations (§16.5, §16.6).

```
PhilSA Events CSV + Basin Boundaries
                │
                ▼
     Flood Event Selection
                │
                ├────────────────► Sentinel-1 Pre/Post (VV, VH)
                │
                ├────────────────► MERIT Hydro (HAND, elev, flow_acc)
                │
                ├────────────────► PhilSA Flood Polygons
                │
                ▼
       Work Region Computation
       (client-side, avoids EE payload limits)
                │
                ▼
         Spatial Alignment
       (all bands → single stacked export image)
                │
                ▼
       Patch Grid Generation
        (UTM-based, non-overlapping)
                │
                ▼
       Patch Statistics
         (single reduceRegions call)
                │
                ▼
          QC Filter + Stratification
          (valid-pixel gate + flood buckets)
                │
                ▼
        Multi-band GeoTIFF Export
         (to Google Drive / GCS)
                │
                ▼
           Metadata Logging
        (per-patch CSV + per-event JSON)
```

---

## 2. Data Sources

| Source | GEE Asset / Local | Purpose |
|---|---|---|
| Sentinel-1 SAR | `COPERNICUS/S1_GRD` | Pre/post-flood radar backscatter (VV+VH) |
| MERIT Hydro | `MERIT/Hydro/v1_0_1` | Topographic/hydrographic features |
| PhilSA flood masks | Local `.shp` files | Binary flood-extent reference labels |
| Basin boundaries | Local `.geojson` files | Defines per-basin working region |

### 2.1 Sentinel-1 SAR

- **Collection**: `COPERNICUS/S1_GRD` (C-band GRD, IW mode)
- **Polarizations**: VV, VH
- **Values**: Calibrated sigma-0 in dB as provided by GEE; no manual dB conversion
- **Speckle filter**: `focal_median` (radius = 50 m / 10 m = 5 pixels); configurable via `SPECKLE_FILTER`

### 2.2 MERIT Hydro

- **Asset**: `MERIT/Hydro/v1_0_1`
- **Native resolution**: ~90 m (3 arc-seconds)
- **Resampled to 10 m** using **bilinear** interpolation (avoids blocky artifacts)
- **Bands used**:
  - `hnd` → `hand` (Height Above Nearest Drainage)
  - `elv` → `elevation` (EGM96-adjusted)
  - `upa` → `flow_acc` (upstream drainage area in km², proxy for flow accumulation)

### 2.3 PhilSA Flood Reference Data

- Source: Philippine Space Agency (PhilSA) flood-extent shapefiles
- Downloaded from HDX CKAN API via `download_philsa.py`
- Each shapefile contains flood-extent polygons from Sentinel-1-derived flood mapping
- **DBF fields are only `{fid, DN}`** (or `{DN, ImageDate, DataSource, Line, geometry}` for some regions)
  — date/region are NOT stored inside the shapefile itself
- Pipeline reads shapefiles locally with geopandas (no EE asset upload required)
- **All polygons are dissolved to a single (multi)polygon** — internal attribute distinctions are ignored

### 2.4 Basin Boundaries

- Local GeoJSON files in `data/basins/`
- Read with geopandas, reprojected to EPSG:4326, dissolved to single geometry
- Converted to `ee.Geometry` via GeoJSON (no EE asset upload needed)

---

## 3. Basin Configuration

### 3.1 Active Basins

Three basins are configured in `gee_config.BASINS`:

| Key | Name | Boundary File |
|---|---|---|
| `cagayan` | Cagayan River Basin | `data/basins/cagayan_bbox.geojson` |
| `pampanga` | Pampanga River Basin | `data/basins/pampanga.geojson` |
| `agno` | Agno River Basin | `data/basins/agno.geojson` |

> **Note**: Historical Agusan files (`data/basins/agusan.geojson`,
> `data/philsa_shapefiles/agusan_*`) remain on disk but are NOT referenced
> in `gee_config.BASINS` or `philsa_events.csv`. The third basin is Agno (Luzon),
> not Agusan (Mindanao). See `HANDOFF.md` for the correction history.

### 3.2 CRS / UTM Selection

- **UTM zone is computed from the basin centroid** using `pyproj`
- Philippines typically falls in UTM 50N (EPSG:32650) or UTM 51N (EPSG:32651)
- Formula: `zone = floor((lon + 180) / 6) + 1`
- Hemisphere: 326xx (north), 327xx (south)

---

## 4. Flood Event Selection

### 4.1 Event Index

Events are defined in `data/philsa_events.csv`:

```csv
event_id,basin,philsa_shapefile_path,flood_date
cagayan_2024-10-27,cagayan,data/philsa_shapefiles/cagayan_2024-10-27.shp,2024-10-27
pampanga_2024-07-23,pampanga,data/philsa_shapefiles/pampanga_2024-07-23.shp,2024-07-23
agno_2023-09-03,agno,data/philsa_shapefiles/agno_2023-09-03.shp,2023-09-03
```

### 4.2 PhilSA Event Handling

1. Load events from CSV (`philsa.load_events()`)
2. Validate that each event's `basin` column matches a key in `gee_config.BASINS`
3. Verify shapefile exists, has features, and has a CRS defined

### 4.3 Work Region Computation

The work region is computed **entirely client-side** (`philsa.compute_work_region_local()`) to avoid
EE's 10 MB payload limit on large flood shapefiles (e.g. Pampanga: 34,000+ polygons):

1. Read flood shapefile → reproject to EPSG:4326
2. **Pre-clip flood polygons to basin geometry** (spatial filter, reduces vertex count)
3. Dissolve clipped polygons → single geometry
4. **Simplify dissolved geometry at 50 m tolerance** (in UTM meters, via pyproj) → mask FeatureCollection
5. **Per-polygon simplify at ~500 m tolerance** (hidden by buffer)
6. **Buffer each polygon by 2,000 m** → merge all buffered polygons
7. **Intersect with basin boundary** → final working region
8. Final simplify at 200 m tolerance → `ee.Geometry`

The **mask FeatureCollection** (step 4) and the **working region** (step 8) are separate outputs — the
mask is used for rasterization, the working region for S1/MERIT clipping and patch-grid generation.

---

## 5. Sentinel-1 Processing

### 5.1 Pre-flood Scene Selection

- **Strategy**: Latest valid scene **strictly BEFORE** `(flood_date - min_gap_days)`
- **Search window**: 24 days back from `(flood_date - 1 day)` → looks at `(flood_date - 25)` to `(flood_date - 2)`
- **Rationale**: Keeps land-cover conditions close to the post-event state, so the pre/post difference
  is attributable to the flood. The minimum gap avoids catching early-arriving rain signal.
- **Sort**: Descending by `system:time_start` (latest first)

### 5.2 Post-flood Scene Selection

- **Strategy**: Earliest scene **at-or-after** `flood_date`
- **Search window**: 14 days forward from `flood_date`
- **Rationale**: Minimizes floodwater recession before observation
- **Sort**: Ascending by `system:time_start` (earliest first)

### 5.3 Polarization

- VV + VH dual-pol, IW mode
- Bands renamed: `VV` → `VV_pre`/`VV_post`, `VH` → `VH_pre`/`VH_post`

### 5.4 Orbit/Pass Matching

**Global rule** (`S1_REQUIRE_MATCHING_ORBIT_PASS = True`):
Pre and post scenes must share the same orbit pass (ASCENDING or DESCENDING) so incidence-angle
geometry doesn't confound the temporal-difference signal.

**Per-basin overrides** (`S1_MATCH_ORBIT_PASS_BY_BASIN`):

| Basin | Pass Matching | Rationale |
|---|---|---|
| `pampanga` | **Relaxed** (False) | ASC orbit-142 has no pre-flood scene; only DESC orbit-32 works |
| `agno` | **Relaxed** (False) | Coverage gaps on one pass around the event |

When relaxed:
1. Pick the pre scene first (unconstrained, latest before flood)
2. Pick the post scene **preferring the same pass** as pre; fall back to earliest scene on any pass

### 5.5 Temporal Constraints

| Parameter | Value |
|---|---|
| `PRE_FLOOD_SEARCH_WINDOW_DAYS` | 24 |
| `PRE_FLOOD_MIN_GAP_DAYS` | 1 |
| `POST_FLOOD_SEARCH_WINDOW_DAYS` | 14 |

### 5.6 Preprocessing / Filtering

1. **Speckle filter**: `focal_median` with 50 m radius (5 px at 10 m resolution)
2. **Clip to working region**
3. **Rename bands** with `_pre` / `_post` suffix

---

## 6. MERIT Hydro Processing

### 6.1 Bands

| Pipeline Name | MERIT Asset Band | Description |
|---|---|---|
| `hand` | `hnd` | Height Above Nearest Drainage |
| `elevation` | `elv` | Elevation, EGM96-adjusted |
| `flow_acc` | `upa` | Upstream drainage area (km²) |

### 6.2 Resampling

- **Native resolution**: ~90 m (3 arc-seconds)
- **Resampled to 10 m** using **bilinear** interpolation
- `.resample("bilinear")` is called on the source before any reduceRegions/export
- EE applies the reprojection lazily at export time
- Only continuous hydrographic fields are bilinear-resampled; SAR/mask remain at their native grid

---

## 7. PhilSA Flood Mask Processing

### 7.1 Polygon Loading

1. Read shapefile with geopandas
2. Drop empty/null geometries
3. Require explicit CRS (no silent WGS84 assumption)
4. Reproject to EPSG:4326

### 7.2 Basin Clipping

- Pre-clip flood polygons to basin geometry using spatial intersection
- Reduces vertex count from millions to thousands (avoids EE payload limits)

### 7.3 Rasterization

```python
ee.Image(0).byte().paint(flood_fc, 1).rename("flood_mask").clip(region)
```

- Background = 0 (non-flood)
- Painted areas = 1 (flood)
- Pixels with no Sentinel-1 coverage remain **masked** (not 0)
- This preserves the distinction between "valid dry pixel" (0) and "no data" (masked) in QC

---

## 8. Spatial Alignment

### 8.1 Projection

- All data is exported in the local UTM projection computed from the basin centroid
- Cagayan: EPSG:32651 (UTM 51N)
- Pampanga: EPSG:32651 (UTM 51N)
- Agno: EPSG:32650 (UTM 50N)

### 8.2 Resolution

- **Target**: 10 m per pixel
- Sentinel-1 is already near-native 10 m
- MERIT Hydro is resampled from ~90 m to 10 m via bilinear

### 8.3 Grid Alignment

All bands are stacked into a **single `ee.Image`** before export. Earth Engine reprojects every band
onto the common grid at export time, so bands are **pixel-aligned by construction**. There is no
per-modality resampling step that could drift.

---

## 9. Patch Generation

### 9.1 Target Patch Size

| Parameter | Value |
|---|---|
| `PATCH_SIZE_PX` | 256 |
| `TARGET_RESOLUTION_M` | 10 |
| `PATCH_SIZE_M` | 2,560 m |
| `PATCH_STRIDE_PX` | 256 (non-overlapping) |

### 9.2 Patch Footprint

Each patch covers a 2,560 m × 2,560 m area in the local UTM projection.

### 9.3 Grid Generation

1. Compute WGS84 bounding box of the working region
2. Project corners to UTM meters using **pyproj** (client-side, avoids EE CRS round-trip bugs)
3. Compute `n_cols` and `n_rows` from the projected extent
4. Build every rectangle in UTM meters with `proj=epsg`
5. **Filter patches**: Keep only those that intersect the (irregular) working region
   - Uses a single server-side `map` with `.intersects()` (not per-patch round trips)

### 9.4 Edge Behavior

- Patch grid is built over the bounding box, not the region itself
- Patches that don't intersect the region are dropped
- Patches at the region boundary may have partial valid pixels (handled by QC)

### 9.5 Patch ID Format

```
{event_id}_r{row:03d}_c{col:03d}
```

Example: `cagayan_2024-10-27_r007_c019`

---

## 10. Patch Quality Control

### 10.1 Valid-Pixel Calculation

A pixel is "valid" if it is present across **ALL** output bands (the strictest measure):

```python
all_bands_valid = stack.select(OUTPUT_BANDS).mask().reduce(ee.Reducer.min())
```

The union of band masks determines validity — a patch is rejected if ANY band has missing pixels.

**Valid-pixel percentage** = mean of `all_bands_valid` across all pixels in the patch, expressed as 0–100%.

### 10.2 Flood-Coverage Calculation

**Flood-pixel percentage** = mean of the binary flood mask (0/1) across all pixels, expressed as 0–100%.

### 10.3 Flood-Coverage Buckets

Patches are classified into one of five buckets based on their flood-coverage fraction (0–1):

| Bucket | Range | Purpose |
|---|---|---|
| 1 | [0.00, 0.01) | Dry / non-flood (hard negatives) |
| 2 | [0.01, 0.10) | Minimal flood |
| 3 | [0.10, 0.30) | Low flood |
| 4 | [0.30, 0.60) | Moderate flood |
| 5 | [0.60, 1.00) | High flood |

### 10.4 Patch Acceptance / Rejection

A patch is **rejected** if ANY of:

1. `valid_pixel_percentage < MIN_VALID_PIXEL_PERCENT` (configured: 1.0%)
2. `valid_pixel_percentage < (1 - MAX_NODATA_FRACTION) * 100` → must be ≥ 98.0%
3. `flood_pixel_percentage < MIN_FLOOD_PIXEL_PERCENT` (configured: 0.0 — effectively disabled)

### 10.5 Maximum Patches per Bucket per Event

After the valid-pixel gate, patches within each bucket are:
1. **Sorted by distance to the bucket's median flood fraction** (deterministic, not arbitrary)
2. **Capped at `MAX_PATCHES_PER_BUCKET_PER_EVENT = 40`**

This means a single event produces at most **5 buckets × 40 patches = 200 patches**.

### 10.6 Dry Patch Retention

Dry / non-flood patches (bucket [0.00, 0.01)) are **deliberately kept** — the binary segmentation
model needs hard negatives (rice paddies, roads, buildings, vegetation, bare soil, permanent water,
urban areas).

---

## 11. GeoTIFF Export

### 11.1 Band Ordering (8 bands)

Verified from `gee_config.OUTPUT_BANDS + [LABEL_BAND]` and `patching.export_patch()`:

| Band # | Name | Category |
|---|---|---|
| 1 | `VV_pre` | Model input |
| 2 | `VH_pre` | Model input |
| 3 | `VV_post` | Model input |
| 4 | `VH_post` | Model input |
| 5 | `hand` | Auxiliary band |
| 6 | `elevation` | Model input |
| 7 | `flow_acc` | Model input |
| 8 | `flood_mask` | Reference label |

**Model input bands** (6):
VV_pre, VH_pre, VV_post, VH_post, elevation, flow_acc

**Auxiliary post-processing band** (1):
HAND — Height Above Nearest Drainage; exported for downstream HAND-based
post-processing / refinement, not used as a model input channel.

**Reference label** (1):
flood_mask — binary PhilSA-derived mask (0 = non-flood, 1 = flood)

### 11.2 Data Type

All bands are cast to **float32** before export:

```python
float_stack = stack.select(OUTPUT_BANDS).toFloat()
label = mask.rename(LABEL_BAND).toFloat()
combined = float_stack.addBands(label)
```

EE rejects exports with mixed dtypes. The binary label is preserved as 0.0/1.0.

### 11.3 CRS and Resolution

- **CRS**: Local UTM (e.g. EPSG:32651)
- **Scale**: 10 m
- **Format**: GeoTIFF

### 11.4 Dimensions — The 256 vs 257 Issue

The pipeline is configured for **256 × 256 px** patches. However, **exported GeoTIFFs may be 257 × 257 px**.

**Root cause**: When a patch rectangle defined in UTM meters doesn't land on exact pixel edges at 10 m
resolution, Earth Engine's `toDrive` export pads the raster by **1 pixel on every side** to fully cover
the requested region. The result is a 257×257 delivered GeoTIFF.

**This is expected and documented in the README**. All bands are stacked into one image and exported in
the same grid, so they remain pixel-aligned. Do NOT crop to 256 per-band separately (that would
reintroduce drift). Read the full 257×257 and rely on the GeoTIFF's own geotransform.

### 11.5 Nodata Behavior

- Pixels outside the working region are masked (no data)
- The flood mask uses `ee.Image(0).paint(fc, 1)`, so background = 0 (non-flood), not "no data"
- Pixels with no Sentinel-1 coverage are masked in the SAR bands → flagged as invalid in QC

### 11.6 Export Destination

- **Google Drive** (default): folder `flood_seg_dataset`
- **GCS** (alternative): bucket configured via `GCS_BUCKET`
- Task is started asynchronously; completion monitored via Earth Engine task list

---

## 12. Metadata

### 12.1 Per-Patch Metadata (Master CSV)

Written to `data/patch_metadata.csv`, one row per exported patch:

```
event_id, basin, patch_id,
flood_date, pre_s1_date, post_s1_date, pre_post_days,
orbit_pass, relative_orbit, pre_relative_orbit, post_relative_orbit,
pre_absolute_orbit, post_absolute_orbit, mission, orbit_pass_matched,
epsg, min_lat, max_lat, min_lon, max_lon,
flood_pixel_percentage, valid_pixel_percentage, nodata_fraction,
row, col, export_task_id,
pre_selection_reason, post_selection_reason
```

### 12.2 Per-Event Metadata (JSON)

Written to `dataset/<event_id>/metadata.json` — the **reproducibility ledger** capturing:

- Exact Sentinel-1 scenes chosen (dates, orbit pass, relative/absolute orbit, mission)
- Selection reasons for pre and post scenes
- All processing parameters (windows, orbits, speckle, MERIT mapping/resample, grid, QC thresholds)
- Band list and output layout
- Number of exported patches

### 12.3 Export Task IDs

Each patch's EE batch task ID is recorded in both the master CSV and the report. Tasks can be
monitored at `https://code.earthengine.google.com/tasks`.

### 12.4 Validation Reports

- `data/validation_report.json` — cumulative per-event validation reports (JSON)
- `data/validation_report_brief.txt` — one-line summary per event (TSV)

---

## 13. Output Directory Structure

```
dataset/
  <event_id>/
    metadata.json              # full selection ledger
    patches/
      <event_id>_r000_c000.tif
      <event_id>_r000_c001.tif
      ...
  preview/                     # only from --validate-one runs
    <event_id>/
      <event_id>_r000_c000.tif
      ...
data/
  philsa_events.csv            # event index (input)
  philsa_shapefiles/           # PhilSA flood-extent shapefiles (input)
    <basin>_<date>.shp
    ...
  basins/                      # basin boundary files
    cagayan_bbox.geojson
    pampanga.geojson
    agno.geojson
  patch_metadata.csv           # master CSV, one row per patch
  validation_report.json       # cumulative validation reports
  validation_report_brief.txt  # one-line summaries
work/                          # working directory
```

---

## 14. Validation Workflow

The pipeline is **validation-first**: you run `--validate-one` on a single event, inspect the
report + preview, and only then run batch.

### 14.1 Validation Steps (`--validate-one`)

1. **Prerequisites**: shapefile exists, is readable, has CRS + features; basin known; date valid
2. **Work region**: flood polygon → basin intersection → buffer → region
3. **Sentinel-1**: pre/post scene selection, orbit matching, availability check
4. **MERIT Hydro**: HAND, elevation, flow_acc availability
5. **Alignment**: verify projections, resolution, spatial consistency
6. **Patch grid**: generate grid, count candidate patches
7. **Patch stats**: valid-pixel %, flood-pixel %
8. **QC filter + stratification**: select surviving patches
9. **Preview export**: up to 4 patches exported for visual inspection

### 14.2 Validation Output

- `data/validation_report.json` — full per-event report with check-by-check detail
- `data/validation_report_brief.txt` — one-line summary per event
- `dataset/preview/<event_id>/` — up to 4 preview GeoTIFFs

### 14.3 Validation Commands

```bash
# Validate one event (report + preview)
python main.py --validate-one --event cagayan_2024-10-27

# Dry-run validation (report only, no export)
python main.py --dry-run --validate-one --event cagayan_2024-10-27

# Export a single event (batch mode)
python main.py --event cagayan_2024-10-27

# Batch: all events
python main.py
```

---

## 15. End-to-End Execution Flow

```
1. Load event index (philsa_events.csv)
       │
2. For each event:
       │
       ├─ Validate prerequisites (shapefile, basin, date)
       │
       ├─ Compute work region (client-side)
       │    ├─ Read flood shapefile
       │    ├─ Read basin boundary
       │    ├─ Pre-clip flood to basin
       │    ├─ Dissolve + simplify → mask FeatureCollection
       │    └─ Buffer + intersect → work region
       │
       ├─ Rasterize flood mask
       │
       ├─ Find Sentinel-1 pre/post scenes
       │    ├─ Apply orbit-pass matching (strict or relaxed)
       │    ├─ Speckle filter (focal_median, 50 m radius)
       │    ├─ Clip to region
       │    └─ Rename bands (VV_pre, VH_pre, VV_post, VH_post)
       │
       ├─ Fetch MERIT Hydro stack
       │    ├─ Select bands (hnd, elv, upa)
       │    ├─ Bilinear resample (90 m → 10 m)
       │    └─ Clip to region
       │
       ├─ Stack all bands into single image
       │
       ├─ Generate patch grid (UTM-based, non-overlapping)
       │
       ├─ Compute per-patch statistics (single reduceRegions call)
       │    ├─ valid_pixel_percentage
       │    └─ flood_pixel_percentage
       │
       ├─ Filter + stratify patches
       │    ├─ Valid-pixel gate
       │    ├─ No-data fraction gate
       │    └─ Bucket-based capping (40 per bucket per event)
       │
       ├─ Export each selected patch as multi-band GeoTIFF
       │    └─ Record task ID + metadata
       │
       └─ Write per-event metadata.json
```

---

## 16. Known Limitations / Important Implementation Details

### 16.1 257×257 Export Size

As noted in §11.4, Earth Engine may add a 1-pixel border when the patch rectangle doesn't align to
exact pixel edges. Configured as 256×256 but delivered as 257×257. This is by-design and should not
be "fixed" by cropping.

### 16.2 Alignment Check Reports Inconsistent Projections

The `--validate-one` alignment check samples `.projection().getInfo()` on each band before export.
Because the bands come from different sources (S1 native, MERIT reprojected, mask painted), their
pre-export projections may differ (e.g. SAR = "unknown", hydro = EPSG:4326, mask = EPSG:4326).
**This is expected** — all bands are reprojected to the target UTM grid at export time.
The validation report's `projections_consistent: false` is informational, not a failure.

### 16.3 Pampanga Shapefile Size

The Pampanga PhilSA shapefile has **34,005 polygons** covering a nationwide bounding box.
Without the client-side pre-clip optimization, the dissolved geometry would exceed EE's 10 MB
request payload limit. The `compute_work_region_local()` function addresses this by clipping
to the basin boundary before dissolve/simplify.

### 16.4 Cagayan Post-Flood Window

The post-flood window was widened from 5 to **14 days** because only S1A was operational in
October 2024 (S1B failed Dec 2021, S1C not yet operational). S1A's repeat cycle for orbit 32
(covering Cagayan) is 12 days, so the next available post-flood scene was 2024-11-07 — 11 days
after the event.

### 16.5 Extraction Does Not Perform Modeling Operations

This pipeline is an **extraction pipeline only**. It does NOT perform:

- Train / validation / test splitting
- Normalization or standardization
- Data augmentation
- HAND threshold selection
- Model training or evaluation

Normalization statistics must be computed from **training data only**. HAND-based threshold selection
must be performed **without knowledge of the held-out basin**. Augmentation must be applied
**consistently to all 8 aligned bands** within each patch (the pipeline exports all bands
pixel-aligned in a single GeoTIFF, so any augmentation that reads the full file applies to all
bands identically).

### 16.6 Splitting and Data Leakage

The pipeline does **not** define any train/val/test splits. Downstream splitting must be done
over `patch_metadata.csv` and should follow these rules to avoid data leakage:

**Basin/event-aware splitting (required):**
Use leave-one-basin-out (e.g. Train=A+B, Test=C) or leave-one-event-out. The metadata provides
the `basin` and `event_id` columns for this purpose.

**Spatial leakage (neighboring patches):**
Patches from the same event at adjacent grid positions share temporal context (same pre/post S1
scenes, same MERIT Hydro snapshot, same flood mask). A naive random split across patches from the
same event leaks temporal and spatial information into the test set. Group patches by `event_id`
when splitting, or at minimum use `row`/`col` to enforce spatial separation between splits.

**Metadata columns available for safe splitting:**

| Column | Purpose |
|---|---|
| `basin` | Basin identity — for leave-one-basin-out |
| `event_id` | Flood event identity — for leave-one-event-out |
| `row`, `col` | Grid position — for spatial separation |
| `min_lat`, `max_lat`, `min_lon`, `max_lon` | Bounding box — for distance-based separation |
| `flood_pixel_percentage`, `valid_pixel_percentage` | Patch statistics — for stratified sampling |
| `pre_s1_date`, `post_s1_date`, `orbit_pass` | Acquisition info — for temporal/orbit grouping |

### 16.7 Sentinel-1 Values Are Unmodified

`COPERNICUS/S1_GRD` is already calibrated sigma-0 in dB. No manual dB conversion is applied,
matching the thesis methodology.

### 16.8 Speckle Filter: focal_median Only

`refined_lee` is declared in the config validator but **not implemented** in `sentinel1.py`.
Setting `SPECKLE_FILTER = "refined_lee"` raises `NotImplementedError`.

### 16.9 No ΔVV/ΔVH Change Bands

The pipeline computes pre/post SAR images but does not compute difference bands by default.
These are a potential ablation for future work.

### 16.10 Drive Download Is Separate

The EE export only **queues** batch tasks. Actual `.tif` files land on Google Drive and must be
downloaded separately via `scripts/download_drive_patches.py`. The Drive downloader uses raw
HTTP against the Google Drive v3 API, authenticated with the same EE refresh token.

---

## Appendix: Module Dependency Map

```
main.py
  ├── gee_config.py          (configuration, validation)
  ├── philsa.py              (event loading, work region, mask rasterization)
  ├── sentinel1.py           (S1 scene selection, preprocessing)
  ├── merit_hydro.py         (MERIT Hydro retrieval)
  ├── patching.py            (patch grid, stats, QC, export)
  ├── alignment.py           (alignment verification)
  ├── quality_control.py     (prerequisite checks, failure classification)
  ├── metadata.py            (CSV + JSON metadata logging)
  └── exports.py             (export orchestration)

validate.py (standalone single-event validation)
  └── same modules as main.py

download_philsa.py (standalone PhilSA acquisition)
  └── HDX CKAN API (no EE dependency)

scripts/download_drive_patches.py (standalone Drive download)
  └── Google Drive v3 API + EE task list

scripts/check_s1_availability.py (standalone S1 diagnostic)
  └── Earth Engine API
```
