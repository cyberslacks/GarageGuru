#!/usr/bin/env python3
"""
manual_mcp.py — MCP server for service manual search and management.

Tools:
  search              Full-text search with phrase / proximity / term fallback
  get_page            Full text of a specific manual page
  list_vehicles       All indexed vehicles with page counts
  list_sections       Sections in a vehicle (or all vehicles)
  browse_section      Pages in a section ordered by page number
  add_vehicle_zip     Import and index a local ZIP file
  add_vehicle_url     Download a ZIP from a URL and index it
  reindex_vehicle     Re-parse and re-index an already-extracted vehicle
  delete_vehicle      Remove a vehicle from the search index
  database_stats      Row counts, DB file size, per-vehicle summary

Run directly for testing:
  venv/bin/python3 manual_mcp.py

Register in ~/.claude/settings.json:
  {
    "mcpServers": {
      "service-manual": {
        "command": "/abs/path/truck_agent/venv/bin/python3",
        "args":    ["/abs/path/truck_agent/manual_mcp.py"]
      }
    }
  }
"""

import io
import re
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

# Ensure sibling modules (build_index, downloader) are importable when the
# working directory is somewhere else (e.g. when Claude Code spawns the server).
_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE))

from mcp.server.fastmcp import FastMCP
import build_index as _bi
import downloader  as _dl

DB_PATH     = _HERE / "truck_manual.db"
MANUAL_ROOT = _HERE / "sources" / "vehicles"

mcp = FastMCP("service-manual")


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-16000")
    return conn


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return s.strip("-")


def _resolve_vehicle(conn: sqlite3.Connection, name_or_slug: str) -> str | None:
    """Accept exact vehicle name or slug; return canonical name or None."""
    row = conn.execute(
        "SELECT DISTINCT vehicle FROM pages WHERE vehicle = ?", (name_or_slug,)
    ).fetchone()
    if row:
        return row["vehicle"]
    slug = _slugify(name_or_slug)
    for r in conn.execute("SELECT DISTINCT vehicle FROM pages").fetchall():
        if _slugify(r["vehicle"]) == slug:
            return r["vehicle"]
    return None


def _snippet(content: str, query: str, length: int = 300) -> str:
    if not content:
        return ""
    terms = [t.lower() for t in query.split() if len(t) > 2]
    low   = content.lower()
    pos   = 0
    for term in terms:
        p = low.find(term)
        if p != -1:
            pos = max(0, p - 80)
            break
    chunk = content[pos:pos + length]
    if pos > 0:
        chunk = "..." + chunk
    if pos + length < len(content):
        chunk += "..."
    return chunk


