# HANDOFF — next session start here

**Date:** 2026-09-09
**Prior session ended:** mid-way through multi-basin expansion. Cagayan milestone is
committed and solid. Pampanga validation is **blocked on a GEE payload limit**; a fix
is written but **not yet verified/working**. Continue from here.

---

## Where we are (two layers)

### 1. Cagayan milestone — DONE, committed, verified ✅
The pipeline runs end-to-end on one real event and is committed:

- `c1d19c4` — fix patch grid (zero-patch collapse) + mixed-dtype export → 112 patches, 8-band float32 10 m UTM tiles
- `d84da48` — add Drive patch downloader (`scripts/download_drive_patches.py`)
- Preview tiles **visually confirmed aligned in QGIS** by the user → Cagayan event is trustworthy.
- Working tree was clean after these two commits.

### 2. Multi-basin expansion — IN PROGRESS (uncommitted, blocked on Pampanga)
Target (user-confirmed): **3 events** — `cagayan_2024-10-27` (keep) + `pampanga_2024-07-23` + `agusan_2023-01-03`. Data sourced from **HDX** (PhilSA) + **HydroSHEDS** (basins). No fabricated data.

---

## Current git state (uncommitted)

**Modified (9):** `.gitignore`, `data/philsa_events.csv`, `data/validation_report.json`,
`data/validation_report_brief.txt`, `download_philsa.py`, `gee_config.py`, `main.py`,
`philsa.py`, `validate.py`

**Untracked (13):** `data/basins/pampanga.geojson`, `data/basins/agusan.geojson`, the 8 sidecar
files for `data/philsa_shapefiles/pampanga_2024-07-23.*` + `agusan_2023-01-03.*`, and
`data/hydrosheds/` (gitignored, not listed). `HANDOFF.md` itself is also untracked.

Nothing from the multi-basin work is committed yet — all of the above is the working tree.
The `.claude/plans/ancient-wibbling-gadget.md` file holds the full approved multi-basin plan.

---

## What's done this session (verified working)

1. **Raw data fetched (no GEE):**
   - HydroBASINS `hybas_au_lev12_v1c` downloaded + extracted to `data/hydrosheds/` (gitignored; 95 MB shp).
   - PhilSA shapefiles staged via the extended downloader: `pampanga_2024-07-23.shp` (34,005 feats,
     EPSG:4326), `agusan_2023-01-03.shp` (22,142 feats, EPSG:4326). Both are **nationwide** packages
     (flood extent covers all of Luzon/Mindanao; the basin boundary defines the clip).

