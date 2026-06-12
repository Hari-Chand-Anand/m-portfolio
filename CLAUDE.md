# HCA Portfolio — Developer Guide for Claude

## What This Project Is

A sales intelligence platform for **Hari Chand Anand & Co.** (industrial sewing machinery, India). It has three layers:

1. **Static HTML frontend** — catalog browsing, 3D models, installation map, AI chat (`rag.html`)
2. **Node.js backend** (`backend/server.js`) — serves catalog data, live pricing from Google Sheets, proxies chat to Python agent
3. **Python AI agent** (`python-agent/`) — LangGraph ReAct agent using Llama 3.3 70B (Groq), Postgres memory, 5 tools

---

## Architecture

```
Browser (HTML pages)
    ↕ REST
Node.js Express (port 3001)
    ├── GET  /api/machines              → products.json + data/machines.json merged (296 total)
    ├── GET  /api/machines/:id          → single machine full detail
    ├── GET  /api/price/:model          → live price from Google Sheets CSV
    ├── GET  /api/projects              → data/projects.json (3 factory lines)
    ├── GET  /api/installations/stats   → data/brand_model_city_counts_correct.json
    ├── POST /api/chat                  → proxies to Python agent
    ├── POST /api/login                 → returns JWT
    └── GET  /api/admin/logs            → JWT-protected query log
    ↕ HTTP
Python FastAPI agent (port 8000, Docker)
    ├── POST /chat    → LangGraph ReAct agent
    └── GET  /health
    ↕ Postgres (port 5432, Docker) — LangGraph checkpoint tables
```

---

## Data Files

| File | Contents | Update method |
|---|---|---|
| `products.json` | 242 machines (DUKE, DUKEJIA, 12 brands) | Manual edit |
| `data/machines.json` | 54 extended machines (EPA, DUKE R9, DUKEJIA DY 1202) | Manual edit |
| `data/projects.json` | 3 factory line setups | Manual edit |
| `data/installations.json` | 4 sample records only — not used by agent | Ignore |
| `data/brand_model_city_counts_correct.json` | 1,239 city/brand installation records — used by `get_installation_stats` tool | Manual edit |
| `data/hca_pricing.xlsx` | Pricing reference (NOT live) | Replaced by Google Sheet |

**Live pricing**: Set `SHEET_ID` in `backend/.env`. Node.js fetches the sheet as CSV on each `GET /api/price/:model` call.

---

## Running Locally

```bash
# 1. Start Postgres + Python agent
docker compose up -d --build

# 2. Start Node.js backend
node backend/server.js

# 3. Open index.html in browser, or rag.html for the AI chat
```

**Required: `backend/.env`**
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

---

## AI Agent — Expected Behaviour

The agent (`python-agent/agent.py`) must **always call tools** for catalog questions. It must never answer from its own memory.

### What correct behaviour looks like

| User asks | Agent must do |
|---|---|
| "What brands do you have?" | Call `search_machines(q="")`, return cards |
| "Show me DUKE machines" | Call `search_machines(brand="DUKE")`, return machine_list card |
| "What embroidery machines do you have?" | Call `search_machines(category="embroidery")` |
| "Price of DY-1201?" | Call `get_price(model="DY-1201")` (retry if needed) |
| "Tell me about the R9" | Call `search_machines(q="R9")`, then `get_machine_details` |
| "How many installations in Delhi?" | Call `get_installation_stats()` |
| "What factory lines do you offer?" | Call `get_projects()` |

### What broken behaviour looks like (never acceptable)
- Listing brands or machine models in plain text without calling a tool
- Saying "full range available" or "various models available" from memory
- Answering count/stats questions (installations, cities) without calling `get_installation_stats()`
- Pasting catalog PDF or video URLs in text (these are on cards)

### The #1 bug and its fix
**Bug**: System prompt listed brand names and stats as facts → LLM answered from memory instead of calling tools.

**Fix applied**:
- Model switched from `llama-4-scout-17b` → `llama-3.3-70b-versatile` (temperature=0). The 70B model is dramatically more reliable at tool use.
- System prompt now uses a **3-step chain-of-thought reasoning framework**: (1) classify query type, (2) look up tool decision table, (3) respond only after tool results are in hand. Brand names and hardcoded counts removed from the prompt entirely.

If the agent regresses to answering from memory, **do not add more facts to the system prompt**. Strengthen step 1 (the classification rule) instead.

---

## Tools (`python-agent/tools.py`)

| Tool | When to use | Key params |
|---|---|---|
| `search_machines` | Any catalog query | `q`, `brand`, `category` — all optional |
| `get_machine_details` | Full specs for one machine | `id` from search results |
| `get_price` | Price of any model | `model` — try multiple formats on failure |
| `get_projects` | Factory line setups | none |
| `get_installation_stats` | City/brand install presence | none |

All tools call the Node.js backend. `BASE` defaults to `http://localhost:3001` (set via `API_BASE` env var in Docker).

---

## Frontend Pages

| File | Purpose |
|---|---|
| `index.html` | Homepage |
| `machines.html` | Full catalog with filters |
| `machine.html` | Single machine detail |
| `rag.html` | AI chat — the main sales tool |
| `projects.html` | Factory line setups |
| `map.html` | Installation map |
| `exhibition.html` | 3D interactive display |

**Stale/duplicate files** (safe to delete): `prevmachines.html`, `machines(3).html`, `exhibition-machine1.html`, `test.html`

---

## Known Issues

1. **Dual pricing sources**: Google Sheet is live truth but `data/hca_pricing.xlsx` also exists. Keep the Sheet as source of truth; don't edit the xlsx for pricing updates.

2. **Split catalog**: `products.json` and `data/machines.json` are merged at runtime in server.js. If you add machines, check which file they belong to — don't duplicate across both.

3. **System prompt hardcoded counts** (historical): Previous versions hardcoded "296 machines", "1,239 installations" in the system prompt. These drift as data updates. Never re-add hardcoded catalog counts to the prompt.

4. **Log rotation**: `logs/query_log.jsonl` grows indefinitely. Add rotation if log size becomes a concern.

5. **Linux Docker**: `extra_hosts: host.docker.internal:host-gateway` is commented out in `docker-compose.yml`. Uncomment on Linux so the Python agent can reach the Node.js backend.

6. **No frontend auth**: Only `/api/admin/*` endpoints are JWT-protected. If pricing data is sensitive, consider adding auth to `/api/price/:model`.

---

## Card Types (Frontend Contract)

The Node.js `/api/chat` response includes a `cards` array. Frontend renders these:

| `type` | Contents |
|---|---|
| `machine_list` | `{ machines: [...] }` — list of machine objects |
| `machine_detail` | `{ machine: {...} }` — full spec of one machine |
| `price` | `{ model, quote_price_inr, currency }` |
| `project_list` | `{ projects: [...] }` — factory line objects |

The `answer` field is the LLM's text response (should be brief — one sentence intro). Cards carry the real data.
