# HANDOFF — next session start here

**Date:** 2026-09-09 (evening session, continued)
**Prior session:** Resolved Pampanga blockers. **This session:** PROJECT CORRECTION — the third thesis
basin is **AGNO RIVER BASIN, NOT AGUSAN**. Every prior "Agusan" reference is **OBSOLETE**. Agno basin
boundary has been built from HydroSHEDS and visually validated. Agusan data files remain on disk as
historical (not deleted), but the project no longer references Agusan anywhere.

---

## ⚠️ THE THREE STUDY BASINS (authoritative)

1. **Cagayan** River Basin
2. **Pampanga** River Basin
3. **Agno** River Basin — **NOT** Agusan

> Any prior mention of Agusan/Agno *as Agusan* belongs to an OLD/OUTDATED version. Agusan = Mindanao/CARAGA,
> wrong geography. Agno = Luzon, Lingayen Gulf outlet (~16.03N / 120.42E).

The intended cross-basin evaluation (folds):
```
Fold 1: Train = Cagayan + Pampanga   Test = Agno
Fold 2: Train = Cagayan + Agno       Test = Pampanga
Fold 3: Train = Pampanga + Agno      Test = Cagayan
```

---

## Where we are (two layers)

### 1. Cagayan milestone — DONE, committed, verified ✅ (unchanged)
Pipeline runs end-to-end on one real event, committed. Preview tiles **visually confirmed in QGIS by the
user** (4 tifs in `dataset/cagayan_2024-10-27/patches/`). Trustworthy.

### 2. Multi-basin expansion — IN PROGRESS (uncommitted, LIVE)
- **Cagayan:** validated, QGIS-inspected ✅
- **Pampanga:** validated (120 patches), QGIS-inspected ✅, ready for batch export
- **Agno (2023-09-03):** validated (73 patches), QGIS-inspected ✅, ready for batch export
- All three basins validated and QGIS-inspected. Ready for export.

---

## ✅ THIS SESSION: Agusan → Agno correction + Agno basin built

### Project correction
- Third basin is **Agno**, not Agusan. All config/docs/CSV updated (see git changes below).
- `data/basins/agusan.geojson`, `data/philsa_shapefiles/agusan_*.*` are **kept on disk as historical**
  but no longer referenced anywhere (user-approved: "Remove Agusan from config now, keep files").
- `data/philsa_events.csv` no longer has an agusan row. No Agno event row yet (basin first, events after).

### Agno basin boundary — BUILT & VISUALLY VALIDATED 🆕
`python scripts/build_hydrosheds_basin.py --hybas-id 5120029740 --name agno` → `data/basins/agno.geojson`
- 53 lev12 catchments traced upstream via `NEXT_DOWN` from the Agno outlet.
- **6,664 km²** — matches the real Agno River Basin scale. Outlet = `5120029740`, UP_AREA 6,664 km².
- **CRITICAL identification insight:** the approximate Agno mouth (16.03N/120.42E) lands inside a small
  coastal cell `5120029760` (only 287 km², NEXT_DOWN=0) — **that is NOT the Agno outlet**. The true outlet
  is the adjacent gulf cell `5120029740` with the 6,664 km² upstream accumulation. This is exactly the
  "don't trust nearest-centroid / NEXT_DOWN=0" trap — the nearest cell to the mouth drains a tiny
  neighboring catchment, not the Agno.
- Visual validation map: `work/agno_basin_map.png` (static) + `work/agno_basin_map.html` (interactive
  Leaflet, basin + sub-basins + outlet). Also `work/agno_subbasins.geojson` (53 cells).

