#!/usr/bin/env python3
"""
app.py — Service Manual Dashboard (multi-vehicle)
Run: venv/bin/python3 app.py
Then open: http://localhost:5000
"""

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, abort, Response, stream_with_context
from bs4 import BeautifulSoup
from downloader import add_vehicle, create_job, get_job, derive_folder_name

app = Flask(__name__)

DB_PATH     = Path(__file__).parent / "truck_manual.db"
MANUAL_ROOT = Path(__file__).parent / "sources" / "vehicles"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def slugify(text: str) -> str:
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-16000")  # 16 MB page cache
    return conn


def get_vehicles():
    conn = get_conn()
    rows = conn.execute("""
        SELECT vehicle, COUNT(*) as cnt
        FROM pages GROUP BY vehicle ORDER BY vehicle
    """).fetchall()
    conn.close()
    return [{"vehicle": r["vehicle"], "slug": slugify(r["vehicle"]), "cnt": r["cnt"]}
            for r in rows]


_slug_map: dict[str, str] | None = None
_slug_map_lock = threading.Lock()


def _invalidate_vehicle_cache():
    global _slug_map
    with _slug_map_lock:
        _slug_map = None


def slug_to_vehicle(slug: str) -> str | None:
    """Resolve a URL slug back to a vehicle name (cached)."""
    global _slug_map
    with _slug_map_lock:
        if _slug_map is None:
            conn = get_conn()
            rows = conn.execute("SELECT DISTINCT vehicle FROM pages").fetchall()
            conn.close()
            _slug_map = {slugify(r["vehicle"]): r["vehicle"] for r in rows}
        return _slug_map.get(slug)


