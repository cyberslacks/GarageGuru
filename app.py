#!/usr/bin/env python3
"""
app.py — Service Manual Dashboard (multi-vehicle)
Run: venv/bin/python3 app.py
Then open: http://localhost:5000
"""

import sqlite3
import re
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from bs4 import BeautifulSoup

app = Flask(__name__)

DB_PATH     = Path(__file__).parent / "truck_manual.db"
MANUAL_ROOT = Path(__file__).parent.parent / "sources"


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


def slug_to_vehicle(slug: str) -> str | None:
    """Resolve a URL slug back to a vehicle name."""
    conn = get_conn()
    vehicles = conn.execute("SELECT DISTINCT vehicle FROM pages").fetchall()
    conn.close()
    for row in vehicles:
        if slugify(row["vehicle"]) == slug:
            return row["vehicle"]
    return None


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


def do_search(query: str, n: int = 10, section: str = None,
              vehicle: str = None, include_nav: bool = False):
    conn = get_conn()

    where_parts = ["pages_fts MATCH ?"]
    params = [query]

    if not include_nav:
        where_parts.append("p.is_nav = 0")
    if vehicle:
        where_parts.append("p.vehicle = ?")
        params.append(vehicle)
    if section:
        where_parts.append("p.section LIKE ?")
        params.append(f"%{section}%")

    where_clause = " AND ".join(where_parts)
    params.append(n)

    sql = f"""
        SELECT p.id, p.vehicle, p.page_num, p.title, p.breadcrumb,
               p.section, p.subsection, p.content, p.is_nav, rank
        FROM pages_fts
        JOIN pages p ON pages_fts.rowid = p.id
        WHERE {where_clause}
        ORDER BY rank
        LIMIT ?
    """

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        try:
            params[0] = f'"{query}"'
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            rows = []

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
        if href.endswith(".html") and not href.startswith("http"):
            page_match = re.search(r"(\d+)\.html", href)
            if page_match:
                a["href"] = f"/page/{v_slug}/{page_match.group(1)}"

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


@app.route("/manual-static/<path:filename>")
def serve_manual_static(filename):
    return send_from_directory(MANUAL_ROOT, filename)


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Service Manual Dashboard")
    print("Open: http://localhost:5000")
    app.run(debug=True, port=5000)
