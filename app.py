#!/usr/bin/env python3
"""
app.py — 1988 F-350 7.3L IDI Service Manual Dashboard
Run: venv/bin/python3 app.py
Then open: http://localhost:5000
"""

import sqlite3
import re
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from bs4 import BeautifulSoup

app = Flask(__name__)

DB_PATH      = Path(__file__).parent / "truck_manual.db"
PAGES_DIR    = Path(__file__).parent.parent / "sources" / "1988 Ford F 350 2WD Pickup V8-7.3L DSL" / "pages"
MANUAL_ROOT  = PAGES_DIR.parent

# ─────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def do_search(query: str, n: int = 10, section: str = None, include_nav: bool = False):
    """Run FTS5 search and return list of result dicts."""
    conn = get_conn()

    where_parts = ["pages_fts MATCH ?"]
    params = [query]

    if not include_nav:
        where_parts.append("p.is_nav = 0")
    if section:
        where_parts.append("p.section LIKE ?")
        params.append(f"%{section}%")

    where_clause = " AND ".join(where_parts)
    params.append(n)

    sql = f"""
        SELECT p.page_num, p.title, p.breadcrumb, p.section,
               p.subsection, p.content, p.is_nav, rank
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


def get_sections():
    conn = get_conn()
    rows = conn.execute("""
        SELECT section, COUNT(*) as cnt
        FROM pages WHERE section != ''
        GROUP BY section ORDER BY cnt DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_page_data(page_num: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM pages WHERE page_num = ?", (page_num,)).fetchone()
    conn.close()
    return dict(row) if row else None


def make_snippet(content: str, query: str, length: int = 300) -> str:
    """Extract a snippet around the first query term match."""
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

    # Bold matching terms
    for term in terms:
        snippet = re.sub(
            f"({re.escape(term)})",
            r"<strong>\1</strong>",
            snippet,
            flags=re.IGNORECASE
        )

    return snippet


def render_page_html(page_num: int) -> str:
    """Extract and return the main content HTML from a manual page file."""
    html_file = PAGES_DIR / f"{page_num}.html"
    if not html_file.exists():
        return "<p>Page file not found.</p>"

    with open(html_file, "r", encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    main_div = soup.find("div", class_="main")
    if not main_div:
        return "<p>No content found.</p>"

    # Remove expand/collapse buttons
    for btn in main_div.find_all("button"):
        btn.decompose()

    # Rewrite internal page links to go through our viewer
    for a in main_div.find_all("a", href=True):
        href = a["href"]
        if href.endswith(".html") and not href.startswith("http"):
            page_match = re.search(r"(\d+)\.html", href)
            if page_match:
                a["href"] = f"/page/{page_match.group(1)}"

    # Rewrite image src paths to go through our static file server
    for img in main_div.find_all("img"):
        src = img.get("src", "")
        if src.startswith("../"):
            img["src"] = "/manual-static/" + src[3:]

    return str(main_div)


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    query   = request.args.get("q", "").strip()
    section = request.args.get("section", "").strip()
    page    = int(request.args.get("page", 1))
    n       = 15
    offset  = (page - 1) * n

    results  = []
    snippets = {}
    total    = 0

    if query:
        # Fetch a larger set and paginate manually
        all_results = do_search(query, n=100, section=section or None)
        total       = len(all_results)
        results     = all_results[offset:offset + n]

        for r in results:
            snippets[r["page_num"]] = make_snippet(r["content"], query)

    sections = get_sections()

    return render_template(
        "index.html",
        query=query,
        section=section,
        results=results,
        snippets=snippets,
        sections=sections,
        total=total,
        page=page,
        n=n,
        has_prev=page > 1,
        has_next=(offset + n) < total,
    )


@app.route("/page/<int:page_num>")
def view_page(page_num):
    data = get_page_data(page_num)
    if not data:
        abort(404)

    content_html = render_page_html(page_num)
    query = request.args.get("q", "")

    return render_template(
        "page.html",
        page=data,
        content_html=content_html,
        query=query,
    )


@app.route("/api/search")
def api_search():
    """JSON API endpoint for future JS/agent use."""
    query   = request.args.get("q", "").strip()
    n       = int(request.args.get("n", 10))
    section = request.args.get("section", "").strip() or None

    if not query:
        return jsonify({"error": "q parameter required"}), 400

    results = do_search(query, n=n, section=section)

    return jsonify({
        "query":   query,
        "count":   len(results),
        "results": [
            {
                "page_num":   r["page_num"],
                "title":      r["title"],
                "breadcrumb": r["breadcrumb"],
                "section":    r["section"],
                "snippet":    make_snippet(r["content"], query, 200),
                "url":        f"/page/{r['page_num']}",
            }
            for r in results
        ],
    })


@app.route("/api/page/<int:page_num>")
def api_page(page_num):
    """JSON API — return full page content for agent use."""
    data = get_page_data(page_num)
    if not data:
        return jsonify({"error": "Page not found"}), 404
    return jsonify(data)


@app.route("/manual-static/<path:filename>")
def serve_manual_static(filename):
    """Serve images and other assets from the manual directory."""
    return send_from_directory(MANUAL_ROOT, filename)


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("1988 F-350 Service Manual Dashboard")
    print("Open: http://localhost:5000")
    app.run(debug=True, port=5000)
