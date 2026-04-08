# 1988 F-350 7.3L IDI Service Manual — Web Dashboard

Full-text search interface for the factory service manual. 10,000+ indexed pages, searchable by keyword with section filtering.

---

## First-Time Setup

### 1. Create the virtual environment

```bash
cd truck_agent
python3 -m venv venv
```

> If this fails with `ensurepip is not available`, install the venv package first:
> ```bash
> sudo apt install python3.12-venv
> python3 -m venv venv
> ```

### 2. Install dependencies

```bash
venv/bin/pip install flask beautifulsoup4
```

### 3. Build the search index

Only needed once. Parses all HTML pages and writes `truck_manual.db` (~29 MB).

```bash
venv/bin/python3 build_index.py
```

Output will show progress and confirm how many pages were indexed (~10,700).

---

## Launching the Dashboard

```bash
venv/bin/python3 app.py
```

Then open **http://localhost:5000** in your browser.

To stop the server press `Ctrl+C`.

---

## Using the Interface

### Home page

- **Search bar** — type any term and press Enter or click the search icon
- **Quick searches** — one-click common searches (oil pressure, glow plug relay, fuel gauge sender, etc.)
- **Browse by Section** — click any section card to browse all pages in that section

### Search results

- Results are ranked by relevance; matching terms are **highlighted in amber**
- The green path below each result shows where the page sits in the manual hierarchy
- **Sidebar** — filter results to a specific section (Electrical, Engine, Brakes, etc.)
- **Pagination** — Previous / Next buttons appear when there are more than 15 results

### Viewing a page

- Click any result title to open the full manual page
- The sidebar shows the page's location in the manual (breadcrumb), page number, and section
- **Prev page / Next page** buttons step through adjacent pages in the manual
- Images and wiring diagrams are served directly from the source files
- Internal manual links work — clicking a cross-reference opens that page in the viewer
- The **Back to results** button returns to your last search

---

## Command-Line Search (no browser needed)

```bash
# Basic search
venv/bin/python3 search.py "oil pressure warning light"

# More results
venv/bin/python3 search.py "fuel sender" --results 10

# Show full page content
venv/bin/python3 search.py "glow plug relay" --full

# Filter to a section
venv/bin/python3 search.py "temperature" --section "Cooling"

# View a specific page by number
venv/bin/python3 search.py --page 1843

# List all available sections
venv/bin/python3 search.py --sections
```

---

## JSON API

The app exposes a simple API for programmatic access or future agent use.

```
GET /api/search?q=<query>&n=<count>&section=<section>
GET /api/page/<page_num>
```

Examples:

```bash
curl "http://localhost:5000/api/search?q=oil+pressure&n=5"
curl "http://localhost:5000/api/page/1843"
```

---

## Project Structure

```
truck_agent/
├── app.py              Flask web application
├── build_index.py      One-time database builder
├── search.py           CLI search tool
├── truck_manual.db     SQLite FTS5 index (built by build_index.py)
├── CLAUDE.md           Instructions for AI troubleshooting assistant
├── venv/               Python virtual environment
├── templates/
│   ├── index.html      Home + search results page
│   └── page.html       Manual page viewer
└── static/
    └── style.css       Dark automotive theme

../sources/
└── 1988 Ford F 350 2WD Pickup V8-7.3L DSL/
    └── pages/          10,700+ HTML source pages from the factory manual
```
