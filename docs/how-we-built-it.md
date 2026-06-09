# How We Built It — HCA RAG Assistant

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

The `/api/chat` response now returns:
```json
{ "answer": "...", "history": [...], "cards": [...] }
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

## Step 6 — One-Click Launcher

Created `start-rag.bat` so the user doesn't have to manually start the backend:

1. Checks if port 3001 is already in use (skips re-launch if so)
2. Starts `node server.js` in a minimized window
3. Opens `rag.html` in the default browser