2. **`scripts/build_hydrosheds_basin.py` (NEW)** — builds basin geojson from HydroBASINS au lev12
   via `NEXT_DOWN` upstream closure + dissolve. **Regression-validated:** `--hybas-id 5120030230`
   reproduces the tracked `cagayan.geojson` with **IoU = 1.0**.
   - `pampanga.geojson` (outlet HYBAS_ID 5120029400): 9,458 km², **381 km² of flood inside**, 4.03% basin flooded.
   - `agusan.geojson` (outlet HYBAS_ID 5120117760): 11,759 km², **472 km² of flood inside**, 4.01% basin flooded.
   - Note: the `--verify-shp` coverage check computes fraction-of-flood-inside-basin (nationwide packages
     can't be "fully contained"); it reports low % for nationwide floods by design — verify per-basin in
     projected CRS instead (step was done manually, see "flood coverage" numbers above).

3. **`download_philsa.py` extended (backward compatible):** new `--no-region` flag → `region=None` picks the
   single S1 SHP directly (Pampanga/Agusan have no region token in the filename). `--region cagayan-isabela`
   path unchanged. Verified for both new events.

4. **Config + events wired:** `gee_config.BASINS` now has `cagayan` (bbox), `pampanga`, `agusan` (full basins);
   `data/philsa_events.csv` has all 3 rows. `gee_config.ensure_ready()` passes; `philsa.load_events()` returns
   3 events with no ValueError; all 3 basins read + dissolve cleanly.

---

## THE BLOCKER — Pampanga validation fails (task #9 in-progress)

`python main.py --validate-one --event pampanga_2024-07-23` → **failed** `quality_control_failed`, 0 patches.
Root cause (from `data/validation_report.json`): the S1 step raised
**`Request payload size exceeds the limit: 10485760 bytes`** (GEE's 10 MB limit).

Why: the dissolved Pampanga flood polygon has **~2,000,000 vertices across 52,241 parts** (vs Cagayan's
small geometry). `filterBounds(region)` / `.clip(region)` with that dense geometry blew the payload.

### The fix written this session (NOT yet verified)
Added `philsa.compute_work_region_local(flood_path, basin_key)` → `(flood_fc, work_region)`:
- Computes the work region **client-side** (geopandas): read flood + basin → simplify per-polygon → UTM
  buffer(2000) → intersect basin → simplify → `ee.Geometry`.
- Full-resolution `flood_fc` is still built (for mask painting); only the *region* is simplified.
- `main.py:60` and `validate.py:90` now call it.

**Current status of the fix: UNVERIFIED and likely still too slow.** The helper is written but a direct
test timed out (>5–10 min) on Pampanga. The suspected bottlenecks (not yet profiled):
1. `flood_gdf.dissolve()` for the full-res FC — dense 2M-vertex union is very slow.
2. Per-polygon `shapely_transform` + `buffer` over 34k polygons — O(n) with per-polygon overhead, still slow.

**Do NOT run `--validate-one` for Pampanga again until the helper is confirmed fast.** A profiler run was
about to be launched (stage-by-stage timing of read_file / clean+reproject / simplify / dissolve) when the
session handed off. That profiling is the immediate next task.

### Suggested next steps (in order)
1. **Profile `compute_work_region_local` stage-by-stage** on Pampanga:
   ```
   python -c "...time read_file, clean+reproject, per-polygon simplify, dissolve, per-polygon buffer..."
   ```
   to find the actual bottleneck and pick the fix below.
2. **Speed it up.** Likely options (pick after profiling):
   - Avoid the full-res dissolve: build `flood_fc` from the shapefile without a client-side dissolve
     (e.g. pass the shapefile's GeoJSON to EE and let EE dissolve server-side, OR keep the php-fc as-is
     but simplify hard for the region).
   - Simplify in WGS84 first (cheap degrees-tolerance), buffer in UTM only on the ALREADY-COMPACT set.
   - Pre-clip flood polys to the basin extent before buffering (most of the nationwide extent is wasted work
     outside the basin; clip to basin bbox first).
   - Consider `shapely.ops.transform` batch vs vectorized reprojection.
   - For the FC, use the already-dissolved-but-only-simplified geometry (mask still full res via EE `.paint`).
3. **Re-run** `python main.py --validate-one --event pampanga_2024-07-23` and confirm `status: valid`, `selected>0`,
   correct `pre_s1_date`/`post_s1_date`. **Watch the post-flood window** — `POST_FLOOD_SEARCH_WINDOW_DAYS=14`
   was tuned for Cagayan's orbit-32; Pampanga is on a different relative orbit. If `missing_post_s1`, widen via a
   per-basin `POST_WINDOW_BY_BASIN` map in `gee_config.py` + consult in `sentinel1.py`.
4. **Human check:** download 4 preview tiles (`python scripts/download_drive_patches.py --wait --event pampanga_2024-07-23`),
   open in QGIS, confirm SAR↔hydro↔mask aligned + mask overlaps SAR flood signal (same gate as Cagayan).
5. **Export:** `python main.py --event pampanga_2024-07-23`, pull tiles, verify `patch_metadata.csv`.
6. **Agusan** (task #10): same sequence for `agusan_2023-01-03`. NOTE Agusan's shapefile has invalid ring
   winding — pyogrio auto-corrects but GEE may reject; if flaky, run the buffer(0)/make_valid repair on the
   geometries (the builder already buffers(0) in its verify path).
7. **Commit** the multi-basin work in logical commits (data fetch + builder + downloader ext + config/events
   + the work-region fix). Do NOT commit `data/hydrosheds/` (gitignored) — only the generated geojsons + code + CSV.
8. **Optional later:** run bare `python main.py` batch (re-exports all; doubles GEE quota — only after every
   event is individually validated+exported).

---

## Key files / where things are

| Path | Role |
|---|---|
| `gee_config.py` | `BASINS` (3 basins), `POST_FLOOD_SEARCH_WINDOW_DAYS=14`, `EE_PROJECT="flood-thesis-507015"`, `DRIVE_FOLDER="flood_seg_dataset"` |
| `philsa.py` | `compute_work_region_local` (the fix, at line 154), `load_events`, `shapefile_to_ee_featurecollection`, `rasterize_flood_mask` |
| `main.py` / `validate.py` | callers of `compute_work_region_local` (lines 60 / 90) |
| `download_philsa.py` | HDX fetch, now with `--no-region` |
| `scripts/build_hydrosheds_basin.py` | NEW basin builder (upstream closure + dissolve) |
| `scripts/download_drive_patches.py` | pulls Drive tiles → `dataset/<event>/patches/` (committed) |
| `data/philsa_events.csv` | 3 events |
| `data/validation_report.json` / `brief.txt` | last per-event validation (cagayan×2 valid, pampanga failed) |
| `.claude/plans/ancient-wibbling-gadget.md` | the approved multi-basin plan |

---

## Repo design invariants (don't break)
- **No fabricated data** — every event/boundary must come from a real source. `gee_config.ensure_ready()` + `philsa.load_events()` fail loudly on placeholders/unknown basins.
- **Basin is a clip.** `work_region ≈ flood.buffer(2000) ∩ basin`. Basin must contain the flood or the mask silently truncates. Use full hydro basins for new events (not tight bboxes).
- **One event at a time** for GEE quota. Previews are a subset of selected patches, land in the same Drive folder + `dataset/<event>/patches/` (`PREVIEW_DIR` config is dead).
- **`event_id` / shapefile basename / BASINS key must all match** the `basin_YYYY-MM-DD` convention.
- Sentinel-1 is used as provided (calibrated σ⁰ dB); DON'T convert.
- Cross-basin integrity: extraction never mixes events; splits happen downstream on `patch_metadata.csv`.

---

## Useful commands
```bash
# local pre-flight (no GEE)
python -c "import gee_config; gee_config.ensure_ready(); print('ok')"
python -c "import philsa; print([e.event_id for e in philsa.load_events()])"

# validate one event (GEE, a few min)
python main.py --validate-one --event pampanga_2024-07-23

# pull preview tiles from Drive + human QGIS check
python scripts/download_drive_patches.py --wait --event pampanga_2024-07-23
# → dataset/pampanga_2024-07-23/patches/*.tif

# rebuild a basin from HydroSHEDS (regression-safe)
python scripts/build_hydrosheds_basin.py --hybas-id 5120030230 --name cagayan_test  # compare vs cagayan.geojson

# fetch a PhilSA shapefile
python download_philsa.py --date 2023-01-03 --basin agusan --no-region
```