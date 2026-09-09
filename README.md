# Philippine Multi-Basin Flood Segmentation — GEE Extraction Pipeline

Implements the pipeline described in the research notes: PhilSA flood masks
+ pre/post Sentinel-1 SAR (VV/VH) + MERIT Hydro (HAND, elevation, flow
accumulation) → aligned 256×256 patches for a Siamese Multi-Encoder U-Net.

**Source of truth: Google Earth Engine.**
* Sentinel-1: `COPERNICUS/S1_GRD` (C-band GRD, IW mode, VV+VH)
* MERIT Hydro: `MERIT/Hydro/v1_0_1` (HAND, elevation, flow accumulation)
* PhilSA flood labels: your local shapefiles, rasterized to binary masks

---

## Important: read this before you run anything

1. This pipeline **does not run until you supply real data** — its config
   deliberately has **no placeholder values**. It will refuse to start with
   the error *"gee_config is not ready to run"* listing what's missing.
2. The pipeline is **validation-first**: you run `--validate-one` on a single
   event, inspect the report + preview, and only then run batch. This is by
   design, per the thesis notes ("Do NOT immediately process the entire
   dataset").
3. **No data is fabricated.** All field names, dates, asset IDs, and paths are
   read from your provided inputs. The pipeline *reports* the actual
   shapefile field names it finds (via `philsa.inspect_shapefile`) so you can
   verify against your real PhilSA data.

---

## Setup (one-time)

### 1. Python environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Earth Engine authentication

```bash
earthengine authenticate   # opens a browser OAuth flow, once
```

You also need a Google Cloud project with the **Earth Engine API** enabled.
Put its id in `gee_config.py` under `EE_PROJECT`.

### 3. Supply basin boundaries (`gee_config.BASINS`)

Each basin needs a boundary polygon. Two options, both supported — use
whichever you have:

**Option A — local file (recommended).** Put a shapefile/GeoJSON boundary in
`data/basins/` and reference it in `gee_config.BASINS` with `local_path`:

```python
BASINS = {
    "cagayan":  {"local_path": "data/basins/cagayan.geojson"},
    "pampanga": {"local_path": "data/basins/pampanga.geojson"},
    "agno":     {"local_path": "data/basins/agno.geojson"},
}
```
The file is read locally with geopandas, reprojected to EPSG:4326, and pushed
to Earth Engine as an `ee.Geometry` — no EE asset upload needed for the
boundary. (`data/basins/cagayan.geojson` ships as an example, derived from
HydroSHEDS/HydroBASINS `hybas_au_lev12` main basin `5120030230`.)

**Option B — EE table asset.** Upload a boundary as an EE table asset
(Code Editor → Assets, or `earthengine upload table`) and use `asset_id`:

```python
BASINS = {"pampanga": {"asset_id": "projects/<your-project>/assets/pampanga_basin"}}
```

The `basin` column of your events CSV must match these keys. If you pass both
`local_path` and `asset_id`, `local_path` wins.

### 4. Put your PhilSA shapefiles on disk

Copy your flood-extent shapefiles somewhere the pipeline can read them,
e.g. `data/philsa_shapefiles/<event>.shp` (with sibling `.shx/.dbf/.prj`).

### 5. Build the event index

Copy `data/philsa_events.sample.csv` → `data/philsa_events.csv` and fill in
one row per flood event:

```csv
event_id,basin,philsa_shapefile_path,flood_date
pampanga_2024-07-24,pampanga,data/philsa_shapefiles/pampanga_2024-07-24.shp,2024-07-24
```

Same set of columns as the sample. **Every event needs a flood_date** —
the pipeline uses it to select pre/post S1 scenes. It does *not* guess dates.

---

## Run

```bash
# 1) ONE-EVENT VALIDATION — do this first, inspect everything
python main.py --validate-one --event pampanga_2024-07-24

# 2) dry-run a single event (compute patch counts, no export)
python main.py --dry-run --event pampanga_2024-07-24

# 3) real export of one event
python main.py --event pampanga_2024-07-24

# 4) batch — all events in the CSV
python main.py
```

`--validate-one` produces `data/validation_report.json` (full per-event
report) + `data/validation_report_brief.txt` (one line per event) + a small
preview export under `<OUTPUT_ROOT>/preview/<event_id>/` so you can visually
confirm SAR ↔ Hydro ↔ mask alignment before spending export quota.

See `data/validation_report.json` after a run for the check-by-check detail
(geometry, pre/post S1 dates, VV/VH availability, MERIT availability,
projection, resolution, alignment, rasterization, patch stats).

---

## What each exported patch contains

**One multi-band GeoTIFF per patch**, 257×257 px @ 10 m (note the border),
bands in this order:

```
VV_pre, VH_pre, VV_post, VH_post, hand, elevation, flow_acc, flood_mask
```

> **257 px export border:** the grid is configured as 256×256 px
> (`PATCH_SIZE_PX`, `PATCH_SIZE_M`), but Earth Engine's `toDrive` export
> appends a **1-px border on every side** because the patch rect doesn't land
> on exact pixel edges, so the delivered GeoTIFF is **257×257**. This is
> expected and harmless: all bands (SAR + MERIT + `flood_mask`) are stacked
> into ONE image and exported in the same 257×257 grid, so they stay
> pixel-aligned. **Do NOT crop to 256 per-band separately** (that would
> reintroduce drift); read the full 257×257 and rely on the tiff's own
> geotransform. Model training loaders must not hard-code a 256 window.

* First 7 are model **input** channels (`gee_config.OUTPUT_BANDS`).
* `flood_mask` is the PhilSA-derived binary **label** (1 = flood, 0 = not).
* Keeping them in **one file** guarantees every band is pixel-aligned — there
  is no separate per-modality resampling step that could drift.

Filename = `{event_id}_r{row:03d}_c{col:03d}.tif` (row/col come from the
UTM patch grid), so the file is self-describing about which patch it is.

## Output structure

```
dataset/
  <event_id>/
    metadata.json          # full selection ledger (scenes, dates, orbits, params, reasons)
    patches/
      <event_id>_r000_c000.tif
      ...
data/
  patch_metadata.csv       # master CSV, one row per exported patch  (see below)
  validation_report.json   # per-event validation reports
```

`data/patch_metadata.csv` fields (`metadata.FIELDNAMES`):

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

---

## Design decisions (why it behaves this way)

* **Pre-flood scene selection** = latest valid scene strictly *before* the
  event (within a configurable window, default 24 d; min gap 1 d), so
  land-cover conditions are as close as possible to the post-event state —
  this makes the pre/post *difference* attributable to the flood.
* **Post-flood scene selection** = earliest scene *at-or-after* the event
  (default window 5 d), to minimize floodwater recession before observation.
* **Orbit pass matching** (`S1_REQUIRE_MATCHING_ORBIT_PASS`, default True):
  pre/post scenes share the same ascending/descending pass so incidence-angle
  geometry doesn't confound the temporal difference. Turn off only if it makes
  valid events unmatchable — check the resulting dates carefully.
* **Selection reasons are recorded** (`pre_selection_reason` /
  `post_selection_reason` columns, plus `metadata.json`) so you (and a
  reviewer) can audit *why* a scene was chosen — never a blind "nearest
  image".
* **Sentinel-1 values are used as provided by GEE.** `COPERNICUS/S1_GRD` is
  already calibrated σ⁰ in dB; no manual dB conversion is applied (matching
  the thesis notes: don't convert unless there's a specific technical reason).
* **MERIT Hydro** (native ~90 m) is resampled to the 10 m grid with
  **bilinear** interpolation for the continuous fields (avoids blocky
  artifacts). Add more MERIT variables by extending
  `gee_config.MERIT_BAND_MAP` — no other module changes.
* **Quality filtering**: a patch is dropped if any input band has more than
  `MAX_NODATA_FRACTION` missing pixels, or fewer than
  `MIN_VALID_PIXEL_PERCENT` valid pixels, or (optionally) below a minimum
  flood coverage.
* **Stratification**: patches are bucketed by flood-coverage fraction and each
  bucket is capped per event (`FLOOD_COVERAGE_BUCKETS`,
  `MAX_PATCHES_PER_BUCKET_PER_EVENT`), so an event dominated by dry background
  — or by flood — doesn't skew the dataset. **Dry/non-flood patches are
  deliberately kept** (bucket 0–0.01): the binary model needs hard negatives
  (rice paddies, roads, buildings, vegetation, bare soil, permanent water,
  urban areas).
* **Cross-basin integrity**: batch runs never mix events. At training time you
  split **by basin/event** using `patch_metadata.csv` (group by `basin`), which
  enables clean leave-one-basin-out experiments (Train=AB/Test=C, etc.) with no
  geographic/event leakage. Extraction itself does not split.

---

## Config reference (`gee_config.py`)

Everything tunable is here; nothing else hard-codes pipeline parameters.
Key groups:

| Group | Keys |
|---|---|
| EE / export | `EE_PROJECT`, `EXPORT_DESTINATION`, `DRIVE_FOLDER`, `GCS_BUCKET`, `GCS_PREFIX` |
| Basins | `BASINS` (dict of `label → {"asset_id": ...}`) |
| Events | `PHILSA_EVENTS_CSV` |
| Sentinel-1 | `S1_COLLECTION`, `S1_POLARIZATIONS`, `S1_INSTRUMENT_MODE`, `S1_REQUIRE_MATCHING_ORBIT_PASS`, `PRE_FLOOD_SEARCH_WINDOW_DAYS`, `PRE_FLOOD_MIN_GAP_DAYS`, `POST_FLOOD_SEARCH_WINDOW_DAYS`, `SPECKLE_FILTER`, `SPECKLE_KERNEL_RADIUS_M` |
| MERIT | `MERIT_HYDRO_IMAGE`, `MERIT_BAND_MAP`, `MERIT_RESAMPLE` |
| Patching | `TARGET_RESOLUTION_M`, `PATCH_SIZE_PX`, `PATCH_SIZE_M`, `PATCH_STRIDE_PX` |
| QC | `MAX_NODATA_FRACTION`, `MIN_VALID_PIXEL_PERCENT`, `MIN_FLOOD_PIXEL_PERCENT`, `FLOOD_COVERAGE_BUCKETS`, `MAX_PATCHES_PER_BUCKET_PER_EVENT` |
| Output | `OUTPUT_ROOT`, `METADATA_CSV_PATH`, `VALIDATION_REPORT_PATH`, `PREVIEW_DIR`, `VALIDATE_PREVIEW_PATCHES` |

`gee_config.validate()` / `ensure_ready()` fail loudly on placeholders or
invalid combinations before any EE work starts.

---

## Failure reporting

The pipeline **does not silently skip** failed events. `main.py` outputs a
per-event status, and `quality_control.classify_event_failure` buckets the
reason:

* `missing_pre_s1` — no valid pre-flood scene in window
* `missing_post_s1` — no valid post-flood scene in window
* `vv_vh_unavailable` — collection has neither/both pols missing
* `invalid_geometry` — shapefile empty / no CRS / dissolves to empty
* `merit_unavailable` — MERIT Hydro retrieve failed
* `rasterization_failed` — could not rasterize the mask
* `insufficient_valid_pixels` — too few valid pixels
* `insufficient_coverage` — too few selected patches for a valid event
* `quality_control_failed` — catch-all

`validate_one_event` writes a structured record with these, and `main.py`
prints a concise batch summary at the end.

---

## Reproducibility ledger

Every event writes `dataset/<event_id>/metadata.json` with:

* the exact Sentinel-1 scenes chosen (dates, orbit pass, relative/absolute
  orbit, mission) and the selection reasons,
* every processing parameter that affects output (windows, orbits, speckle,
  MERIT mapping/resample, grid, QC thresholds),
* the band list and output layout.

Combine that with `data/patch_metadata.csv` and the master CSVs and you can
fully reconstruct the dataset from inputs → decided scenes → exported patches.

## What this pipeline does *not* do

* No road/building/land-cover datasets are added as inputs (those surfaces are
  *represented* in patches, not explicitly labeled).
* No ΔVV/ΔVH change bands are computed by default (a cheap ablation in
  `sentinel1.py` if you ever want them).
* No dataset splitting logic — cross-basin splits are a downstream step over
  `patch_metadata.csv`.
* No frontend, database, API, auth, or deployment infrastructure.