def _run_fts(conn, fts_query: str, n: int, vehicle: str | None,
             section: str | None) -> list:
    where  = ["pages_fts MATCH ?", "p.is_nav = 0"]
    params = [fts_query]
    if vehicle:
        where.append("p.vehicle = ?")
        params.append(vehicle)
    if section:
        where.append("p.section LIKE ?")
        params.append(f"%{section}%")
    params.append(n)
    sql = f"""
        SELECT p.id, p.vehicle, p.page_num, p.title, p.breadcrumb,
               p.section, p.content, rank
        FROM pages_fts
        JOIN pages p ON pages_fts.rowid = p.id
        WHERE {" AND ".join(where)}
        ORDER BY rank
        LIMIT ?
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _no_db() -> str | None:
    if not DB_PATH.exists():
        return "Database not found — run build_index.py first."
    return None


# ─────────────────────────────────────────────
# Search & browse tools
# ─────────────────────────────────────────────

@mcp.tool()
def search(
    query: str,
    vehicle: str | None = None,
    section: str | None = None,
    n: int = 10,
) -> str:
    """Search service manuals using full-text search.

    Uses a three-tier fallback strategy for multi-word queries:
      1. Exact phrase  — words adjacent
      2. Proximity     — words within 5 tokens (NEAR/5)
      3. All terms     — words anywhere on the page

    Args:
        query:   Search terms, e.g. "oil pressure sender" or "glow plug"
        vehicle: Restrict to one vehicle (name or slug). Omit for all vehicles.
        section: Restrict to a section, e.g. "Fuel System" or "Brakes"
        n:       Max results (default 10, max 50)

    Returns ranked results with title, breadcrumb path, vehicle, and excerpt.
    """
    err = _no_db()
    if err:
        return err

    n = max(1, min(n, 50))
    conn = _conn()

    resolved = None
    if vehicle:
        resolved = _resolve_vehicle(conn, vehicle)
        if not resolved:
            conn.close()
            return f"Vehicle '{vehicle}' not found. Call list_vehicles() to see what's indexed."

    words   = query.strip().split()
    seen    = set()
    results = []

    def add_rows(rows):
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                results.append(dict(r))

    if len(words) > 1:
        add_rows(_run_fts(conn, f'"{query}"', n, resolved, section))
        if len(results) < n:
            add_rows(_run_fts(conn, " NEAR/5 ".join(words), n, resolved, section))
    if len(results) < n:
        add_rows(_run_fts(conn, query, n, resolved, section))

    conn.close()

    if not results:
        tip = f" in '{resolved}'" if resolved else ""
        return f"No results for '{query}'{tip}."

    out = [f"Found {len(results)} result(s) for '{query}':\n"]
    for r in results[:n]:
        out.append(f"Page {r['page_num']}  —  {r['title']}")
        if r["breadcrumb"]:
            out.append(f"  Path:    {r['breadcrumb']}")
        out.append(f"  Vehicle: {r['vehicle']}")
        out.append(f"  Ref:     page/{_slugify(r['vehicle'])}/{r['page_num']}")
        snip = _snippet(r["content"], query)
        if snip:
            out.append(f"  Excerpt: {snip}")
        out.append("")
    return "\n".join(out)


@mcp.tool()
def get_page(vehicle: str, page_num: int) -> str:
    """Return the full text content of a specific manual page.

    Args:
        vehicle:  Vehicle name or slug (from list_vehicles or search results)
        page_num: Page number (from search results)
    """
    err = _no_db()
    if err:
        return err

    conn = _conn()
    resolved = _resolve_vehicle(conn, vehicle)
    if not resolved:
        conn.close()
        return f"Vehicle '{vehicle}' not found."

    row = conn.execute(
        "SELECT * FROM pages WHERE vehicle = ? AND page_num = ?",
        (resolved, page_num)
    ).fetchone()
    conn.close()

    if not row:
        return f"Page {page_num} not found for '{resolved}'."

    out = [
        f"Vehicle:  {row['vehicle']}",
        f"Page:     {row['page_num']}",
        f"Title:    {row['title']}",
    ]
    if row["breadcrumb"]:
        out.append(f"Path:     {row['breadcrumb']}")
    out += ["", row["content"] or "(no content)"]
    return "\n".join(out)


@mcp.tool()
def list_vehicles() -> str:
    """List all vehicles currently indexed in the database with page counts."""
    err = _no_db()
    if err:
        return err

    conn = _conn()
    rows = conn.execute("""
        SELECT vehicle,
               COUNT(*) as total,
               SUM(CASE WHEN is_nav = 0 THEN 1 ELSE 0 END) as content
        FROM pages GROUP BY vehicle ORDER BY vehicle
    """).fetchall()
    conn.close()

    if not rows:
        return "No vehicles indexed yet. Use add_vehicle_zip or add_vehicle_url to add one."

    out = [f"{len(rows)} vehicle(s) in database:\n"]
    for r in rows:
        out.append(f"  {r['vehicle']}")
        out.append(f"    slug: {_slugify(r['vehicle'])}  |  {r['content']} content pages  |  {r['total']} total")
    return "\n".join(out)


@mcp.tool()
def list_sections(vehicle: str | None = None) -> str:
    """List manual sections and page counts, optionally filtered to one vehicle.

    Args:
        vehicle: Vehicle name or slug. Omit to list sections across all vehicles.
    """
    err = _no_db()
    if err:
        return err

    conn = _conn()
    resolved = None
    if vehicle:
        resolved = _resolve_vehicle(conn, vehicle)
        if not resolved:
            conn.close()
            return f"Vehicle '{vehicle}' not found."

    if resolved:
        rows = conn.execute("""
            SELECT section, COUNT(*) as cnt FROM pages
            WHERE section != '' AND vehicle = ? AND is_nav = 0
            GROUP BY section ORDER BY cnt DESC
        """, (resolved,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT section, COUNT(*) as cnt FROM pages
            WHERE section != '' AND is_nav = 0
            GROUP BY section ORDER BY cnt DESC
        """).fetchall()
    conn.close()

    if not rows:
        return "No sections found."

    header = f"Sections for '{resolved}':" if resolved else "Sections (all vehicles):"
    out = [header, ""]
    for r in rows:
        out.append(f"  {r['section']:<42}  {r['cnt']} pages")
    return "\n".join(out)


