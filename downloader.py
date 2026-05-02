#!/usr/bin/env python3
"""
downloader.py — Download, extract, and index a vehicle service manual.

Supports ZIP archives from:
  - lemon-manuals.la
  - charm.li (direct ZIP download links)
  - Any direct URL to a .zip file

Usage (CLI):
    venv/bin/python3 downloader.py \
        --url "https://lemon-manuals.la/download/..." \
        --vehicle "2003 Honda Civic EX"

    # With explicit folder name (optional — auto-derived if omitted)
    venv/bin/python3 downloader.py \
        --url "https://..." \
        --vehicle "2003 Honda Civic EX L4-1.7L" \
        --folder "2003_Honda_Civic_EX_I4-1.7L_FWD_GSL"
"""

import os
import re
import sys
import uuid
import shutil
import zipfile
import tempfile
import argparse
import threading
import subprocess
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
from functools import partial

VEHICLES_DIR = Path(__file__).parent / "sources" / "vehicles"
BUILD_INDEX  = Path(__file__).parent / "build_index.py"
PYTHON       = Path(__file__).parent / "venv" / "bin" / "python3"


# ─────────────────────────────────────────────
# Job tracking (for web UI progress streaming)
# ─────────────────────────────────────────────

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def create_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "messages": [], "done": False, "error": None}
    return job_id


def job_log(job_id: str, msg: str):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["messages"].append(msg)
    print(msg)


def job_done(job_id: str, error: str = None):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["done"] = True
            _jobs[job_id]["error"] = error
            _jobs[job_id]["status"] = "error" if error else "complete"


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _log(msg: str, job_id: str = None):
    """Log a message to the job queue and stdout."""
    if job_id:
        job_log(job_id, msg)
    else:
        print(msg)


def derive_folder_name(vehicle_name: str) -> str:
    """
    Convert a vehicle name to the folder naming convention.
    "2014 Toyota Sienna XLE FWD" → "2014_Toyota_Sienna_XLE_FWD"
    """
    name = re.sub(r"[^\w\s\-\.]", "", vehicle_name)
    name = re.sub(r"\s+", "_", name.strip())
    return name


def find_pages_dir(root: Path) -> Path | None:
    """Recursively find the pages/ directory containing HTML files."""
    for pages in root.rglob("pages"):
        if pages.is_dir() and any(pages.glob("*.html")):
            return pages
    return None


# ─────────────────────────────────────────────
# Core workflow
# ─────────────────────────────────────────────

def download_zip(url: str, dest_path: Path, job_id: str = None) -> bool:
    """Download a ZIP file with progress reporting."""
    log = partial(_log, job_id=job_id)
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            total_mb = total / 1024 / 1024

            if total_mb > 0:
                log(f"Downloading {total_mb:.1f} MB...")
            else:
                log("Downloading (size unknown)...")

            downloaded = 0
            last_pct = -1
            chunk = 65536

            with open(dest_path, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)
                    if total:
                        pct = int(downloaded / total * 100)
                        if pct // 10 != last_pct // 10:
                            last_pct = pct
                            log(f"  {pct}% ({downloaded / 1024 / 1024:.1f} MB)")

        log(f"Download complete: {dest_path.stat().st_size / 1024 / 1024:.1f} MB")
        return True

    except URLError as e:
        log(f"Download failed: {e}")
        return False


