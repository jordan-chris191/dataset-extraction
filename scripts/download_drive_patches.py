"""
download_drive_patches.py
-------------------------
Download exported patch GeoTIFFs from Google Drive into the local dataset
layout. (patch_metadata.csv rows are written by the export step itself, via
metadata.append_patch_record; this script only pulls the .tif files down.)

Background
----------
The pipeline's EXPORT_DESTINATION = "drive" only *queues* Earth Engine batch
tasks to your Google Drive folder (gee_config.DRIVE_FOLDER). The actual .tif
files land in the cloud and NEVER appear locally just by running main.py.
The modern `earthengine` CLI has no `task run` download command (only
cancel/info/list/wait), so this script pulls completed tiles from Drive using
RAW HTTP against the Google Drive v3 API, authenticated with the SAME
refresh_token already stored for Earth Engine in
~/.config/earthengine/credentials (which carries the drive scope).

Usage
-----
  python scripts/download_drive_patches.py --status
  python scripts/download_drive_patches.py --event cagayan_2024-10-27
  python scripts/download_drive_patches.py --event cagayan_2024-10-27 --wait
  python scripts/download_drive_patches.py --all

The script:
  1. (optional --wait) blocks until matching non-terminal tasks finish.
  2. For each COMPLETED task whose description matches a patch grid id
     ({event_id}_r{row:03d}_c{col:03d}), resolves the Drive file named
     {patch_id}.tif in the export folder and downloads it to
     <OUTPUT_ROOT>/<event_id>/patches/<patch_id>.tif.
  3. Prints what it downloaded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import ee

# Allow running as `python scripts/download_drive_patches.py` from the repo
# root: this script lives in scripts/ but imports the top-level gee_config.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gee_config as config

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


PATCH_ID_RE = re.compile(r"^(?P<event>[A-Za-z0-9_-]+)_r(?P<row>\d+)_c(?P<col>\d+)$")


def _creds_path() -> str:
    return os.path.expanduser("~/.config/earthengine/credentials")


def _access_token() -> str:
    """Exchange the EE refresh_token for a short-lived access token (raw HTTP).

    Uses the SAME OAuth client that minted the refresh token (the Earth Engine
    CLI client, from ee.oauth) — a refresh token is bound to the client that
    issued it, so exchanging it against a different client_id (or an empty
    client_secret) is rejected with a 401. Any client_id/client_secret stored in
    the credentials file itself take precedence; otherwise fall back to the ee
    library's own OAuth client.
    """
    if not requests:
        raise SystemExit("pip install requests to use the Drive downloader.")
    cred_path = _creds_path()
    stored = {}
    try:
        with open(cred_path, encoding="utf-8") as f:
            stored = json.load(f)
    except OSError:
        pass

    refresh_token = stored.get("refresh_token")
    if not refresh_token:
        raise SystemExit("No refresh_token in EE credentials — re-auth needed.")
    client_id = stored.get("client_id") or getattr(ee.oauth, "CLIENT_ID", None)
    client_secret = stored.get("client_secret") or getattr(ee.oauth, "CLIENT_SECRET", "")

    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _drive_headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}"}


def _find_drive_file(patch_id: str, folder: str, headers: dict):
    """Return (file_id, name) for {patch_id}.tif in the export folder, if any."""
    if not requests:
        return None
    # First locate the export folder by name, then search inside it.
    q = f"name = '{folder}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = requests.get("https://www.googleapis.com/drive/v3/files",
                     params={"q": q, "fields": "files(id)"}, headers=headers, timeout=30)
    if r.status_code != 200:
        return None
    folders = r.json().get("files", [])
    fid = folders[0]["id"] if folders else None
    if not fid:
        # Folder may not be queryable; search globally by exact filename.
        q = f"name = '{patch_id}.tif' and trashed = false"
        r2 = requests.get("https://www.googleapis.com/drive/v3/files",
                          params={"q": q, "fields": "files(id, name)"}, headers=headers, timeout=30)
        if r2.status_code != 200:
            return None
        hits = r2.json().get("files", [])
        return (hits[0]["id"], hits[0]["name"]) if hits else None
    q = f"name = '{patch_id}.tif' and '{fid}' in parents and trashed = false"
    r3 = requests.get("https://www.googleapis.com/drive/v3/files",
                      params={"q": q, "fields": "files(id, name)"}, headers=headers, timeout=30)
    if r3.status_code != 200:
        return None
    hits = r3.json().get("files", [])
    return (hits[0]["id"], hits[0]["name"]) if hits else None


def _download_file(file_id: str, dest: str, headers: dict) -> bool:
    if not requests:
        return False
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    r = requests.get(url, headers=headers, stream=True, timeout=300)
    if r.status_code != 200:
        return False
    with open(dest, "wb") as fh:
        for chunk in r.iter_content(1 << 16):
            fh.write(chunk)
    return True


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def _ee_initialize():
    ee.Initialize(project=config.EE_PROJECT)


def _snapshot(tasks) -> list[dict]:
    out = []
    for t in tasks:
        st = t.status()
        out.append({
            "id": t.id,
            "state": st.get("state"),
            "desc": (t.config or {}).get("description"),
            "error": st.get("error_message") or "",
        })
    return out


def list_status(event: str | None = None) -> None:
    _ee_initialize()
    snap = _snapshot(ee.batch.Task.list())
    if event:
        snap = [s for s in snap if s["desc"] and s["desc"].startswith(event)]
    print(f"{len(snap)} task(s):")
    for s in sorted(snap, key=lambda x: x["id"]):
        print(f"  {s['id']}  {s['state']:<12} {s['desc']}"
              + (f"  · {s['error'][:80]}" if s["error"] else ""))


def wait_for(event: str | None = None, timeout_s: int = 3600) -> list[dict]:
    _ee_initialize()
    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        tasks = ee.batch.Task.list()
        pending = []
        for t in tasks:
            if t.status().get("state") in terminal:
                continue
            if event and not ((t.config or {}).get("description", "") or "").startswith(event):
                continue
            pending.append(t)
        if not pending:
            return _snapshot([t for t in tasks])
        counts = {}
        for t in pending:
            s = t.status().get("state")
            counts[s] = counts.get(s, 0) + 1
        print(f"[{time.strftime('%H:%M:%S')}] waiting… {counts}")
        time.sleep(10)
    raise TimeoutError(f"Tasks did not finish within {timeout_s}s")


def download_completed(event: str | None = None, out_root: str | None = None,
                       wait: bool = False, dry_run: bool = False,
                       force: bool = False) -> dict:
    """Pull exported patch GeoTIFFs from Drive into <out_root>/<event>/patches/.

    Returns a dict with counts/sets for reporting:
      {"skipped_local": [...], "found": [...], "missing": [...], "downloaded": [...]}
    - Only COMPLETED patch tasks are considered (never submits new tasks).
    - patch_ids are de-duplicated so a retried patch with several COMPLETED
      tasks is downloaded at most once.
    - Already-local .tif files are skipped unless force=True (resume-safe).
    - dry_run=True resolves Drive availability but writes no bytes.
    """
    out_root = out_root or config.OUTPUT_ROOT
    _ee_initialize()
    if wait:
        wait_for(event)

    headers = _drive_headers()
    folders = [config.DRIVE_FOLDER]
    tasks = ee.batch.Task.list()

    seen = set()
    patch_tasks = []
    for t in tasks:
        st = t.status()
        cfg = t.config or {}
        desc = cfg.get("description", "")
        if st.get("state") != "COMPLETED":
            continue
        if event and not desc.startswith(event):
            continue
        if not PATCH_ID_RE.match(desc):
            continue
        if desc in seen:
            continue
        seen.add(desc)
        patch_tasks.append(desc)

    skipped_local, found, missing, downloaded = [], [], [], []
    for patch_id in patch_tasks:
        ev = PATCH_ID_RE.match(patch_id).group("event")
        dest = os.path.join(out_root, ev, "patches", f"{patch_id}.tif")
        if not force and os.path.exists(dest):
            skipped_local.append(patch_id)
            print(f"  = {patch_id}.tif already local — skip")
            continue
        hit = None
        for folder in folders:
            hit = _find_drive_file(patch_id, folder, headers)
            if hit:
                break
        if not hit:
            missing.append(patch_id)
            print(f"  !! no Drive file for {patch_id}")
            continue
        found.append(patch_id)
        if dry_run:
            print(f"  ~ {patch_id}.tif available in Drive (dry-run)")
            continue
        out_dir = os.path.dirname(dest)
        os.makedirs(out_dir, exist_ok=True)
        if _download_file(hit[0], dest, headers):
            downloaded.append(patch_id)
            print(f"  + {patch_id}.tif")
        else:
            print(f"  !! download failed for {patch_id}")
    return {"skipped_local": skipped_local, "found": found,
            "missing": missing, "downloaded": downloaded}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", help="Filter by event_id")
    ap.add_argument("--all", action="store_true", help="All events (all COMPLETED patches)")
    ap.add_argument("--status", action="store_true", help="Print task status and exit")
    ap.add_argument("--wait", action="store_true", help="Wait for running tasks first")
    ap.add_argument("--out", help="Override OUTPUT_ROOT")
    ap.add_argument("--folder", help="Override Drive folder name (default flood_seg_dataset)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve Drive availability but download nothing")
    ap.add_argument("--force", action="store_true",
                    help="Re-download tifs that already exist locally")
    args = ap.parse_args()

    if args.status:
        list_status(args.event)
        return

    if args.event and args.all:
        sys.exit("--event and --all are mutually exclusive")
    if not args.event and not args.all:
        sys.exit("Provide --event <event_id> or --all")

    if args.folder:
        config.DRIVE_FOLDER = args.folder
    result = download_completed(event=None if args.all else args.event,
                                out_root=args.out, wait=args.wait,
                                dry_run=args.dry_run, force=args.force)
    print("\n------------------------")
    print(f"  already local : {len(result['skipped_local'])}")
    print(f"  found in Drive: {len(result['found'])}")
    print(f"  missing Drive : {len(result['missing'])}")
    print(f"  downloaded    : {len(result['downloaded'])}")
    print("------------------------")


if __name__ == "__main__":
    main()