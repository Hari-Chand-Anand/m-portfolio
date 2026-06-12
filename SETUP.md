# HCA Portfolio — Setup & Run Instructions

## What Was Built

Three things were added/fixed in this session:

1. **Agent fix** — removed brand name hints from tool descriptions and hardened the system prompt so the agent always calls a tool before answering, never from memory.
2. **PDF catalog RAG** — ingestion pipeline that downloads catalog PDFs from Google Drive, extracts text, and stores it in Postgres for full-text search. A new `search_catalog_docs` tool lets the agent answer technical spec questions from actual catalog content.
3. **Thread sidebar** — ChatGPT-style sidebar in `rag.html` showing past conversations. Click any thread to resume it. Toggle to collapse.

---

## Prerequisites

- Node.js (v18+)
- Python 3.10+
- Docker Desktop running
- Google Drive catalog PDFs shared as "Anyone with the link can view"

---

## First-Time Setup

### 1. Install Node.js dependencies

```bash
cd backend
npm install
```

This picks up the new `pg` package added for Postgres connectivity.

### 2. Install Python ingestion dependencies

Run this from the project root (not inside Docker):

```bash
pip install pdfplumber gdown psycopg2-binary
```

### 3. Start Postgres + Python agent via Docker

```bash
docker compose up -d --build
```

The `--build` flag is required this time because `tools.py` and `agent.py` were updated. After the first build, you can use `docker compose up -d` for subsequent starts.

Verify both containers are healthy:

```bash
docker compose ps
```

Both `hca_postgres` and `hca_python_agent` should show `running`.

### 4. Start the Node.js backend

```bash
node backend/server.js
```

You should see: `✅ Backend running at http://localhost:3001`

### 5. Ingest catalog PDFs

Run the ingestion script from the project root. Start with a small test batch first:

```bash
# Test with 5 EPA catalogs first
python ingest_catalogs.py --brand EPA --limit 5

# Once confirmed working, ingest everything
python ingest_catalogs.py
```

The script will:
- Download each PDF from Google Drive
- Extract text with pdfplumber
- Store chunks in Postgres (`catalog_docs` table)
- Skip already-ingested entries on subsequent runs

**Options:**

| Flag | Description |
|---|---|
| `--brand NAME` | Only ingest one brand (e.g. `--brand DUKEJIA`) |
| `--limit N` | Process at most N entries |
| `--force` | Re-ingest even already-processed entries |

**You do not need to keep the PDFs.** The script downloads each one temporarily, extracts the text, stores it in Postgres, then discards the file. After ingestion, the agent searches Postgres — not Google Drive.

Re-run `ingest_catalogs.py` only when you add new catalog PDFs to Google Drive.

---

## Daily Start (After First-Time Setup)

```bash
# 1. Start containers
docker compose up -d

# 2. Start backend
node backend/server.js

# 3. Open rag.html in browser
```

---

## Environment Variables (`backend/.env`)

```env
GROQ_API_KEY=gsk_...
SHEET_ID=your_google_sheet_id
SHEET_GID=0
PORT=3001
ADMIN_PASSWORD=...
JWT_SECRET=...
PYTHON_AGENT_URL=http://localhost:8000
DATABASE_URL=postgresql://hca:hca_secret@localhost:5432/hca_agent
```

`DATABASE_URL` is now used by both the Python agent (LangGraph checkpoints) and the Node.js backend (catalog docs search + thread listing).

---

## API Endpoints (New)

### `GET /api/catalog-docs/search`

Searches technical documentation extracted from catalog PDFs.

Query params:
- `query` (required) — search term e.g. `stitch length`, `needle type`
- `model` (optional) — narrow to a specific model e.g. `DY-1201`

Returns up to 5 ranked text chunks.

### `GET /api/threads`

Lists all past conversation threads stored in Postgres by LangGraph.

Returns: `[{ thread_id, title, last_active }]` sorted newest first.

---

## Agent Behaviour

The agent uses **LangGraph ReAct** (keep this — it is the right framework for tool-use agents). The bug was not the framework; it was that tool docstrings contained brand names and category hints, giving the LLM enough context to answer from training data instead of calling tools.

**Fixed:** tool docstrings are now generic. The system prompt opens with a hard rule — the agent treats its own catalog knowledge as zero and must call a tool for every catalog question.

**Tool decision table (for reference):**

| User asks about | Tool called |
|---|---|
| Brands / what machines you carry | `search_machines(q="")` |
| Machines by brand | `search_machines(brand="...")` |
| Machines by type/category | `search_machines(category="...")` |
| Specific model | `search_machines(q="...")` |
| Full specs of one machine | `get_machine_details(id)` |
| Price / cost | `get_price(machine_model="...")` |
| Factory line setups | `get_projects()` |
| City / installation presence | `get_installation_stats()` |
| Technical specs from catalog PDF | `search_catalog_docs(query, model)` |

---

## Thread Sidebar

The sidebar in `rag.html` loads from `/api/threads` on page open and refreshes after every reply.

- **Click a thread** to resume it (resets the chat view, keeps the `thread_id`)
- **`+ New` button** starts a fresh conversation
- **Toggle button** (`‹` / `›`) on the left edge collapses/expands the sidebar — preference is saved in `localStorage`

---

## Linux Docker Note

If running on Linux, uncomment these lines in `docker-compose.yml` so the Python agent can reach the Node.js backend:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

---

## Known Issues

- **Log rotation:** `logs/query_log.jsonl` grows indefinitely. Rotate manually if it gets large.
- **Dual pricing sources:** `data/hca_pricing.xlsx` exists but is not used. Google Sheet is the live source. Do not edit the xlsx for pricing updates.
- **Split catalog:** `products.json` (242 machines) and `data/machines.json` (54 machines) are merged at runtime. When adding machines, don't duplicate across both files.
