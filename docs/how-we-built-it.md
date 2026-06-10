# How We Built It — HCA Salesman Knowledge Bot

## Starting Point

The portfolio already had an Express backend (`backend/server.js`) with:
- A machine catalog API (`/api/machines`) serving 242 machines from `products.json`
- A live pricing API (`/api/price/:model`) fetching from a Google Sheet CSV
- A Gemini-based AI chat endpoint (`/api/chat`) with tool-use

The problem: the AI chat endpoint returned only a text answer. The frontend had no way to render machine cards or price cards — it was just a string.

---

## Step 1 — Expose Structured Card Data from the Agent

**File:** `backend/agent.js`

The agent already called three tools internally (`search_machines`, `get_machine_details`, `get_price`). Those tool results went to the LLM but never reached the frontend.

We added a `cards` array that collects tool results as typed objects during the agent loop:

```js
if (name === "search_machines" && Array.isArray(result)) {
  cards.push({ type: "machine_list", machines: result });
}
if (name === "get_machine_details" && !result.error) {
  cards.push({ type: "machine_detail", machine: result });
}
if (name === "get_price" && !result.error) {
  cards.push({ type: "price", ...result });
}
```

---

## Step 2 — Build the Chat UI (rag.html)

Created a standalone `rag.html` matching the site's dark glassmorphism design.

Key decisions:
- **No framework** — vanilla JS to stay consistent with the rest of the site
- **Full-height flex layout** — header / messages / input, no scrolling on the page itself
- **Cards render below each AI message** — machine cards as a horizontal wrap grid, price cards as a standalone block
- **Starter suggestion chips** — 6 common queries shown on load to guide users

---

## Step 3 — Switch from Gemini to Groq

The `GOOGLE_API_KEY` in `.env` was suspended. We replaced the entire AI layer:

| Before | After |
|---|---|
| `@google/genai` SDK | Native `fetch` to Groq's OpenAI-compatible API |
| Gemini 2.0 Flash | `meta-llama/llama-4-scout-17b-16e-instruct` |
| Gemini content format (`parts`) | OpenAI message format (`role/content`) |
| Gemini tool declarations | OpenAI function tool declarations |

No new npm packages needed — Groq's API is OpenAI-compatible so plain `fetch` works.

---

## Step 4 — Fix Price Matching

The price lookup was returning "not found" for models like `DY-1201`.

**Root cause:** `findByModel()` in `server.js` only searched the `model` column in the Google Sheet, which contains brand names (`DUKEJIA`, `DUKE`, `HIKARI`). The actual model codes (`DY-1201H`, `GC-188`, etc.) live in the `key` column.

**Fix:** Updated `findByModel()` to search in priority order:
1. Exact match on `key` column
2. Exact match on `model` column
3. Contains match on `key` column (`DY-1201` matches `DY-1201H`)
4. Contains match on `model` column

---

## Step 5 — Fix CORS for file:// Pages

Opening `rag.html` directly from the filesystem sends `Origin: null` (the literal string) — not JavaScript `null` and not `file://`. The CORS check had to explicitly allow it:

```js
if (!origin || origin === "null") return true;
```

---

## Step 6 — Migrate to LangGraph + PostgreSQL (Persistent Memory)

The browser-side history approach had a hard limit: refreshing the page lost the entire conversation. We rebuilt the agent in Python using LangGraph.

### What Changed

| Before | After |
|---|---|
| `backend/agent.js` (Node.js, in-memory) | `python-agent/agent.py` (Python, LangGraph) |
| `history[]` in request body | `thread_id` UUID |
| State in browser `let history = []` | State in PostgreSQL via PostgresSaver |
| Groq via native `fetch` | Groq via `langchain-groq` |
| History lost on refresh | History survives refresh, restart, reboot |
| Single-process Node.js | Docker: python-agent + postgres |

### Key files added

**`python-agent/agent.py`** — LangGraph core:
```python
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver(conn_pool)
graph = create_react_agent(model, tools, prompt=SYSTEM_PROMPT, checkpointer=checkpointer)
```

**`python-agent/tools.py`** — Three LangChain tools calling the existing Node.js API via `httpx`. The tools call `http://host.docker.internal:3001` when inside Docker, or `http://localhost:3001` when running locally.

**`python-agent/main.py`** — FastAPI with `/health` and `/chat` endpoints.

**`docker-compose.yml`** — `postgres:16-alpine` with a named volume (`postgres_data`) and `python-agent` with a health check dependency.

### Node.js server update

`backend/server.js` `/api/chat` now proxies to the Python agent instead of calling `agent.js`:
```js
const pyRes = await fetch(`${PYTHON_AGENT_URL}/chat`, {
  method: "POST",
  body: JSON.stringify({ message, thread_id }),
});
```
`agent.js` remains in the repo but is no longer called — kept only for rollback.

### Groq constraint: non-empty tool messages

During development, `search_machines("overlock")` returned `[]`. Groq rejected this with:
```
'messages.7.content': minimum number of items is 1
```
Fix: always return at least one item from tool functions:
```python
if not data:
    return [{"message": "No machines found. Available categories: embroidery, lockstitch, finishing, industrial, sewing."}]
```

### `state_modifier` → `prompt`

Newer versions of LangGraph renamed the parameter. Updated:
```python
# Old (broke)
create_react_agent(..., state_modifier=SYSTEM_PROMPT)

# New
create_react_agent(..., prompt=SYSTEM_PROMPT)
```

---

## Step 7 — UI Updates for Persistence

Updated `rag.html` to surface the new persistence behaviour:

- **`thread_id` in `localStorage`** — survives page refresh; sent on every request
- **Turn counter badge** — `• N turns` in the header shows how many messages are in the thread
- **"New Chat" button** — clears `localStorage`, triggers a fresh thread UUID on the next message
- **Resume notice** — on page load, if a prior thread exists, shows "Resuming previous session · N turns" on the welcome screen

---

## Step 8 — Pre-Push Hook

Created `.githooks/pre-push` to block accidental secret leaks and broken pushes:

1. **Secret scan** — regex patterns for Groq, OpenAI, AWS, GitHub, Anthropic, HuggingFace keys; private key headers; weak dev secrets
2. **Smoke tests** — verifies Node.js (:3001), Python agent (:8000/health), and a full chat round-trip before allowing the push

Activated automatically after `npm install` via `package.json` `prepare` script:
```json
"prepare": "git config core.hooksPath .githooks"
```

Fresh clones just need `npm install` — no manual `git config` needed.
