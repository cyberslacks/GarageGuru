# GarageGuru

Factory service manual search with a brain. Import ZIPs from [lemon-manuals.la](https://lemon-manuals.la) or [charm.li](https://charm.li), search 10,000+ pages via web UI, and query it directly from Claude Code (MCP) or Ollama (OpenAI-compatible tools).

Multi-vehicle. Dark theme. Fast.

---

## Features

- **Full-text search** — SQLite FTS5 with three-tier fallback: exact phrase → proximity (NEAR/5) → all terms
- **Web UI** — search, browse by section, paginated results with highlighted snippets, full page viewer with working images and cross-reference links
- **Add vehicles via drag-and-drop** — drop a ZIP downloaded from lemon-manuals.la or charm.li; byte-level upload progress, real-time indexing log
- **MCP server** — 10 tools Claude Code can call natively (search, browse, get page, add/reindex/delete vehicle)
- **OpenAI-compatible tool endpoints** — use GarageGuru as a tool source from Ollama, Open WebUI, or any OpenAI function-calling client
- **CLI search** — `search.py` for terminal use without a browser
- **Run as a service** — systemd unit included

---

## Quick Start

### 1. Create the virtual environment

```bash
cd truck_agent
python3 -m venv venv
```

> If this fails with `ensurepip is not available`:
> ```bash
> sudo apt install python3.12-venv
> python3 -m venv venv
> ```

### 2. Install dependencies

```bash
venv/bin/pip install flask beautifulsoup4 mcp
```

### 3. Build the search index

Only needed once per vehicle. Parses all HTML pages and writes `truck_manual.db`.

```bash
venv/bin/python3 build_index.py
```

### 4. Launch the dashboard

```bash
venv/bin/python3 app.py
```

Open **http://localhost:5000** in your browser. Press `Ctrl+C` to stop.

---

## Adding Vehicles

### Via the web UI (recommended)

1. Go to **http://localhost:5000/add-vehicle**
2. Download a ZIP from [lemon-manuals.la](https://lemon-manuals.la) or [charm.li](https://charm.li)
3. Drop it on the upload zone or paste a direct download URL
4. Enter the vehicle name — the indexer runs in the background with a live log

### Via the CLI

```bash
venv/bin/python3 build_index.py \
  --source "sources/vehicles/2014_Toyota_Sienna/pages" \
  --vehicle "2014 Toyota Sienna XLE FWD"
```

Re-running for the same vehicle safely re-indexes it (removes old pages first).

To rebuild the entire database from scratch:

```bash
venv/bin/python3 build_index.py --rebuild
```

---

## MCP Server (Claude Code)

`manual_mcp.py` exposes GarageGuru as a native MCP server. Register it in `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "service-manual": {
      "command": "/path/to/truck_agent/venv/bin/python3",
      "args": ["/path/to/truck_agent/manual_mcp.py"]
    }
  }
}
```

### Available tools

| Tool | Description |
|------|-------------|
| `search` | FTS5 search with 3-tier fallback |
| `get_page` | Full content of a page by vehicle + page number |
| `list_vehicles` | All indexed vehicles with page counts |
| `list_sections` | Section breakdown, optionally filtered by vehicle |
| `browse_section` | All pages in a section ordered by page number |
| `add_vehicle_zip` | Index a local ZIP file |
| `add_vehicle_url` | Download and index from a URL |
| `reindex_vehicle` | Rebuild FTS index for an existing vehicle |
| `delete_vehicle` | Remove a vehicle from DB and disk |
| `database_stats` | Total pages, vehicles, DB file size |

---

## OpenAI-Compatible Tool Endpoints (Ollama / Open WebUI)

Two endpoints serve GarageGuru as a tool source for any OpenAI function-calling client:

```
GET  /v1/tools          — tool definitions in OpenAI format
POST /v1/tools/call     — execute a tool call
```

### Ollama example

```python
import requests, ollama

tools = requests.get("http://localhost:5000/v1/tools").json()

response = ollama.chat(
    model="llama3.2",
    messages=[{"role": "user", "content": "How do I test the oil pressure sender?"}],
    tools=tools,
)

for call in response.message.tool_calls or []:
    result = requests.post("http://localhost:5000/v1/tools/call", json={
        "name": call.function.name,
        "arguments": call.function.arguments,
    }).json()
    print(result["result"])
```

### Available tools via API

| Tool | Required args | Optional args |
|------|--------------|---------------|
| `search_manual` | `query` | `n`, `vehicle` (slug), `section` |
| `get_page` | `vehicle_slug`, `page_num` | — |
| `list_vehicles` | — | — |
| `list_sections` | — | `vehicle` (slug) |
| `browse_section` | `section` | `vehicle` (slug), `n` |

---

## JSON API

```
GET /api/search?q=<query>&n=<count>&section=<section>&vehicle=<slug>
GET /api/page/<vehicle_slug>/<page_num>
GET /api/vehicles
```

```bash
curl "http://localhost:5000/api/search?q=oil+pressure&n=5"
curl "http://localhost:5000/api/page/1988-ford-f-350-7-3l-diesel/1843"
curl "http://localhost:5000/api/vehicles"
```

---

## CLI Search

```bash
# Basic search
venv/bin/python3 search.py "oil pressure sender"

# More results
venv/bin/python3 search.py "fuel sender" --results 10

# Full page content
venv/bin/python3 search.py "glow plug relay" --full

# Filter by vehicle
venv/bin/python3 search.py "coolant temp" --vehicle "F-350"

# Filter by section
venv/bin/python3 search.py "temperature" --section "Cooling"

# View a specific page
venv/bin/python3 search.py --page 1843 --vehicle "F-350"

# List all vehicles
venv/bin/python3 search.py --vehicles

# List all sections
venv/bin/python3 search.py --sections
```

---

## Run as a Service (systemd)

```bash
sudo cp truck-agent.service /etc/systemd/system/garageguru.service
# Edit the paths in the unit file if your checkout is in a different location, then:
sudo systemctl daemon-reload
sudo systemctl enable --now garageguru
sudo journalctl -u garageguru -f
```

---

## Project Structure

```
truck_agent/
├── app.py                  Flask web app (UI + REST API + OpenAI tool endpoints)
├── build_index.py          Indexes a vehicle's HTML pages into SQLite FTS5
├── downloader.py           Downloads and unpacks ZIPs from charm.li / lemon-manuals.la
├── manual_mcp.py           MCP server (Claude Code native tool access)
├── search.py               CLI search tool
├── truck-agent.service     systemd unit file
├── truck_manual.db         SQLite FTS5 index (built locally, not in git)
├── venv/                   Python virtual environment (not in git)
├── templates/
│   ├── index.html          Search home + results
│   ├── page.html           Manual page viewer
│   └── add_vehicle.html    Add vehicle UI with drag-and-drop upload
├── static/
│   └── style.css           Dark automotive theme
└── sources/
    └── vehicles/           One folder per vehicle (HTML data not in git)
        ├── 1988_Ford_F-350_v8-7.3L_2WD_DSL/
        ├── 2004_Ford_F-150_V8-4.6L_4WD_GSL/
        ├── 2006_Toyota_Prius_I4-1.5L_FWD_GSL/
        └── 2014_Toyota_Sienna_XLE_v6-3.5L_FWD_GSL/
```

---

## Wire Color Reference (Ford)

`BK`=Black `R`=Red `W`=White `Y`=Yellow `LG`=Light Green `DG`=Dark Green `LB`=Light Blue `P`=Purple `O`=Orange `BR`=Brown `GY`=Gray `T`=Tan
