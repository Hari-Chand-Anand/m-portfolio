# System Thinking — HCA Salesman Knowledge Bot

## Why Tool-Use Instead of Pure RAG

Classic RAG embeds documents into a vector database, retrieves the top-K chunks at query time, and stuffs them into the LLM context. It works well for unstructured text (PDFs, articles).

We chose **tool-use** instead because the data is already structured:

| Classic RAG | Tool-Use (our approach) |
|---|---|
| Embed catalog into vector DB | Call `/api/machines?q=...` directly |
| Retrieve top-K chunks | Get exactly the right machines |
| Chunks may be stale | Always reads live `products.json` |
| Price requires separate pipeline | `get_price` tool hits Google Sheets live |
| Setup: embedding model + vector DB | Setup: three API endpoints already existed |

The catalog is a queryable JSON API, not a document store. Using tool-use means the LLM always gets exact, current data — no embedding drift, no stale chunks.

**The filename `rag.html` is a misnomer** from an earlier prototype name that stuck. The agent does not embed, index, or semantically retrieve anything.

### Why answers can feel thin

The agent is limited by what the catalog contains. If `products.json` has a machine with an empty `description` or no `features` array, the agent has nothing to say beyond the model name and category. The agent doesn't have general internet knowledge about these machines — it only knows what's in `products.json` and the Google Sheet. It also has no access to the rest of the portfolio website (the HTML pages, catalogs, videos, or 3D models).

---

## Why Groq + Llama 4 Scout

The original Google Gemini key was suspended. The replacement options were:

| Option | Reason rejected / chosen |
|---|---|
| New Gemini key | User switched to Groq |
| OpenAI GPT-4o | Paid, no free tier |
| `llama-3.3-70b-versatile` | Intermittent `tool_use_failed` errors on Groq |
| `llama3-groq-70b-8192-tool-use-preview` | Decommissioned |
| `llama-3.1-70b-versatile` | Decommissioned |
| **`meta-llama/llama-4-scout-17b-16e-instruct`** | ✅ Active, reliable tool calling, fast |

LangChain's `ChatGroq` wraps the OpenAI-compatible Groq API — no custom HTTP code needed.

---

## Why the `model` Parameter Was Renamed

The first version of `get_price` used `model` as the parameter name. This caused `tool_use_failed` on `llama-3.3-70b-versatile`. The word `model` is a reserved/overloaded concept for LLMs (it refers to themselves). Renaming to `machine_model` removed the ambiguity.

Lesson: **avoid parameter names that are LLM meta-concepts** (`model`, `prompt`, `role`, `system`, `content`).

---

## Why History Moved to PostgreSQL

Originally, session state (conversation history) lived in the browser (`let history = []`) and was sent with each request. This was simple but had a hard limit: refreshing the page lost the entire conversation.

We migrated to LangGraph's `PostgresSaver` checkpointer:

| Option | Problem |
|---|---|
| Browser-side history | Lost on page refresh |
| Server-side in-memory session | Backend restart wipes all sessions |
| Redis session store | Extra dep, overkill for a single-user tool |
| **LangGraph + PostgreSQL** | ✅ Survives refreshes, restarts, and reboots |

LangGraph stores the full LangChain message state (every `HumanMessage`, `AIMessage`, `ToolMessage`) per `thread_id` in a Postgres table managed by the checkpointer. The browser holds only the UUID in `localStorage` — no conversation payload.

**Postgres + Docker named volume** means the data survives `docker compose down && up`. It is only destroyed by `docker compose down -v`.

---

## Why the `model` Column in the Sheet Isn't the Model Code

The Google Sheet `model` column stores brand names (`DUKEJIA`, `DUKE`, `HIKARI`) — not model codes. The actual model code is in a separate `key` column (`DY-1201H`, `dy430gt`).

This is likely because the sheet was designed for grouping by brand, not for direct model lookup. Our `findByModel()` handles it by searching `key` first, then `model`, with both exact and contains matching.

The normalisation step (`norm()`) is critical — it strips hyphens, lowercases, removes non-breaking spaces, and collapses whitespace. This handles:
- `DY-1201` matching `DY 1201`
- `dy430gt ` (trailing space in sheet) matching `dy430gt`
- `HX‑6818TD` (non-breaking hyphen) matching `HX-6818TD`

---

## Why Cards Are Separate from Text

The AI text answer and the card data serve different purposes:

- **Text answer** — conversational, summarises what was found
- **Cards** — visual, scannable, actionable (catalog links, formatted prices)

By keeping them separate in the API response (`answer` vs `cards`), the frontend can render them independently. The text doesn't need to contain model names or prices — the cards show those.

`extract_cards()` scans only the **current turn's** `ToolMessage` objects (messages after the last `HumanMessage`). Without this scope, cards from previous turns would re-appear on every subsequent message.

---

## Why Tool Messages Must Be Non-Empty

Groq's API rejects LangChain tool messages with empty content (`[]`). This manifested when `search_machines("overlock")` returned an empty list — Groq returned HTTP 400:
```
'messages.7.content': minimum number of items is 1
```

Fix: every tool function returns at least one item, even if it's a "not found" message:
```python
if not data:
    return [{"message": "No machines found. Available categories: embroidery, lockstitch, finishing, industrial, sewing."}]
```

Lesson: **never return an empty list from a LangChain tool** when using Groq.

---

## CORS: Why `origin === "null"` Not `!origin`

When a browser opens a `file://` page and that page makes a `fetch()` to `localhost`, the browser sends:
```
Origin: null
```
as a **string literal** — not a missing header. JavaScript's `!origin` check catches `undefined` and empty string but not the string `"null"`. We explicitly check `origin === "null"` to allow local file access.

---

## Why the Pre-Push Hook Uses BRE Not ERE

The hook scans added lines with:
```sh
ADDED=$(git diff @{u}..HEAD | grep "^+" | grep -v "^+++")
```

`grep "^+"` uses BRE (Basic Regular Expressions) where `+` is a literal character, not a quantifier. In BRE, `+` means "one or more" only when escaped as `\+`. So `grep "^+"` matches lines beginning with `+`, and `grep -v "^+++"` removes the `+++` diff header lines (literal `+++`).

If this were ERE (`grep -E`), you'd need `grep -E "^\+"` and `grep -Ev "^\+\+\+"`. Using BRE avoids that escaping complexity.

---

## Known Limitations

| Limitation | Impact | Fix path |
|---|---|---|
| No vector search | Can't answer "what's the best machine for heavy denim" semantically | Add embedding layer + pgvector |
| Agent has no portfolio website knowledge | Can't describe catalog PDFs, videos, or 3D models | Feed portfolio content as additional tools |
| Price sheet fuzzy matching | Could match wrong machine if model codes overlap | Enforce exact model code in sheet |
| Catalog data quality | Sparse `description`/`features` → thin answers | Enrich `products.json` with richer descriptions |
| Docker required | Python agent + Postgres must be running to chat | Deploy to cloud (Fly.io, Railway, etc.) |
| `down -v` destroys history | If someone runs `docker compose down -v`, all threads are gone | Periodic Postgres backup |
