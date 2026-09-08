"""
download_philsa.py
------------------
Small acquisition helper for the PhilSA flood-extent shapefiles (no web app).

The pipeline only reads local shapefiles (see data/philsa_events.csv). This
script fetches a PhilSA flood-event SHP zip from the HDX CKAN API
(https://data.humdata.org, org "philsa"), extracts the shapefile, and stages
it under data/philsa_shapefiles/<basin>_<date>.shp so the pipeline can use it
directly.

Usage:
    python download_philsa.py --date 2024-10-27 --basin cagayan
        [--region cagayan-isabela] [--out-dir data/philsa_shapefiles]

Behavior / rules (deliberate, so no data is invented):
  * The flood date and the affected region come from the *filename* of the
    PhilSA resource (PhilSA DBF fields are only {fid, DN} - date/region are
    NOT stored inside the shapefile). Wiring is only accepted when we can
    read those values back from the resource name on the live API.
  * We require an event date of exactly YYYY-MM-DD (the zip is named like
    "20241027_0547_fld_s1_shp_cagayan-isabela.zip").
  * We select the resource whose lowercased name contains the region token
    (default "cagayan-isabela").
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile

HDX_API = "https://data.humdata.org/api/3/action/package_search"
HDX_ORG = "philsa"
PACKAGE_PREFIX = "philippines-flood-"
DEFAULT_REGION = "cagayan-isabela"
SIDECAR_EXTS = {".shx", ".dbf", ".prj", ".cpg"}


def _urlopen(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def search_packages(query: str) -> list[dict]:
    """Return PhilSA flood packages whose name/title contains `query`."""
    url = f"{HDX_API}?q={urllib.parse.quote(query)}&rows=10&fq=organization:{HDX_ORG}"
    data = json.loads(_urlopen(url))
    return data.get("result", {}).get("results", [])


def acquire(
    date_iso: str,
    basin: str,
    region: str | None = DEFAULT_REGION,
    out_dir: str = "data/philsa_shapefiles",
) -> dict:
    """
    Download + stage the PhilSA flood shapefile for one event.

    `region` selects the SHP resource by a region token in the resource name
    when given (e.g. "cagayan-isabela"). Pass `region=None` for packages whose
    SHP filename carries NO region token (single unnamed S1 SHP, e.g. the
    Pampanga/Agusan nationwide packages) — the S1 SHP is then picked directly,
    and the basin boundary defines the clip, not the resource name.

    Returns a structured report dict:
      {ok, download_url, staged_shp, sidecars, report}
    Raises on any hard failure (no package / no matching SHP resource).
    """
    # 1. Validate date & derive the package token
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date_iso)
    if not m:
        raise ValueError(f"date must be YYYY-MM-DD, got {date_iso!r}")
    y, mo, d = m.groups()
    pkg_token = f"{PACKAGE_PREFIX}{y}{mo}{d}"  # e.g. philippines-flood-20241027

    # 2. Find the live package for that event date
    packages = search_packages(pkg_token)
    pkgs = [p for p in packages if p.get("name", "").startswith(pkg_token)]
    if not pkgs:
        raise LookupError(f"No PhilSA package found for {pkg_token}")
    pkg = sorted(pkgs, key=lambda p: p.get("metadata_modified", ""), reverse=True)[0]

    resources = pkg.get("resources", [])
    shp_resources = [r for r in resources if (r.get("format") or "").upper() == "SHP"]

    # 3. Pick the SHP resource.
    if region is None:
        # No region token available: pick the Sentinel-1 flood SHP directly.
        # The basin boundary defines the clip, not the resource name.
        s1 = [r for r in shp_resources if "s1" in r.get("name", "").lower()]
        if len(shp_resources) == 1:
            res = shp_resources[0]
        elif len(s1) == 1:
            res = s1[0]
        else:
            avail = [r.get("name") for r in shp_resources]
            raise LookupError(
                f"region=None but package {pkg.get('name')} has multiple SHP "
                f"resources and no unique S1 one. Available: {avail}"
            )
        url = res["url"]
        region_note = (
            f"NO region token; using {res.get('name')}; clip defined by basin boundary"
        )
    else:
        region_l = region.lower()
        matches = [r for r in shp_resources if region_l in r.get("name", "").lower()]
        if not matches:
            avail = [r.get("name") for r in shp_resources]
            raise LookupError(
                f"No SHP resource for region {region!r} in package {pkg.get('name')}. "
                f"Available: {avail}"
            )
        res = sorted(matches, key=lambda r: r.get("name", ""), reverse=True)[0]
        url = res["url"]
        region_note = ""

    # 4. Download + extract
    zip_bytes = _urlopen(url)
    os.makedirs(out_dir, exist_ok=True)

    basename = f"{basin}_{date_iso}"
    tmpdir = os.path.join(out_dir, ".download_tmp")
    if os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir)
    os.makedirs(tmpdir, exist_ok=True)

    final_files: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = z.namelist()
            main_shp = [n for n in names if n.lower().endswith(".shp")]
            if not main_shp:
                raise ValueError("zip contains no .shp file")
            picked = max(main_shp, key=len)  # most specific .shp name
            stem = picked.rsplit(".", 1)[0]

            to_extract = [picked]
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext in SIDECAR_EXTS and n.rsplit(".", 1)[0] == stem:
                    to_extract.append(n)

            for n in to_extract:
                ext = os.path.splitext(n)[1].lower().lstrip(".")
                target = os.path.join(tmpdir, f"{basename}.{ext}")
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                final_files[ext] = os.path.join(out_dir, f"{basename}.{ext}")

        if "shp" not in final_files:
            raise ValueError("zip extraction produced no shapefile")

        # Move from tmpdir to final paths
        for ext, target in final_files.items():
            src = os.path.join(tmpdir, f"{basename}.{ext}")
            os.replace(src, target)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    note = f" {region_note}" if region_note else ""
    return {
        "ok": True,
        "download_url": url,
        "staged_shp": final_files["shp"],
        "sidecars": [v for k, v in final_files.items() if k != "shp"],
        "report": (
            f"Downloaded PhilSA {pkg.get('name')} resource {res.get('name')} "
            f"({len(final_files)} files) -> {final_files['shp']}{note}"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Download a PhilSA flood-extent shapefile from HDX.")
    ap.add_argument("--date", required=True, help="Flood event date, YYYY-MM-DD (filename-derived).")
    ap.add_argument("--basin", required=True, help="Basin label used as the output basename.")
    ap.add_argument("--region", default=DEFAULT_REGION, help="Region token in the resource name.")
    ap.add_argument("--no-region", action="store_true",
                    help="Package SHP has no region token; pick the single S1 SHP "
                         "(basin boundary defines the clip).")
    ap.add_argument("--out-dir", default="data/philsa_shapefiles")
    args = ap.parse_args()

    region = None if args.no_region else args.region
    try:
        report = acquire(args.date, args.basin, region, args.out_dir)
    except Exception as exc:  # noqa: BLE001 - CLI-facing
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(report["report"])
    print("staged_shp:", report["staged_shp"])
    return 0


if __name__ == "__main__":
    sys.exit(main())