def get_sections(vehicle: str = None):
    conn = get_conn()
    if vehicle:
        rows = conn.execute("""
            SELECT section, COUNT(*) as cnt
            FROM pages WHERE section != '' AND vehicle = ?
            GROUP BY section ORDER BY cnt DESC
        """, (vehicle,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT section, COUNT(*) as cnt
            FROM pages WHERE section != ''
            GROUP BY section ORDER BY cnt DESC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _run_fts(conn, fts_query: str, n: int, vehicle: str,
             section: str, include_nav: bool) -> list:
    """Execute one FTS query and return rows."""
    where_parts = ["pages_fts MATCH ?"]
    params = [fts_query]

    if not include_nav:
        where_parts.append("p.is_nav = 0")
    if vehicle:
        where_parts.append("p.vehicle = ?")
        params.append(vehicle)
    if section:
        where_parts.append("p.section LIKE ?")
        params.append(f"%{section}%")

    params.append(n)
    sql = f"""
        SELECT p.id, p.vehicle, p.page_num, p.title, p.breadcrumb,
               p.section, p.subsection, p.content, p.is_nav, rank
        FROM pages_fts
        JOIN pages p ON pages_fts.rowid = p.id
        WHERE {" AND ".join(where_parts)}
        ORDER BY rank
        LIMIT ?
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def do_search(query: str, n: int = 10, section: str = None,
              vehicle: str = None, include_nav: bool = False):
    """
    Search with a three-tier fallback for multi-word queries:
      1. Exact phrase  — "injection pump"   (words must be adjacent)
      2. Proximity     — injection NEAR/5 pump  (words within 5 tokens)
      3. All terms     — injection pump          (words anywhere on the page)

    Single-word queries skip to tier 3 directly.
    Results from higher tiers are returned first; lower-tier results are
    appended only to fill up to n without duplicating.
    """
    conn = get_conn()
    words = query.strip().split()
    seen_ids = set()
    results = []

    def add_rows(rows):
        for r in rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                results.append(dict(r))

    if len(words) > 1:
        # Tier 1 — exact phrase
        phrase = f'"{query}"'
        add_rows(_run_fts(conn, phrase, n, vehicle, section, include_nav))

        # Tier 2 — proximity (words within 5 tokens of each other)
        if len(results) < n:
            near = " NEAR/5 ".join(words)
            add_rows(_run_fts(conn, near, n, vehicle, section, include_nav))

    # Tier 3 — all terms anywhere (standard FTS)
    if len(results) < n:
        add_rows(_run_fts(conn, query, n, vehicle, section, include_nav))

    conn.close()
    return results[:n]


def do_browse(section: str, vehicle: str = None, n: int = 100) -> list:
    """Return non-nav pages in a section, ordered by page number."""
    conn = get_conn()
    where = ["section LIKE ?", "is_nav = 0"]
    params: list = [f"%{section}%"]
    if vehicle:
        where.append("vehicle = ?")
        params.append(vehicle)
    params.append(n)
    rows = conn.execute(
        f"SELECT * FROM pages WHERE {' AND '.join(where)} ORDER BY page_num LIMIT ?",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_page_data(vehicle: str, page_num: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM pages WHERE vehicle = ? AND page_num = ?",
        (vehicle, page_num)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def make_snippet(content: str, query: str, length: int = 300) -> str:
    if not content:
        return ""

    terms = [t.lower() for t in query.split() if len(t) > 2]
    lower_content = content.lower()

    best_pos = 0
    for term in terms:
        pos = lower_content.find(term)
        if pos != -1:
            best_pos = max(0, pos - 80)
            break

    snippet = content[best_pos:best_pos + length]
    if best_pos > 0:
        snippet = "..." + snippet
    if best_pos + length < len(content):
        snippet = snippet + "..."

    for term in terms:
        snippet = re.sub(
            f"({re.escape(term)})",
            r"<strong>\1</strong>",
            snippet,
            flags=re.IGNORECASE,
        )

    return snippet


def render_page_html(vehicle: str, page_num: int, pages_dir: str = None) -> str:
    """Extract and return the main content HTML from a manual page file."""
    if pages_dir:
        vehicle_dir = Path(pages_dir)
    else:
        # Fallback: search recursively (should not normally be needed)
        vehicle_dir = None
        for pd in MANUAL_ROOT.rglob("pages"):
            if pd.is_dir() and (pd / f"{page_num}.html").exists():
                vehicle_dir = pd
                break

    if vehicle_dir is None or not vehicle_dir.exists():
        return "<p>Page file not found.</p>"

    html_file = vehicle_dir / f"{page_num}.html"
    with open(html_file, "r", encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    main_div = soup.find("div", class_="main")
    if not main_div:
        return "<p>No content found.</p>"

    for btn in main_div.find_all("button"):
        btn.decompose()

    # Rewrite internal links → /page/<vehicle_slug>/<num>
    v_slug = slugify(vehicle)
    for a in main_div.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http"):
            continue
        # Split off fragment (#section-name) before checking extension
        fragment = ""
        if "#" in href:
            href, fragment = href.split("#", 1)
            fragment = "#" + fragment
        if href.endswith(".html"):
            page_match = re.search(r"(\d+)\.html", href)
            if page_match:
                a["href"] = f"/page/{v_slug}/{page_match.group(1)}{fragment}"

    # Rewrite image paths → /manual-static/<relative-to-MANUAL_ROOT>/...
    for img in main_div.find_all("img"):
        src = img.get("src", "")
        if src.startswith("../"):
            abs_path = (vehicle_dir / src).resolve()
            try:
                rel = abs_path.relative_to(MANUAL_ROOT)
                img["src"] = f"/manual-static/{rel}"
            except ValueError:
                pass

    return str(main_div)


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    query        = request.args.get("q", "").strip()
    section      = request.args.get("section", "").strip()
    vehicle_slug = request.args.get("vehicle", "").strip()
    page         = int(request.args.get("page", 1))
    n            = 15
    offset       = (page - 1) * n

    # Resolve slug → vehicle name
    vehicle = slug_to_vehicle(vehicle_slug) if vehicle_slug else None

    results  = []
    snippets = {}
    total    = 0

    if query:
        all_results = do_search(query, n=100, section=section or None,
                                vehicle=vehicle or None)
        total   = len(all_results)
        results = all_results[offset:offset + n]
        for r in results:
            snippets[r["page_num"]] = make_snippet(r["content"], query)
    elif section:
        all_results = do_browse(section, vehicle=vehicle or None)
        total   = len(all_results)
        results = all_results[offset:offset + n]

    vehicles = get_vehicles()
    sections = get_sections(vehicle=vehicle)

    return render_template(
        "index.html",
        query=query,
        section=section,
        vehicle_slug=vehicle_slug,
        vehicle_name=vehicle,
        results=results,
        snippets=snippets,
        sections=sections,
        vehicles=vehicles,
        total=total,
        page=page,
        n=n,
        has_prev=page > 1,
        has_next=(offset + n) < total,
        slugify=slugify,
    )


@app.route("/page/<vehicle_slug>/<int:page_num>")
def view_page(vehicle_slug, page_num):
    vehicle = slug_to_vehicle(vehicle_slug)
    if not vehicle:
        abort(404)

    data = get_page_data(vehicle, page_num)
    if not data:
        abort(404)

    content_html = render_page_html(vehicle, page_num, pages_dir=data.get("pages_dir"))
    query = request.args.get("q", "")

    return render_template(
        "page.html",
        page=data,
        vehicle_slug=vehicle_slug,
        content_html=content_html,
        query=query,
    )


@app.route("/api/search")
def api_search():
    query        = request.args.get("q", "").strip()
    n            = int(request.args.get("n", 10))
    section      = request.args.get("section", "").strip() or None
    vehicle_slug = request.args.get("vehicle", "").strip()
    vehicle      = slug_to_vehicle(vehicle_slug) if vehicle_slug else None

    if not query:
        return jsonify({"error": "q parameter required"}), 400

    results = do_search(query, n=n, section=section, vehicle=vehicle)

    return jsonify({
        "query":   query,
        "count":   len(results),
        "results": [
            {
                "page_num":   r["page_num"],
                "vehicle":    r["vehicle"],
                "title":      r["title"],
                "breadcrumb": r["breadcrumb"],
                "section":    r["section"],
                "snippet":    make_snippet(r["content"], query, 200),
                "url":        f"/page/{slugify(r['vehicle'])}/{r['page_num']}",
            }
            for r in results
        ],
    })


@app.route("/api/page/<vehicle_slug>/<int:page_num>")
def api_page(vehicle_slug, page_num):
    vehicle = slug_to_vehicle(vehicle_slug)
    if not vehicle:
        return jsonify({"error": "Vehicle not found"}), 404
    data = get_page_data(vehicle, page_num)
    if not data:
        return jsonify({"error": "Page not found"}), 404
    return jsonify(data)


@app.route("/api/vehicles")
def api_vehicles():
    return jsonify(get_vehicles())


@app.route("/manual-static/<path:filename>")
def serve_manual_static(filename):
    return send_from_directory(MANUAL_ROOT, filename)


# ─────────────────────────────────────────────
# Add Vehicle
# ─────────────────────────────────────────────

@app.route("/add-vehicle", methods=["GET"])
def add_vehicle_page():
    return render_template("add_vehicle.html")


@app.route("/add-vehicle/start", methods=["POST"])
def add_vehicle_start():
    url          = request.form.get("url", "").strip() or None
    local_path   = request.form.get("local_path", "").strip() or None
    vehicle_name = request.form.get("vehicle_name", "").strip()
    folder_name  = request.form.get("folder_name", "").strip() or None

    if not vehicle_name:
        return jsonify({"error": "vehicle_name is required"}), 400
    if not url and not local_path:
        return jsonify({"error": "Provide either a download URL or a local file path"}), 400

    if not folder_name:
        folder_name = derive_folder_name(vehicle_name)

    job_id = create_job()

    def _run():
        add_vehicle(vehicle_name, vehicle_folder=folder_name,
                    url=url, local_path=local_path, job_id=job_id)
        _invalidate_vehicle_cache()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "folder": folder_name})


@app.route("/add-vehicle/progress/<job_id>")
def add_vehicle_progress(job_id):
    """Server-Sent Events stream for job progress."""
    def generate():
        last_idx = 0
        while True:
            job = get_job(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            messages = job.get("messages", [])
            for msg in messages[last_idx:]:
                yield f"data: {json.dumps({'msg': msg})}\n\n"
            last_idx = len(messages)

            if job.get("done"):
                error = job.get("error")
                if error:
                    yield f"data: {json.dumps({'done': True, 'error': error})}\n\n"
                else:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                break

            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Service Manual Dashboard")
    print("Open: http://localhost:5000")
    app.run(debug=True, port=5000)
