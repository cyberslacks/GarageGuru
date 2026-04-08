#!/usr/bin/env python3
"""
search.py — Search the service manual database.

Usage:
    python3 search.py "oil pressure warning light"
    python3 search.py "fuel sender resistance" --results 5
    python3 search.py "glow plug relay" --full
    python3 search.py "brake" --section "Brakes"
    python3 search.py "coolant" --vehicle "2014 Toyota Sienna XLE FWD"
    python3 search.py --sections          # list all sections
    python3 search.py --vehicles          # list indexed vehicles

Options:
    --results N       Number of results to return (default: 5)
    --full            Print full content of each result (default: truncated)
    --section NAME    Filter results to a specific section
    --vehicle NAME    Filter results to a specific vehicle
    --sections        List all available sections and page counts
    --vehicles        List all indexed vehicles
    --nav             Include navigation-only pages in results (default: excluded)
"""

import sqlite3
import sys
import argparse
import textwrap
from pathlib import Path

DB_PATH = Path(__file__).parent / "truck_manual.db"


def get_conn():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Run:  python3 build_index.py  first.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_vehicles():
    conn = get_conn()
    rows = conn.execute("""
        SELECT vehicle, COUNT(*) as cnt
        FROM pages GROUP BY vehicle ORDER BY vehicle
    """).fetchall()
    conn.close()

    print(f"\n{'Vehicle':<55} {'Pages':>6}")
    print("-" * 63)
    for row in rows:
        print(f"{row['vehicle']:<55} {row['cnt']:>6}")


def list_sections(vehicle_filter: str = None):
    conn = get_conn()
    if vehicle_filter:
        rows = conn.execute("""
            SELECT section, COUNT(*) as cnt
            FROM pages WHERE section != '' AND vehicle LIKE ?
            GROUP BY section ORDER BY cnt DESC
        """, (f"%{vehicle_filter}%",)).fetchall()
    else:
        rows = conn.execute("""
            SELECT section, COUNT(*) as cnt
            FROM pages WHERE section != ''
            GROUP BY section ORDER BY cnt DESC
        """).fetchall()
    conn.close()

    print(f"\n{'Section':<50} {'Pages':>6}")
    print("-" * 58)
    for row in rows:
        print(f"{row['section']:<50} {row['cnt']:>6}")


def search(query: str, n_results: int = 5, section_filter: str = None,
           vehicle_filter: str = None, show_full: bool = False,
           include_nav: bool = False):

    conn = get_conn()

    where_parts = ["pages_fts MATCH ?"]
    params = [query]

    if not include_nav:
        where_parts.append("p.is_nav = 0")
    if vehicle_filter:
        where_parts.append("p.vehicle LIKE ?")
        params.append(f"%{vehicle_filter}%")
    if section_filter:
        where_parts.append("p.section LIKE ?")
        params.append(f"%{section_filter}%")

    where_clause = " AND ".join(where_parts)
    params.append(n_results)

    sql = f"""
        SELECT
            p.page_num,
            p.vehicle,
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
        if vehicle_filter:
            print(f'  (filtered to vehicle: {vehicle_filter})')
        if section_filter:
            print(f'  (filtered to section: {section_filter})')
        print('\nTips:')
        print('  - Try broader terms')
        print('  - Use --vehicles to see indexed vehicles')
        print('  - Use --sections to see available section names')
        return

    print(f'\n=== Results for: "{query}" ({len(rows)} found) ===\n')

    for i, row in enumerate(rows, 1):
        print(f"[{i}] {row['title']}")
        print(f"    Vehicle: {row['vehicle']}")
        print(f"    Path   : {row['breadcrumb']}")
        print(f"    File   : pages/{row['page_num']}.html")

        if show_full:
            print(f"    Content:")
            wrapped = textwrap.fill(row['content'], width=100,
                                    initial_indent="      ",
                                    subsequent_indent="      ")
            print(wrapped)
        else:
            content = row['content']
            snippet = content[:400] + ("..." if len(content) > 400 else "")
            wrapped = textwrap.fill(snippet, width=100,
                                    initial_indent="    Preview: ",
                                    subsequent_indent="             ")
            print(wrapped)

        print()


def get_page(page_num: int, vehicle_filter: str = None):
    """Fetch and display a specific page by number."""
    conn = get_conn()
    if vehicle_filter:
        row = conn.execute(
            "SELECT * FROM pages WHERE page_num = ? AND vehicle LIKE ?",
            (page_num, f"%{vehicle_filter}%")
        ).fetchone()
    else:
        # If multiple vehicles, just return the first match
        row = conn.execute(
            "SELECT * FROM pages WHERE page_num = ? LIMIT 1", (page_num,)
        ).fetchone()
    conn.close()

    if not row:
        print(f"Page {page_num} not found in database.")
        if not vehicle_filter:
            print("  Tip: use --vehicle to specify which vehicle's page to show.")
        return

    print(f"\n{'='*70}")
    print(f"Vehicle  : {row['vehicle']}")
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
        description="Search the service manual database",
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
    parser.add_argument("--vehicle", "-v",
                        help="Filter to a specific vehicle (partial match)")
    parser.add_argument("--sections", action="store_true",
                        help="List all sections and page counts")
    parser.add_argument("--vehicles", action="store_true",
                        help="List all indexed vehicles")
    parser.add_argument("--nav", action="store_true",
                        help="Include navigation-only pages")
    parser.add_argument("--page", "-p", type=int,
                        help="Show a specific page by number")

    args = parser.parse_args()

    if args.vehicles:
        list_vehicles()
    elif args.sections:
        list_sections(vehicle_filter=args.vehicle)
    elif args.page is not None:
        get_page(args.page, vehicle_filter=args.vehicle)
    elif args.query:
        search(
            query=args.query,
            n_results=args.results,
            section_filter=args.section,
            vehicle_filter=args.vehicle,
            show_full=args.full,
            include_nav=args.nav,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
