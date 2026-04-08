#!/usr/bin/env python3
"""
search.py — Search the 1988 F-350 service manual database.

Usage:
    python3 search.py "oil pressure warning light"
    python3 search.py "fuel sender resistance" --results 5
    python3 search.py "glow plug relay" --full
    python3 search.py "brake" --section "Brakes"
    python3 search.py --sections          # list all sections

Options:
    --results N     Number of results to return (default: 5)
    --full          Print full content of each result (default: truncated)
    --section NAME  Filter results to a specific section
    --sections      List all available sections and page counts
    --nav           Include navigation-only pages in results (default: excluded)
"""

import sqlite3
import sys
import argparse
import textwrap
from pathlib import Path

DB_PATH = Path(__file__).parent / "truck_manual.db"
PAGES_DIR = Path(__file__).parent.parent / "sources" / "1988 Ford F 350 2WD Pickup V8-7.3L DSL" / "pages"


def get_conn():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Run:  python3 build_index.py  first.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_sections():
    conn = get_conn()
    rows = conn.execute("""
        SELECT section, COUNT(*) as cnt
        FROM pages
        WHERE section != ''
        GROUP BY section
        ORDER BY cnt DESC
    """).fetchall()
    conn.close()

    print(f"\n{'Section':<50} {'Pages':>6}")
    print("-" * 58)
    for row in rows:
        print(f"{row['section']:<50} {row['cnt']:>6}")


def search(query: str, n_results: int = 5, section_filter: str = None,
           show_full: bool = False, include_nav: bool = False):

    conn = get_conn()

    # Build WHERE clause
    where_parts = ["pages_fts MATCH ?"]
    params = [query]

    if not include_nav:
        where_parts.append("p.is_nav = 0")
    if section_filter:
        where_parts.append("p.section LIKE ?")
        params.append(f"%{section_filter}%")

    where_clause = " AND ".join(where_parts)
    params.append(n_results)

    sql = f"""
        SELECT
            p.page_num,
            p.title,
            p.breadcrumb,
            p.section,
            p.subsection,
            p.content,
            p.is_nav,
            rank
        FROM pages_fts
        JOIN pages p ON pages_fts.rowid = p.id
        WHERE {where_clause}
        ORDER BY rank
        LIMIT ?
    """

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS5 query syntax error — try wrapping query in quotes
        try:
            params[0] = f'"{query}"'
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            print(f"Search error: {e}")
            conn.close()
            return

    conn.close()

    if not rows:
        print(f'\nNo results found for: "{query}"')
        if section_filter:
            print(f'  (filtered to section: {section_filter})')
        print('\nTips:')
        print('  - Try broader terms (e.g. "oil pressure" instead of "oil pressure warning lamp circuit")')
        print('  - Use --sections to see available section names')
        return

    print(f'\n=== Results for: "{query}" ({len(rows)} found) ===\n')

    for i, row in enumerate(rows, 1):
        print(f"[{i}] {row['title']}")
        print(f"    Path : {row['breadcrumb']}")
        print(f"    File : pages/{row['page_num']}.html")

        if show_full:
            print(f"    Content:")
            wrapped = textwrap.fill(row['content'], width=100,
                                    initial_indent="      ",
                                    subsequent_indent="      ")
            print(wrapped)
        else:
            # Show a snippet around the first keyword match
            content = row['content']
            snippet = content[:400] + ("..." if len(content) > 400 else "")
            wrapped = textwrap.fill(snippet, width=100,
                                    initial_indent="    Preview: ",
                                    subsequent_indent="             ")
            print(wrapped)

        print()


def get_page(page_num: int):
    """Fetch and display a specific page by number."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM pages WHERE page_num = ?", (page_num,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"Page {page_num} not found in database.")
        return

    print(f"\n{'='*70}")
    print(f"Title    : {row['title']}")
    print(f"Path     : {row['breadcrumb']}")
    print(f"Section  : {row['section']}")
    print(f"File     : pages/{row['page_num']}.html")
    print(f"{'='*70}")
    print()
    print(row['content'])
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Search the 1988 F-350 7.3L DSL service manual",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--results", "-n", type=int, default=5,
                        help="Number of results (default: 5)")
    parser.add_argument("--full", "-f", action="store_true",
                        help="Show full content of results")
    parser.add_argument("--section", "-s",
                        help="Filter to a specific section")
    parser.add_argument("--sections", action="store_true",
                        help="List all sections and page counts")
    parser.add_argument("--nav", action="store_true",
                        help="Include navigation-only pages")
    parser.add_argument("--page", "-p", type=int,
                        help="Show a specific page by number")

    args = parser.parse_args()

    if args.sections:
        list_sections()
    elif args.page is not None:
        get_page(args.page)
    elif args.query:
        search(
            query=args.query,
            n_results=args.results,
            section_filter=args.section,
            show_full=args.full,
            include_nav=args.nav,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