@mcp.tool()
def browse_section(
    section: str,
    vehicle: str | None = None,
    n: int = 50,
) -> str:
    """List pages in a manual section, ordered by page number.

    Args:
        section: Section name (partial match OK, e.g. "Fuel" matches "Fuel System")
        vehicle: Restrict to one vehicle (name or slug)
        n:       Max results (default 50)
    """
    err = _no_db()
    if err:
        return err

    conn = _conn()
    resolved = None
    if vehicle:
        resolved = _resolve_vehicle(conn, vehicle)
        if not resolved:
            conn.close()
            return f"Vehicle '{vehicle}' not found."

    where  = ["section LIKE ?", "is_nav = 0"]
    params = [f"%{section}%"]
    if resolved:
        where.append("vehicle = ?")
        params.append(resolved)
    params.append(max(1, min(n, 200)))

    rows = conn.execute(
        f"SELECT * FROM pages WHERE {' AND '.join(where)} ORDER BY page_num LIMIT ?",
        params
    ).fetchall()
    conn.close()

    if not rows:
        return f"No pages found in section matching '{section}'."

    out = [f"{len(rows)} page(s) matching section '{section}':\n"]
    for r in rows:
        out.append(f"  [{r['page_num']:>5}]  {r['title']}")
        if r["breadcrumb"]:
            out.append(f"           {r['breadcrumb']}")
    return "\n".join(out)


# ─────────────────────────────────────────────
# Vehicle management tools
# ─────────────────────────────────────────────

@mcp.tool()
def add_vehicle_zip(
    zip_path: str,
    vehicle_name: str,
    folder_name: str | None = None,
) -> str:
    """Import and index a service manual from a local ZIP file.

    Supports ZIP archives from lemon-manuals.la and charm.li.
    Runs the full pipeline: extract → parse HTML → build FTS index.
    May take several minutes for large manuals (10 000+ pages).

    Args:
        zip_path:     Absolute path to the downloaded .zip file
        vehicle_name: Display label, e.g. "2003 Honda Civic EX L4-1.7L FWD"
        folder_name:  Folder name under sources/vehicles/ (auto-derived if omitted)
    """
    zp = Path(zip_path).expanduser().resolve()
    if not zp.exists():
        return f"File not found: {zp}"
    if zp.suffix.lower() != ".zip":
        return f"Expected a .zip file, got: {zp.name}"

    if not folder_name:
        folder_name = _dl.derive_folder_name(vehicle_name)

    job_id = _dl.create_job()
    buf    = io.StringIO()
    with redirect_stdout(buf):
        _dl.add_vehicle(vehicle_name, vehicle_folder=folder_name,
                        local_path=str(zp), job_id=job_id)

    job = _dl.get_job(job_id)
    log = "\n".join(job.get("messages", []))

    if job.get("error"):
        return f"Failed: {job['error']}\n\nLog:\n{log}"
    return f"Done. '{vehicle_name}' is now indexed and searchable.\n\nLog:\n{log}"


@mcp.tool()
def add_vehicle_url(
    url: str,
    vehicle_name: str,
    folder_name: str | None = None,
) -> str:
    """Download a service manual ZIP from a URL and index it.

    Supports charm.li direct download links and any public .zip URL.
    Runs the full pipeline: download → extract → parse HTML → build FTS index.
    May take several minutes depending on file size and network speed.

    Args:
        url:          Direct URL to the .zip file
        vehicle_name: Display label, e.g. "2014 Toyota Sienna XLE FWD"
        folder_name:  Folder name under sources/vehicles/ (auto-derived if omitted)
    """
    if not folder_name:
        folder_name = _dl.derive_folder_name(vehicle_name)

    job_id = _dl.create_job()
    buf    = io.StringIO()
    with redirect_stdout(buf):
        _dl.add_vehicle(vehicle_name, vehicle_folder=folder_name,
                        url=url, job_id=job_id)

    job = _dl.get_job(job_id)
    log = "\n".join(job.get("messages", []))

    if job.get("error"):
        return f"Failed: {job['error']}\n\nLog:\n{log}"
    return f"Done. '{vehicle_name}' is now indexed and searchable.\n\nLog:\n{log}"