def extract_and_install(zip_path: Path, vehicle_folder: str, job_id: str = None) -> Path | None:
    """Extract ZIP and move pages/ + assets into the vehicle folder."""
    log = partial(_log, job_id=job_id)
    vehicle_dir = VEHICLES_DIR / vehicle_folder
    vehicle_dir.mkdir(parents=True, exist_ok=True)

    log(f"Extracting ZIP to temp directory...")
    tmp_dir = Path(tempfile.mkdtemp())

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            total_files = len(zf.namelist())
            log(f"  {total_files} files in archive")
            zf.extractall(tmp_dir)

        log("Locating pages directory...")
        pages_src = find_pages_dir(tmp_dir)

        if pages_src is None:
            log("ERROR: No pages/ directory found in ZIP. Is this a valid CHARM/LEMON archive?")
            return None

        log(f"  Found: {pages_src.relative_to(tmp_dir)}")
        log(f"  Pages: {len(list(pages_src.glob('*.html')))}")

        # The manual root is pages_src's parent — move all its contents
        manual_root = pages_src.parent

        for item in manual_root.iterdir():
            dest = vehicle_dir / item.name
            if dest.exists():
                log(f"  Replacing existing: {item.name}/")
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            log(f"  Moving: {item.name}/")
            shutil.move(str(item), str(dest))

        # Add .gitkeep so folder stays tracked if data is removed
        (vehicle_dir / ".gitkeep").touch()

        log(f"Installed to: sources/vehicles/{vehicle_folder}/")
        return vehicle_dir / "pages"

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_index(vehicle_name: str, pages_dir: Path, job_id: str = None):
    """Run build_index.py for the given vehicle."""
    log = partial(_log, job_id=job_id)
    log(f"Indexing '{vehicle_name}'...")
    log(f"  Source: {pages_dir}")

    cmd = [str(PYTHON), str(BUILD_INDEX),
           "--source", str(pages_dir),
           "--vehicle", vehicle_name]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(Path(__file__).parent),
    )

    for line in proc.stdout:
        log(line.rstrip())

    proc.wait()

    if proc.returncode != 0:
        log(f"Indexing failed (exit code {proc.returncode})")
        return False

    return True


def add_vehicle(vehicle_name: str, vehicle_folder: str = None,
                url: str = None, local_path: str = None,
                job_id: str = None):
    """Full pipeline: (download or use local file) → extract → index."""
    log = partial(_log, job_id=job_id)
    if not url and not local_path:
        job_done(job_id, error="Must provide either a URL or a local file path")
        return

    if not vehicle_folder:
        vehicle_folder = derive_folder_name(vehicle_name)

    log(f"=== Adding vehicle: {vehicle_name} ===")
    log(f"Folder: sources/vehicles/{vehicle_folder}/")
    log("")

    tmp_zip = None
    cleanup_zip = False

    try:
        if local_path:
            zip_path = Path(local_path).expanduser().resolve()
            if not zip_path.exists():
                job_done(job_id, error=f"File not found: {zip_path}")
                return
            if not zipfile.is_zipfile(zip_path):
                job_done(job_id, error=f"Not a valid ZIP file: {zip_path}")
                return
            log(f"Using local file: {zip_path}")
            log(f"  Size: {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
            log("")
        else:
            fd, tmp_path = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            tmp_zip = Path(tmp_path)
            cleanup_zip = True
            ok = download_zip(url, tmp_zip, job_id=job_id)
            if not ok:
                job_done(job_id, error="Download failed")
                return
            zip_path = tmp_zip
            log("")

        # Extract
        pages_dir = extract_and_install(zip_path, vehicle_folder, job_id=job_id)
        if pages_dir is None:
            job_done(job_id, error="Extraction failed — no pages/ directory found")
            return

        log("")

        # Index
        ok = run_index(vehicle_name, pages_dir, job_id=job_id)
        if not ok:
            job_done(job_id, error="Indexing failed")
            return

        log("")
        log(f"=== Done! '{vehicle_name}' is now searchable. ===")
        job_done(job_id)

    except Exception as e:
        log(f"Unexpected error: {e}")
        job_done(job_id, error=str(e))
    finally:
        if cleanup_zip and tmp_zip:
            tmp_zip.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Install and index a vehicle service manual ZIP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", "-u",
                        help="URL to the ZIP file (charm.li direct download)")
    source.add_argument("--file", "-i",
                        help="Path to a locally downloaded ZIP file")
    parser.add_argument("--vehicle", "-v", required=True,
                        help='Vehicle label, e.g. "2003 Honda Civic EX L4-1.7L"')
    parser.add_argument("--folder", "-f",
                        help="Override folder name (auto-derived from vehicle name if omitted)")
    args = parser.parse_args()

    add_vehicle(args.vehicle, vehicle_folder=args.folder,
                url=args.url, local_path=args.file)


if __name__ == "__main__":
    main()