### ✅ Agno basin INDEPENDENT VALIDATION — VERDICT: KEEP (2026-09-09)
Independent check (details in conversation + memory) — the 6,664 km² is **correct**, not an artifact:
- **HydroBASINS hierarchy:** lev12 (finest standard). Outlet cell `5120029740` SUB_AREA=`5.6` km², but
  UP_AREA=`6,664.2` (HydroBASINS' own upstream area — my traversal independently reproduces it).
- **Per-level (BFS) closure:** 53 cells across 19 upstream levels, running totals end at 6,664.5 km²;
  all cells share `MAIN_BAS=NEXT_SINK=5120029740` -> ONE coherent HydroBASINS main basin.
- **River network (HydroRIVERS AU, downloaded to `work/hydrorivers/`):** all 1,255 reaches inside the
  polygon belong to a SINGLE main river `50009028` (UPLAND_SKM=6,664, Strahler 6, discharge ~375 m³/s),
  mouth at (120.202/16.040) = the true Agno mouth. NOT a jumble of neighboring coastal rivers.
- **River identity:** Wikipedia Agno River — mouth 16°02′17″N 120°12′00″E (matches), main tributaries
  Pila/Camiling/Tarlac/Ambuyan, source Mt. Data (Benguet) = matches the polygon's extent into the high
  Cordillera. **Unambiguously the Agno.**
- **Authoritative area:** official Agno basin ≈ **5,952 km²** (Wikipedia/NWRB) to **7,232 km²** (NWRB
  major-river-basin index). HydroSHEDS 6,664 km² sits inside that range — a topographic-drainage
  definition ~12% above the NWRB figure. **The "~2,700 km²" is NOT the whole basin** — it is the
  upper/middle Agno sub-watershed above the Tarlac confluence (node `5120115560`, UP=2,765.7 km²).
- **Conclusion:** (a) correct HydroBASINS watershed ✓, (b) wrong-outlet over-delineation ✗, (c) official-
  definition difference ✓. **KEEP `data/basins/agno.geojson`.**

### Basin election notes for Agno events
- Do NOT infer the Agno basin from PhilSA flood polygons. Baseline basin is what we built.
- Per user: sit on the basin + map first; only afterwards test PhilSA events against the finalized basin.

---

## Current git state (uncommitted)

**Modified (8):** `data/validation_report.json`, `data/validation_report_brief.txt`, `gee_config.py`,
`main.py`, `philsa.py`, `sentinel1.py`, `validate.py`, `download_philsa.py` (comment) + this HANDOFF,
`README.md`, `data/philsa_events.csv`, `scripts/build_hydrosheds_basin.py` (docstring)

**Untracked:** `dataset/` (downloads/previews), `scripts/profile_work_region.py`, `data/basins/agno.geojson`,
`work/` (agno maps + sub-basins geojson)

**Config changes this session (Agusan removed → Agno):**
- `gee_config.py:46` `BASINS["agusan"]` → `BASINS["agno"]` → `data/basins/agno.geojson`
- `gee_config.py:92` `S1_MATCH_ORBIT_PASS_BY_BASIN`: dropped `"agusan"`. `pampanga: False` kept (verified).
  `agno` left unset (strict default) with a TODO — set it only if Agno's S1 census shows one-pass coverage risk.
- `data/philsa_events.csv`: agusan row removed; no agno row yet.

---

## Next steps (in order)

1. ✅ **Agno basin review — DONE (independent validation verdict: KEEP).** Basin `data/basins/agno.geojson`
   is a correct HydroSHEDS Agno watershed (6,664 km², single main river, true mouth). Proceed to events.
2. ✅ **Pampanga tile check — DONE (QGIS-inspected by user, 2026-09-09).** Valid — SAR↔hydro↔mask
   alignment confirmed. Pampanga is ready for batch export when you proceed.
3. ✅ **Agno 2023-09-03 — VALID (73 patches selected):**
   - Event validated via `python main.py --validate-one --event agno_2023-09-03` → **valid, 73 patches**
   - S1 pair: pre=2023-08-26 DESC orbit 105, post=2023-09-07 DESC orbit 105, same orbit/pass, 12-day gap
   - PhilSA: 3,435 polygons intersecting Agno, 169.7 km² flood, 2.6% basin coverage
   - Patch grid: 665 total, 73 selected, 592 rejected (insufficient flood coverage)
   - Distribution: 27 patches 0–1% flood, 32 patches 1–10%, 13 patches 10–30%, 1 patch 30–60%
   - QC: valid pixel mean=99.95%, min=98.27% — nearly perfect
   - Spatial: concentrated in a ~15×8 patch corridor (rows 17–32, cols 3–10) along Agno main stem
   - Diagnostic map: `work/agno_20230903_diagnostic.png`
   - Preview tiles: **4 tifs pulled** to `dataset/agno_2023-09-03/patches/` — **QGIS-inspected by user ✅**
   - Other candidate events that failed: `agno_2024-07-23` (insufficient_coverage), `agno_2022-08-05` (zero flood in basin)
4. **Commit** the multi-basin + Agno work in logical commits. Do NOT commit `data/hydrosheds/` (gitignored)
   or the historical Agusan working files (untracked/left as-is; decide separately whether to delete).
5. **Optional later:** bare `python main.py` batch (re-exports all; doubles GEE quota — only after every
   event individually validated + exported).

---

## Decision required: how to get an Agno validation through

Both candidate events for the Agno basin failed:
- **`2024-07-23`:** the ASC orbit-142 around that date has no pre-flood S1 scene (same issue Pampanga had;
  relaxed for Pampanga via `S1_MATCH_ORBIT_PASS_BY_BASIN["pampanga"]=False`, but Agno needs its own fix
  AND a valid pre/post pair must exist for whatever orbit is available).
- **`2022-08-05`:** zero flood in the Agno basin (Mindanao event, entirely south of 14°N).

**Two routes forward (neither requires modifying the basin geometry):**

A. **Add Agno to the orbit-pass override + try a third Agno event:**
   - Set `S1_MATCH_ORBIT_PASS_BY_BASIN["agno"] = False` in `gee_config.py` (same fix Pampanga used).
   - Find another Agno flood event (post-2023 when S1C was launched, or check NWRB/RBCO logs for
     historical Agno floods).
   - Run `download_philsa.py --date <new_date> --basin agno --no-region` + inspect + validate.

B. **Broaden the pre-flood search window** for Agno (if ASC remains the only available orbit):
   - Increase `PRE_FLOOD_SEARCH_WINDOW_DAYS` to 36–48 for Agno (or add a per-basin override).
   - This risks using a pre-flood scene months before the flood (temporal signal mismatch), but
     may be the only option if Agno's orbit coverage is sparse.

C. **Skip the Agno validation for now** and commit the basin + other validated work, returning to
   Agno when a suitable event is identified from NWRB/RBCO logs or a future PhilSA package.

---

## Key files / where things are

| Path | Role |
|---|---|
| `gee_config.py` | `BASINS` (3 basins: cagayan, pampanga, **agno** — all `local_path`), `POST_FLOOD_SEARCH_WINDOW_DAYS=14`, `S1_REQUIRE_MATCHING_ORBIT_PASS=True`, `S1_MATCH_ORBIT_PASS_BY_BASIN` (pampanga only, line ~90), `EE_PROJECT`, `DRIVE_FOLDER` |
| `philsa.py` | `compute_work_region_local`, `_utm_epsg_for_lonlat`, `load_events`, `rasterize_flood_mask`, `shapefile_to_ee_featurecollection` |
| `sentinel1.py` | `match_pre_post_scenes` (strict/relaxed), `find_post_flood_scene`, `find_pre_flood_scene`, `get_temporal_sar_stack` (takes `basin=`) |
| `main.py` / `validate.py` | pass `basin=event.basin` into `get_temporal_sar_stack` |
| `download_philsa.py` | HDX fetch, `--no-region` flag for nationwide packages |
| `scripts/build_hydrosheds_basin.py` | basin builder; `--hybas-id 5120029740 --name agno` built the Agno basin |
| `scripts/download_drive_patches.py` | pulls Drive tiles → `dataset/<event>/patches/` |
| `data/basins/agno.geojson` | **NEW** Agno basin (53 catchments, 6,664 km²) |
| `work/agno_basin_map.png`, `work/agno_basin_map.html` | **NEW** Agno visual validation maps |
| `data/philsa_events.csv` | 2 events (cagayan, pampanga); no agno event yet (both candidates failed) |
| `data/validation_report.json` | cagayan×2 valid, pampanga valid (120); agno_2024-07-23 failed (missing_pre_s1); agno_2022-08-05 never ran (zero flood in basin) |
| `data/basins/agusan.geojson`, `data/philsa_shapefiles/agusan_*.*` | **HISTORICAL — keep on disk, do NOT use** |
| `.claude/plans/ancient-wibbling-gadget.md` | the approved multi-basin plan (predates Agno correction) |

---

## Repo design invariants (don't break)
- **No fabricated data** — every event/boundary from a real source. `gee_config.ensure_ready()` +
  `philsa.load_events()` fail loudly on placeholders/unknown basins.
- **Basin is a clip.** `work_region ≈ flood.buffer(2000) ∩ basin`. Basin must contain the flood.
- **One event at a time** for GEE quota. Previews are a subset of selected patches.
- **`event_id` / shapefile basename / BASINS key must all match** the `basin_YYYY-MM-DD` convention.
- Exports are **257×257 px** (EE `toDrive` 1px border) — read full 257×257, all bands share the grid.
- Sentinel-1 used as provided (calibrated σ⁰ dB); DON'T convert.
- Cross-basin integrity: extraction never mixes events; splits downstream on `patch_metadata.csv`.

---

## Useful commands
```bash
# local pre-flight (no GEE)
python -c "import gee_config; gee_config.ensure_ready(); print('ok')"
python -c "import philsa; print([e.event_id for e in philsa.load_events()])"

# validate one event (GEE, a few min)
python main.py --validate-one --event pampanga_2024-07-23   # Pampanga VALID ✅ 120 patches

# Agno basin — already built; rebuild is regression-safe:
python scripts/build_hydrosheds_basin.py --hybas-id 5120029740 --name agno

# fetch an Agno PhilSA shapefile (nationwide packages → --no-region)
python download_philsa.py --date 2024-07-23 --basin agno --no-region

# pull preview tiles + human QGIS check
python scripts/download_drive_patches.py --wait --event pampanga_2024-07-23

# WINDOWS NOTE: keep any new print() / stdio text ASCII-only (cp1252 console; → etc. crash with UnicodeEncodeError).
```