@mcp.tool()
def reindex_vehicle(vehicle_name: str) -> str:
    """Re-parse and re-index an already-extracted vehicle from its source HTML files.

    Use when:
    - The parsing logic in build_index.py was updated
    - Pages were manually added or edited on disk
    - The FTS index became corrupted

    Does NOT re-download or re-extract the ZIP — the source files must exist.

    Args:
        vehicle_name: Vehicle name or slug (from list_vehicles)
    """
    err = _no_db()
    if err:
        return err

    conn = _conn()
    resolved = _resolve_vehicle(conn, vehicle_name)
    if not resolved:
        conn.close()
        return (f"Vehicle '{vehicle_name}' not found. "
                "Use list_vehicles() to see what's indexed.")

    row = conn.execute(
        "SELECT pages_dir FROM pages WHERE vehicle = ? AND pages_dir IS NOT NULL LIMIT 1",
        (resolved,)
    ).fetchone()
    conn.close()

    if not row or not row["pages_dir"]:
        return (f"No source directory recorded for '{resolved}'. "
                "The vehicle may have been indexed by an older version of build_index.py. "
                "Re-import the ZIP to reindex.")

    pages_dir = Path(row["pages_dir"])
    if not pages_dir.exists():
        return f"Source directory no longer exists: {pages_dir}"

    html_count = len(list(pages_dir.glob("*.html")))
    if html_count == 0:
        return f"No HTML files found in {pages_dir}"

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            _bi.index_vehicle(pages_dir, resolved)
    except SystemExit as exc:
        log = buf.getvalue()
        if exc.code != 0:
            return f"Reindex failed (exit {exc.code}).\n\nLog:\n{log}"

    return f"Done. '{resolved}' has been reindexed from {html_count} HTML pages.\n\nLog:\n{buf.getvalue()}"


@mcp.tool()
def delete_vehicle(
    vehicle_name: str,
    delete_files: bool = False,
) -> str:
    """Remove a vehicle from the search index.

    Args:
        vehicle_name: Vehicle name or slug (from list_vehicles)
        delete_files: Also delete the extracted HTML source files from disk.
                      Default False — only removes DB rows, keeps files intact.
    """
    err = _no_db()
    if err:
        return err

    conn = _conn()
    resolved = _resolve_vehicle(conn, vehicle_name)
    if not resolved:
        conn.close()
        return f"Vehicle '{vehicle_name}' not found."

    dir_row = conn.execute(
        "SELECT pages_dir FROM pages WHERE vehicle = ? LIMIT 1", (resolved,)
    ).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE vehicle = ?", (resolved,)
    ).fetchone()[0]

    conn.execute("DELETE FROM pages WHERE vehicle = ?", (resolved,))
    conn.commit()
    conn.close()

    out = [f"Removed '{resolved}' ({count} pages) from the search index."]

    if delete_files and dir_row and dir_row["pages_dir"]:
        import shutil
        vehicle_dir = Path(dir_row["pages_dir"]).parent
        try:
            vehicle_dir.relative_to(MANUAL_ROOT)
            inside = True
        except ValueError:
            inside = False

        if inside and vehicle_dir.exists():
            shutil.rmtree(vehicle_dir)
            out.append(f"Deleted source files: {vehicle_dir}")
        else:
            out.append("Source files not deleted — path is outside the managed directory.")

    return "\n".join(out)


@mcp.tool()
def database_stats() -> str:
    """Return database statistics: file size, vehicle list, content vs nav page counts."""
    if not DB_PATH.exists():
        return "Database not found. Run build_index.py to create it."

    conn = _conn()
    total   = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    content = conn.execute("SELECT COUNT(*) FROM pages WHERE is_nav = 0").fetchone()[0]
    rows    = conn.execute(
        "SELECT vehicle, COUNT(*) as cnt FROM pages GROUP BY vehicle ORDER BY vehicle"
    ).fetchall()
    conn.close()

    mb = DB_PATH.stat().st_size / 1024 / 1024
    out = [
        f"Database : {DB_PATH}",
        f"Size     : {mb:.1f} MB",
        f"Vehicles : {len(rows)}",
        f"Pages    : {total} total  ({content} content, {total - content} nav-only)",
        "",
        "Per vehicle:",
    ]
    for r in rows:
        out.append(f"  {r['cnt']:>6}  {r['vehicle']}")
    return "\n".join(out)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
