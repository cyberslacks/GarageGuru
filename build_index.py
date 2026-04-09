#!/usr/bin/env python3
"""
build_index.py — Parse service manual HTML pages and store in SQLite FTS5.

Usage:
    # Index the default F-350 manual
    python3 build_index.py

    # Index a different vehicle
    python3 build_index.py \\
        --source "../sources/2014 Toyota Sienna XLE FWD/" \\
        --vehicle "2014 Toyota Sienna XLE FWD"

    # Wipe the entire database and start fresh
    python3 build_index.py --rebuild

Running for a vehicle that is already indexed will re-index it (safe to re-run).
"""

import os
import re
import sys
import sqlite3
import argparse
from pathlib import Path
from bs4 import BeautifulSoup

DB_PATH = Path(__file__).parent / "truck_manual.db"

DEFAULT_SOURCE  = Path(__file__).parent.parent / "sources" / "1988 Ford F 350 2WD Pickup V8-7.3L DSL" / "pages"
DEFAULT_VEHICLE = "1988 Ford F-350 7.3L IDI"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert a vehicle name to a URL-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def extract_page(html_path: Path, vehicle: str) -> dict | None:
    """Parse a single HTML page and return structured data."""
    try:
        with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        title_tag = soup.find("title")
        if not title_tag:
            return None
        title = title_tag.get_text()
        title = re.sub(r"\s*[—–-]\s*.*$", "", title).strip()

        breadcrumb_div = soup.find("div", class_="breadcrumbs")
        breadcrumb = ""
        if breadcrumb_div:
            parts = [a.get_text(strip=True) for a in breadcrumb_div.find_all("a")]
            relevant = parts[4:] if len(parts) > 4 else parts
            breadcrumb = " > ".join(relevant)

        main_div = soup.find("div", class_="main")
        if not main_div:
            return None

        for btn in main_div.find_all("button"):
            btn.decompose()

        content = main_div.get_text(separator=" ", strip=True)
        content = re.sub(r"\s+", " ", content).strip()

        breadcrumb_parts = breadcrumb.split(" > ")
        section    = breadcrumb_parts[0] if breadcrumb_parts else ""
        subsection = breadcrumb_parts[1] if len(breadcrumb_parts) > 1 else ""
        is_nav     = len(content) < 300

        return {
            "vehicle":    vehicle,
            "page_num":   int(html_path.stem),
            "title":      title,
            "breadcrumb": breadcrumb,
            "section":    section,
            "subsection": subsection,
            "content":    content,
            "is_nav":     is_nav,
        }

    except Exception as e:
        print(f"  WARNING: Could not parse {html_path.name}: {e}")
        return None


# ─────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS pages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle     TEXT NOT NULL,
        pages_dir   TEXT,
        page_num    INTEGER NOT NULL,
        title       TEXT NOT NULL,
        breadcrumb  TEXT,
        section     TEXT,
        subsection  TEXT,
        content     TEXT,
        is_nav      INTEGER DEFAULT 0,
        UNIQUE(vehicle, page_num)
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
        title,
        breadcrumb,
        section,
        content,
        content=pages,
        content_rowid=id,
        tokenize='porter unicode61'
    );

    CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
        INSERT INTO pages_fts(rowid, title, breadcrumb, section, content)
        VALUES (new.id, new.title, new.breadcrumb, new.section, new.content);
    END;

    CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
        INSERT INTO pages_fts(pages_fts, rowid, title, breadcrumb, section, content)
        VALUES ('delete', old.id, old.title, old.breadcrumb, old.section, old.content);
    END;

    CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
        INSERT INTO pages_fts(pages_fts, rowid, title, breadcrumb, section, content)
        VALUES ('delete', old.id, old.title, old.breadcrumb, old.section, old.content);
        INSERT INTO pages_fts(rowid, title, breadcrumb, section, content)
        VALUES (new.id, new.title, new.breadcrumb, new.section, new.content);
    END;
"""

DROP_SQL = """
    DROP TABLE IF EXISTS pages_fts;
    DROP TABLE IF EXISTS pages;
    DROP TRIGGER IF EXISTS pages_ai;
    DROP TRIGGER IF EXISTS pages_ad;
    DROP TRIGGER IF EXISTS pages_au;
