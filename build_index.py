#!/usr/bin/env python3
"""
build_index.py — Parse all 1988 F-350 service manual HTML pages
and store them in a SQLite FTS5 database for fast full-text search.

Usage:
    python3 build_index.py

Creates: truck_manual.db in the same directory as this script.
"""

import os
import sqlite3
import re
from pathlib import Path
from bs4 import BeautifulSoup

PAGES_DIR = Path(__file__).parent.parent / "sources" / "1988 Ford F 350 2WD Pickup V8-7.3L DSL" / "pages"
DB_PATH = Path(__file__).parent / "truck_manual.db"


def extract_page(html_path: Path) -> dict | None:
    """Parse a single HTML page and return structured data."""
    try:
        with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        # Title — strip the " | Operation CHARM" suffix
        title_tag = soup.find("title")
        if not title_tag:
            return None
        title = title_tag.get_text()
        title = re.sub(r"\s*[—–-]\s*1988 Ford.*$", "", title).strip()

        # Breadcrumb — the navigation path showing where this page sits
        breadcrumb_div = soup.find("div", class_="breadcrumbs")
        breadcrumb = ""
        if breadcrumb_div:
            parts = [a.get_text(strip=True) for a in breadcrumb_div.find_all("a")]
            # Drop the first 4 generic parts (Home > Ford > 1988 > F350...)
            relevant = parts[4:] if len(parts) > 4 else parts
            breadcrumb = " > ".join(relevant)

        # Main content
        main_div = soup.find("div", class_="main")
        if not main_div:
            return None

        # Remove expand/collapse buttons — not content
        for btn in main_div.find_all("button"):
            btn.decompose()

        content = main_div.get_text(separator=" ", strip=True)
        content = re.sub(r"\s+", " ", content).strip()

        # Determine section (top-level category) from breadcrumb
        breadcrumb_parts = breadcrumb.split(" > ")
        section = breadcrumb_parts[0] if breadcrumb_parts else ""
        subsection = breadcrumb_parts[1] if len(breadcrumb_parts) > 1 else ""

        # Flag nav-only pages (they have very short content — just a list of links)
        is_nav = len(content) < 300

        return {
            "page_num": int(html_path.stem),
            "title": title,
            "breadcrumb": breadcrumb,
            "section": section,
            "subsection": subsection,
            "content": content,
            "is_nav": is_nav,
        }

    except Exception as e:
        print(f"  WARNING: Could not parse {html_path.name}: {e}")
        return None


def build_database():
    print(f"Source:   {PAGES_DIR}")
    print(f"Database: {DB_PATH}")
    print()

    # Collect all page files
    html_files = sorted(PAGES_DIR.glob("*.html"), key=lambda p: int(p.stem))
    total = len(html_files)
    print(f"Found {total} HTML pages to index...")

    # Connect and create schema
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript("""
        DROP TABLE IF EXISTS pages_fts;
        DROP TABLE IF EXISTS pages;

        CREATE TABLE pages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            page_num    INTEGER UNIQUE NOT NULL,
            title       TEXT NOT NULL,
            breadcrumb  TEXT,
            section     TEXT,
            subsection  TEXT,
            content     TEXT,
            is_nav      INTEGER DEFAULT 0
        );

        -- FTS5 virtual table for full-text search
        -- Weights: title matches rank highest, then breadcrumb, then content
        CREATE VIRTUAL TABLE pages_fts USING fts5(
            title,
            breadcrumb,
            section,
            content,
            content=pages,
            content_rowid=id,
            tokenize='porter unicode61'
        );

        -- Triggers to keep FTS in sync with pages table
        CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, breadcrumb, section, content)
            VALUES (new.id, new.title, new.breadcrumb, new.section, new.content);
        END;

        CREATE TRIGGER pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, breadcrumb, section, content)
            VALUES ('delete', old.id, old.title, old.breadcrumb, old.section, old.content);
        END;

        CREATE TRIGGER pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, breadcrumb, section, content)
            VALUES ('delete', old.id, old.title, old.breadcrumb, old.section, old.content);
            INSERT INTO pages_fts(rowid, title, breadcrumb, section, content)
            VALUES (new.id, new.title, new.breadcrumb, new.section, new.content);
        END;
    """)

    # Parse and insert pages
    inserted = 0
    skipped = 0
    batch = []
    BATCH_SIZE = 500

    for i, html_file in enumerate(html_files):
        if i % 500 == 0:
            print(f"  Processing {i}/{total}...")

        data = extract_page(html_file)
        if data is None:
            skipped += 1
            continue

        batch.append((
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
                    (page_num, title, breadcrumb, section, subsection, content, is_nav)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, batch)
            conn.commit()
            inserted += len(batch)
            batch.clear()

    # Insert remaining
    if batch:
        cur.executemany("""
            INSERT OR REPLACE INTO pages
                (page_num, title, breadcrumb, section, subsection, content, is_nav)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()
        inserted += len(batch)

    # Optimize FTS index
    print("  Optimizing FTS index...")
    cur.execute("INSERT INTO pages_fts(pages_fts) VALUES('optimize')")
    conn.commit()

    # Summary stats
    cur.execute("SELECT COUNT(*) FROM pages")
    total_pages = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM pages WHERE is_nav = 0")
    content_pages = cur.fetchone()[0]
    cur.execute("SELECT section, COUNT(*) as cnt FROM pages GROUP BY section ORDER BY cnt DESC LIMIT 10")
    sections = cur.fetchall()

    conn.close()

    db_size_mb = DB_PATH.stat().st_size / 1024 / 1024
    print()
    print(f"Done!")
    print(f"  Total pages indexed : {total_pages}")
    print(f"  Content pages       : {content_pages}")
    print(f"  Nav-only pages      : {total_pages - content_pages}")
    print(f"  Skipped (errors)    : {skipped}")
    print(f"  Database size       : {db_size_mb:.1f} MB")
    print()
    print("Top sections by page count:")
    for section, count in sections:
        print(f"  {count:5d}  {section or '(root)'}")


if __name__ == "__main__":
    build_database()
