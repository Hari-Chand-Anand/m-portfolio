# How It Works — HCA Salesman Knowledge Bot

## Overview

A user types a question in `rag.html`. The message travels to the Node.js backend, which proxies it to a Python FastAPI service running a LangGraph ReAct agent. The agent loads the full conversation thread from PostgreSQL, calls internal tools to fetch real catalog data and live prices, saves the updated thread back to Postgres, then returns a text answer and structured card data. The frontend renders the cards visually alongside the text.

> **Not RAG.** Despite the filename, this is a **tool-calling agent** — it queries structured REST APIs, not a vector store. See [system-thinking.md](system-thinking.md) for the full comparison.

---

## Request Flow

```
User types message
      │
      ▼
rag.html (frontend)
  reads thread_id from localStorage (null on first message)
  POST /api/chat  { message, thread_id }
      │
      ▼
backend/server.js
  generates UUID if thread_id missing
  POST http://localhost:8000/chat  { message, thread_id }
      │
      ▼
python-agent/main.py  (FastAPI)
  calls run_chat(message, thread_id)
      │
      ▼
python-agent/agent.py  (LangGraph ReAct)
  ├─ PostgresSaver loads full thread state from Postgres
  │    (all prior turns for this thread_id)
  │
  ├─ Appends new HumanMessage
  │
  ├─ Sends messages to Groq API (Llama 4 Scout)
  │    model decides which tool(s) to call
  │
  ├─ Tool: search_machines(q, brand, category)
  │    └─ GET /api/machines?q=...
  │         └─ filters products.json (242 machines)
  │
  ├─ Tool: get_machine_details(id)
  │    └─ GET /api/machines/:id
  │         └─ returns full specs from products.json
  │
  ├─ Tool: get_price(machine_model)
  │    └─ GET /api/price/:model
  │         └─ fetches live CSV from Google Sheets
  │              └─ parses CSV → finds row by key/model → returns quote_price
  │
  ├─ Loop until model stops calling tools (max ~10 rounds via LangGraph)
  │
  ├─ PostgresSaver saves updated thread state back to Postgres
  │
  └─ extract_cards() — scans current turn's ToolMessages only
      │
      ▼
Returns: { answer, cards, history_turns }
      │
      ▼
Node proxies response back, injects thread_id
Returns: { answer, cards, thread_id, history: [] }
      │
      ▼
rag.html:
  - stores thread_id in localStorage
  - renders text bubble (AI answer)
  - renders machine cards + price cards
  - updates turn counter badge
```

---

## The Agent Loop (ReAct)

LangGraph's `create_react_agent()` implements a Reasoning + Acting loop. It's not a single LLM call.

```
Thought:  LLM reads user message + full thread history
          LLM decides → call search_machines(category="embroidery")

Act:      Tool runs → returns up to 8 machine objects

Observe:  Tool results added to message state

Thought:  LLM reads tool results
          LLM decides → no more tools needed, write final answer

Final:    { answer: "Here are 8 embroidery machines...", cards: [...] }
```

For a price query with a first-attempt miss:
```
Round 1:  call get_price("DUKEJIA DY-1201")  →  error: not found
Round 2:  retry get_price("DY-1201")         →  ₹5,06,000 ✓
Round 3:  write final answer
```

The agent tools call `http://localhost:3001` — the same Node.js backend. Inside Docker, this is `http://host.docker.internal:3001` (set via `API_BASE` env var).

---

## Conversation Memory (PostgreSQL)

Every conversation is a **thread** identified by a UUID `thread_id`. LangGraph's `PostgresSaver` checkpoint stores the full LangChain message state — every `HumanMessage`, `AIMessage`, and `ToolMessage` — in Postgres after each turn.

```
Thread abc-123 in Postgres after Turn 3:
  [HumanMessage] "show me embroidery machines"
  [AIMessage]    tool_calls=[search_machines(category=embroidery)]
  [ToolMessage]  [8 machine objects]
  [AIMessage]    "Here are 8 machines..."
  [HumanMessage] "price of DY-1201?"
  [AIMessage]    tool_calls=[get_price(DY-1201)]
  [ToolMessage]  {quote_price_inr: 506000}
  [AIMessage]    "The price is ₹5,06,000."
  [HumanMessage] "what about the R9?"
  ...
```

On each new turn, the agent loads this full history from Postgres before calling the LLM — so the model always has context of the entire conversation.

**Frontend persistence:** `thread_id` is stored in `localStorage`. On page refresh, the browser sends the same `thread_id` and the agent resumes the thread from Postgres. The resume notice and turn count are shown in the UI.

**Lifecycle:**
- `docker compose down && docker compose up` — thread state survives (named volume `postgres_data`)
- `docker compose down -v` — state destroyed (volume deleted)
- Page refresh — state survives (thread_id in localStorage + Postgres)
- "New Chat" button — clears localStorage, next message creates a new thread_id

---

## Card Rendering

`extract_cards()` in `agent.py` scans the `ToolMessage` objects from the **current turn only** (messages after the last `HumanMessage`) and maps them to typed card objects.

| ToolMessage source | Card type | Rendered as |
|---|---|---|
| `search_machines` | `machine_list` | Grid of compact machine cards (brand, model, category, catalog link) |
| `get_machine_details` | `machine_detail` | Single detailed machine card with specs |
| `get_price` | `price` | Price card with large ₹ amount + "Live · Google Sheets" indicator |

Cards are scoped to the current turn to prevent cards from previous turns re-appearing on every subsequent message.

---

## Price Matching Logic

The Google Sheet has two relevant columns:

| model   | key       | quote price |
|---------|-----------|-------------|
| DUKEJIA | DY-1201H  | 506000      |
| DUKE    | dy430gt   | 35200       |
| HIKARI  | HX-6818TD | 120000      |

`findByModel()` in `server.js` searches in this order:
1. **Exact key match** — `norm("DY-1201H") === norm(query)`
2. **Exact model match** — `norm("DUKEJIA") === norm(query)`
3. **Key contains match** — `"dy 1201h".includes("dy 1201")` ✓
4. **Model contains match** — fallback

`norm()` strips hyphens, extra spaces, non-breaking spaces, and lowercases before comparing.