"""


def schema_has_vehicle_column(conn) -> bool:
    """Check if the existing schema already has the vehicle column."""
    try:
        conn.execute("SELECT vehicle FROM pages LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def init_db(conn, rebuild: bool = False):
    cur = conn.cursor()
    if rebuild:
        print("  Dropping existing tables...")
        cur.executescript(DROP_SQL)
    cur.executescript(SCHEMA_SQL)
    conn.commit()


# ─────────────────────────────────────────────
# Indexing
# ─────────────────────────────────────────────

def index_vehicle(pages_dir: Path, vehicle: str, rebuild: bool = False):
    print(f"Source  : {pages_dir}")
    print(f"Vehicle : {vehicle}")
    print(f"Database: {DB_PATH}")
    print()

    if not pages_dir.exists():
        print(f"ERROR: Source directory not found: {pages_dir}")
        sys.exit(1)

    html_files = sorted(pages_dir.glob("*.html"), key=lambda p: int(p.stem))
    total = len(html_files)
    if total == 0:
        print(f"ERROR: No HTML files found in {pages_dir}")
        sys.exit(1)

    print(f"Found {total} HTML pages to index...")

    conn = sqlite3.connect(DB_PATH)

    # Migrate old schema (no vehicle column) if needed
    if DB_PATH.exists() and not schema_has_vehicle_column(conn) and not rebuild:
        print("  Existing database has old schema — rebuilding with vehicle support...")
        rebuild = True

    init_db(conn, rebuild=rebuild)

    # Remove existing pages for this vehicle so re-runs are safe
    existing = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE vehicle = ?", (vehicle,)
    ).fetchone()[0]
    if existing and not rebuild:
        print(f"  Removing {existing} existing pages for '{vehicle}'...")
        conn.execute("DELETE FROM pages WHERE vehicle = ?", (vehicle,))
        conn.commit()

    cur = conn.cursor()
    inserted = 0
    skipped  = 0
    batch    = []
    BATCH_SIZE = 500

    for i, html_file in enumerate(html_files):
        if i % 500 == 0:
            print(f"  Processing {i}/{total}...")

        data = extract_page(html_file, vehicle)
        if data is None:
            skipped += 1
            continue

        batch.append((
            data["vehicle"],
            str(pages_dir.resolve()),
            data["page_num"],
            data["title"],
            data["breadcrumb"],
            data["section"],
            data["subsection"],
            data["content"],
            1 if data["is_nav"] else 0,
        ))

        if len(batch) >= BATCH_SIZE:
            cur.executemany("""
                INSERT OR REPLACE INTO pages
                    (vehicle, pages_dir, page_num, title, breadcrumb, section, subsection, content, is_nav)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, batch)
            conn.commit()
            inserted += len(batch)
            batch.clear()

    if batch:
        cur.executemany("""
            INSERT OR REPLACE INTO pages
                (vehicle, pages_dir, page_num, title, breadcrumb, section, subsection, content, is_nav)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()
        inserted += len(batch)

    print("  Optimizing FTS index...")
    cur.execute("INSERT INTO pages_fts(pages_fts) VALUES('optimize')")
    conn.commit()

    # Stats
    total_pages   = conn.execute("SELECT COUNT(*) FROM pages WHERE vehicle = ?", (vehicle,)).fetchone()[0]
    content_pages = conn.execute("SELECT COUNT(*) FROM pages WHERE vehicle = ? AND is_nav = 0", (vehicle,)).fetchone()[0]
    all_vehicles  = conn.execute("SELECT vehicle, COUNT(*) FROM pages GROUP BY vehicle").fetchall()

    db_size_mb = DB_PATH.stat().st_size / 1024 / 1024

    conn.close()

    print()
    print("Done!")
    print(f"  Pages indexed : {total_pages}")
    print(f"  Content pages : {content_pages}")
    print(f"  Nav-only pages: {total_pages - content_pages}")
    print(f"  Skipped       : {skipped}")
    print(f"  Database size : {db_size_mb:.1f} MB")
    print()
    print("All vehicles in database:")
    for v, cnt in all_vehicles:
        print(f"  {cnt:5d}  {v}")


def main():
    parser = argparse.ArgumentParser(
        description="Index a service manual into truck_manual.db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", "-s",
        help="Path to the pages/ directory (default: F-350 source)")
    parser.add_argument("--vehicle", "-v",
        help=f"Vehicle label (default: \"{DEFAULT_VEHICLE}\")")
    parser.add_argument("--rebuild", action="store_true",
        help="Drop all tables and rebuild from scratch")

    args = parser.parse_args()

    source_dir = Path(args.source) if args.source else DEFAULT_SOURCE
    # If --source points to the vehicle root (not pages/), auto-append pages/
    if source_dir.is_dir() and not any(source_dir.glob("*.html")):
        pages_sub = source_dir / "pages"
        if pages_sub.exists():
            source_dir = pages_sub

    vehicle = args.vehicle if args.vehicle else DEFAULT_VEHICLE

    index_vehicle(source_dir, vehicle, rebuild=args.rebuild)


if __name__ == "__main__":
    main()
