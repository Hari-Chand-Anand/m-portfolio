# System Architecture — HCA Assistant

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT (Browser)                         │
│                                                                 │
│   rag.html                                                      │
│   ├── Chat UI (vanilla JS)                                      │
│   ├── thread_id in localStorage (survives page refresh)         │
│   ├── Turn badge + New Chat button                              │
│   └── Card renderer (machine cards, price cards)                │
└──────────────────────────┬──────────────────────────────────────┘
                           │ POST /api/chat
                           │ { message, thread_id }
┌──────────────────────────▼──────────────────────────────────────┐
│                    BACKEND (Node.js / Express)                  │
│                       localhost:3001                            │
│                                                                 │
│   server.js                                                     │
│   ├── CORS middleware (allows origin: null for file://)         │
│   ├── POST /api/chat  ──► proxies to Python agent              │
│   ├── GET  /api/machines         ──► products.json             │
│   ├── GET  /api/machines/:id     ──► products.json             │
│   ├── GET  /api/price/:model     ──► Google Sheets (live CSV)  │
│   ├── POST /api/login            ──► JWT auth                  │
│   └── GET  /api/admin/logs       ──► logs/query_log.jsonl      │
└──────────┬──────────────────────────────────────────────────────┘
           │ POST /chat { message, thread_id }
           ▼
┌──────────────────────────────────────────────────────────────────┐
│                  Python Agent (FastAPI)                          │
│                     localhost:8000  [Docker]                    │
│                                                                 │
│   main.py                                                       │
│   └── POST /chat  ──► agent.py                                 │
│                                                                 │
│   agent.py                                                      │
│   ├── LangGraph create_react_agent (ReAct loop)                 │
│   ├── PostgresSaver checkpointer                                │
│   │   └── loads/saves full thread state per thread_id          │
│   └── extract_cards() — current turn tool messages only        │
│                                                                 │
│   tools.py                                                      │
│   ├── search_machines  ──► GET localhost:3001/api/machines      │
│   ├── get_machine_details ──► GET localhost:3001/api/machines/:id│
│   └── get_price  ──► GET localhost:3001/api/price/:model        │
└──────────┬───────────────────────────────┬──────────────────────┘
           │                               │
           ▼                               ▼
┌──────────────────┐          ┌─────────────────────────┐
│   Groq Cloud API │          │   PostgreSQL [Docker]   │
│                  │          │   localhost:5432         │
│   Llama 4 Scout  │          │                          │
│   17B instruct   │          │   LangGraph checkpoint   │
│   Tool-use       │          │   tables — full thread   │
│   enabled        │          │   history per thread_id  │
└──────────────────┘          └─────────────────────────┘
                                          │
                              ┌───────────▼────────────┐
                              │   Google Sheets        │
                              │   (live pricing CSV)   │
                              │   241 rows             │
                              └────────────────────────┘
```

---

## Is This RAG?

**No.** Despite the filename `rag.html`, this is a **tool-calling agent**, not RAG.

| RAG | This system |
|---|---|
| Embeds documents into a vector store | Calls structured REST API endpoints |
| Retrieves top-K semantic chunks | Gets exact, filtered machine data |
| Works best on unstructured text | Works on a structured JSON catalog |
| Stale if embeddings aren't refreshed | Always reads live `products.json` |
| Prices require a separate pipeline | `get_price` hits Google Sheets live |

The catalog is a queryable API, not a document store. Tool-calling gives the LLM exact data — no embedding drift, no stale chunks. The tradeoff: the agent can only answer what the catalog contains. Sparse machine descriptions → thin answers.

---

## Component Map

| Component | File | Role |
|---|---|---|
| Chat UI | `rag.html` | User interface — input, messages, card rendering, thread persistence |
| API Server | `backend/server.js` | Express routes, CORS, CSV parser, price matching, proxy to Python agent |
| Python Agent | `python-agent/main.py` | FastAPI app — `/health` + `/chat` |
| LangGraph Agent | `python-agent/agent.py` | ReAct graph, PostgresSaver, card extraction |
| Tools | `python-agent/tools.py` | Three tools calling Node.js API via httpx |
| Catalog | `products.json` | 242 machines — specs, media URLs, categories |
| Pricing | Google Sheets | Live quote prices, base prices, FX overrides |
| Persistence | PostgreSQL (Docker) | Full conversation history per thread_id |
| Infrastructure | `docker-compose.yml` | postgres + python-agent services |
| Pre-push hook | `.githooks/pre-push` | Secret scan + smoke tests before every push |
| Styles | `assets/styles.css` | Shared dark theme design tokens |

---

## API Endpoints

### `POST /api/chat` (Node.js — proxies to Python agent)
**Request:**
```json
{ "message": "price of DY-1201?", "thread_id": "abc-123" }
```
**Response:**
```json
{
  "answer": "The price of DY-1201 is ₹5,06,000.",
  "cards": [{ "type": "price", "model": "DY-1201", "quote_price_inr": 506000, "currency": "INR" }],
  "thread_id": "abc-123",
  "history": []
}
```
`thread_id` is generated (UUID) on the first message, then echoed back every turn. State lives in Postgres — `history[]` is empty and kept only for rollback compatibility.

---

### `GET /api/machines`
| Query Param | Example | Effect |
|---|---|---|
| `q` | `?q=embroidery` | Keyword search on name, model, description, tags |
| `brand` | `?brand=DUKEJIA` | Exact brand filter |
| `category` | `?category=lockstitch` | Exact category filter |

Available categories: `embroidery`, `lockstitch`, `finishing`, `industrial`, `sewing`

---

### `GET /api/price/:model`
```json
{ "model": "DY-1201", "quote_price_inr": 506000, "currency": "INR" }
```
Matching order: exact key → exact model → contains key → contains model.

---

### `POST /chat` (Python agent — internal)
```json
{ "message": "...", "thread_id": "abc-123" }
```
```json
{ "answer": "...", "cards": [...], "history_turns": 3 }
```

---

## Agent Tools

```
search_machines(q, brand, category)
  → GET /api/machines
  → returns up to 8 machines (non-empty fallback if 0 results)
  → triggers card: machine_list

get_machine_details(id)
  → GET /api/machines/:id
  → returns full specs, features, catalogUrl
  → triggers card: machine_detail

get_price(machine_model)
  → GET /api/price/:model
  → returns { model, quote_price_inr, currency }
  → triggers card: price
  → agent retries with stripped prefix if first attempt fails
```

---

## Conversation Persistence

```
Turn N:
  Browser sends { message, thread_id }
       │
       ▼
  Node proxies to Python agent
       │
       ▼
  LangGraph loads full thread state from Postgres (via thread_id)
  Appends new HumanMessage
  Runs ReAct loop (tool calls → tool results → final answer)
  Saves updated state back to Postgres
       │
       ▼
  Returns { answer, cards, history_turns }
       │
       ▼
  Browser stores thread_id in localStorage
```

Survives: `docker compose restart`, `docker compose down && up`, machine reboot.
Destroyed only by: `docker compose down -v` (removes the volume).

---

## Environment Variables

| Variable | Where | Purpose |
|---|---|---|
| `GROQ_API_KEY` | `backend/.env` | Groq API auth (used by Python agent via env_file) |
| `SHEET_ID` | `backend/.env` | Google Sheet ID for live pricing |
| `SHEET_GID` | `backend/.env` | Sheet tab GID (default: 0) |
| `PORT` | `backend/.env` | Node.js server port (default: 3001) |
| `ADMIN_PASSWORD` | `backend/.env` | Password for `/api/login` |
| `JWT_SECRET` | `backend/.env` | JWT signing secret |
| `PYTHON_AGENT_URL` | `backend/.env` | URL of Python agent (default: http://localhost:8000) |
| `DATABASE_URL` | `backend/.env` | Postgres connection string |
| `API_BASE` | Docker env | Base URL for tool calls inside container (host.docker.internal:3001) |

---

## File Structure

```
main-portfolio/
├── rag.html                    ← Chat UI (thread persistence, turn badge, New Chat)
├── index.html                  ← Homepage
├── products.json               ← Machine catalog (242 machines)
├── docker-compose.yml          ← postgres + python-agent services
├── .gitignore
├── .githooks/
│   └── pre-push                ← Secret scan + smoke tests on every push
├── assets/
│   ├── styles.css              ← Design tokens + shared styles
│   └── app.js                  ← Shared frontend helpers
├── backend/
│   ├── server.js               ← Express API + proxy to Python agent
│   ├── agent.js                ← RETIRED — kept for rollback only
│   ├── .env                    ← API keys and config (git-ignored)
│   └── package.json            ← Dependencies + prepare hook activation
├── python-agent/
│   ├── main.py                 ← FastAPI app
│   ├── agent.py                ← LangGraph ReAct + PostgresSaver
│   ├── tools.py                ← Three tools via httpx
│   ├── requirements.txt
│   └── Dockerfile
└── docs/
    ├── system-architecture.md  ← This file
    ├── how-it-works.md         ← Runtime flow
    ├── how-we-built-it.md      ← Build history and decisions
    └── system-thinking.md      ← Design rationale
```
