# System Architecture — HCA RAG Assistant

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT (Browser)                         │
│                                                                 │
│   rag.html                                                      │
│   ├── Chat UI (vanilla JS)                                      │
│   ├── Conversation history (in-memory, sent per request)        │
│   └── Card renderer (machine cards, price cards)                │
└──────────────────────────┬──────────────────────────────────────┘
                           │ POST /api/chat
                           │ { message, history }
┌──────────────────────────▼──────────────────────────────────────┐
│                    BACKEND (Node.js / Express)                  │
│                       localhost:3001                            │
│                                                                 │
│   server.js                                                     │
│   ├── CORS middleware (allows origin: null for file://)         │
│   ├── POST /api/chat  ──►  agent.js                            │
│   ├── GET  /api/machines         ──► products.json             │
│   ├── GET  /api/machines/:id     ──► products.json             │
│   └── GET  /api/price/:model     ──► Google Sheets (live CSV)  │
│                                                                 │
│   agent.js                                                      │
│   ├── System prompt (HCA assistant persona + rules)             │
│   ├── Tool definitions (search_machines, get_machine_details,   │
│   │                      get_price)                             │
│   ├── Agent loop (max 5 rounds)                                 │
│   │   ├── Call Groq API                                         │
│   │   ├── Execute tool calls                                    │
│   │   ├── Collect cards[]                                       │
│   │   └── Repeat until no tool calls                            │
│   └── Returns { answer, history, cards }                        │
└──────────┬──────────────────────────────┬───────────────────────┘
           │                              │
           ▼                              ▼
┌──────────────────┐          ┌───────────────────────┐
│   Groq Cloud API │          │   Google Sheets        │
│                  │          │   (live pricing CSV)   │
│   Llama 4 Scout  │          │                        │
│   17B instruct   │          │   241 rows             │
│   Tool-use       │          │   columns: model, key, │
│   enabled        │          │   quote price, base    │
│                  │          │   price, fx override   │
└──────────────────┘          └───────────────────────┘
```

---

## Component Map

| Component | File | Role |
|---|---|---|
| Chat UI | `rag.html` | User interface — input, messages, card rendering |
| Launcher | `start-rag.bat` | Starts backend + opens browser in one click |
| API Server | `backend/server.js` | Express routes, CORS, CSV parser, price matching |
| AI Agent | `backend/agent.js` | LLM loop, tool execution, card collection |
| Catalog | `products.json` | 242 machines — specs, media URLs, stock |
| Pricing | Google Sheets | Live quote prices, base prices, FX overrides |
| Styles | `assets/styles.css` | Shared dark theme design tokens |
| Helpers | `assets/app.js` | Shared frontend utilities (`moneyINR`, `loadData`) |

---

## API Endpoints

### `POST /api/chat`
Main RAG endpoint. Runs the agent loop.

**Request:**
```json
{
  "message": "What is the price of DY-1201?",
  "history": [ ...previous turns in OpenAI format... ]
}
```

**Response:**
```json
{
  "answer": "The price of DY-1201 is ₹5,06,000.",
  "history": [ ...updated history (last 20 messages)... ],
  "cards": [
    { "type": "price", "model": "DY-1201", "quote_price_inr": 506000, "currency": "INR" }
  ]
}
```

---

### `GET /api/machines`
Search and filter the catalog.

| Query Param | Example | Effect |
|---|---|---|
| `q` | `?q=embroidery` | Keyword search on name, model, description, tags |
| `brand` | `?brand=DUKEJIA` | Exact brand filter |
| `category` | `?category=lockstitch` | Exact category filter |

---

### `GET /api/price/:model`
Fetch live price from Google Sheets.

**Response:**
```json
{
  "model": "DY-1201",
  "quote_price_inr": 506000,
  "currency": "INR"
}
```

Matching order: exact key → exact model → contains key → contains model.

---

## Agent Tool Definitions

```
search_machines
  params: q (keyword), brand, category
  returns: array of up to 8 machines
  triggers card: machine_list

get_machine_details
  params: id (machine ID from search results)
  returns: full specs, features, application, catalogUrl
  triggers card: machine_detail

get_price
  params: machine_model (e.g. "DY-1201", "DUKE R9")
  returns: { model, quote_price_inr, currency }
  triggers card: price
```

---

## Data Flow — Price Lookup

```
User: "price of DY-1201"
         │
         ▼
Agent calls get_price("DY-1201")
         │
         ▼
GET /api/price/DY-1201
         │
         ▼
readRowsLive()  ──fetch──►  Google Sheets CSV
         │
         ▼
parseCsv()  →  241 row objects
         │
         ▼
findByModel(rows, "DY-1201")
  1. norm("DY-1201") = "dy 1201"
  2. Search key column:
     - "dk 1201" ≠ "dy 1201"
     - "dy 1201h" contains "dy 1201"  ✓  MATCH
  3. Return row { key: "DY-1201H", quote price: "506000", ... }
         │
         ▼
Response: { model: "DY-1201", quote_price_inr: 506000, currency: "INR" }
```

---

## Data Flow — Catalog Search

```
User: "show me embroidery machines"
         │
         ▼
Agent calls search_machines({ category: "embroidery" })
         │
         ▼
GET /api/machines?category=embroidery
         │
         ▼
Filter products.json (242 machines)
  m.category.toLowerCase() === "embroidery"
  → 9 matches
         │
         ▼
Slice to first 8, map to:
  { id, brand, model, category, operation, description, catalogUrl }
         │
         ▼
cards.push({ type: "machine_list", machines: [...8 items] })
         │
         ▼
Frontend renders 8 machine cards in a flex-wrap grid
```

---

## Environment Variables

| Variable | Location | Purpose |
|---|---|---|
| `GROQ_API_KEY` | `backend/.env` | Groq API authentication |
| `SHEET_ID` | `backend/.env` | Google Sheet ID for live pricing |
| `SHEET_GID` | `backend/.env` | Sheet tab GID (default: 0) |
| `PORT` | `backend/.env` | Server port (default: 3001) |
| `ADMIN_PASSWORD` | `backend/.env` | Password for `/api/login` |
| `JWT_SECRET` | `backend/.env` | JWT signing secret |
| `API_BASE` | `backend/.env` | Base URL for internal tool calls (default: localhost:3001) |

---

## File Structure

```
main-portfolio/
├── rag.html                  ← Chat UI
├── start-rag.bat             ← One-click launcher
├── index.html                ← Homepage (links to rag.html)
├── products.json             ← Machine catalog (242 machines, 135KB)
├── assets/
│   ├── styles.css            ← Design tokens + shared styles
│   └── app.js                ← Shared frontend helpers
├── backend/
│   ├── server.js             ← Express API server
│   ├── agent.js              ← Groq agent loop + tool execution
│   ├── .env                  ← API keys and config
│   └── package.json          ← Dependencies
└── docs/
    ├── how-we-built-it.md    ← Build decisions and steps
    ├── how-it-works.md       ← Runtime flow explanation
    ├── system-thinking.md    ← Design rationale
    └── system-architecture.md ← This file
```
