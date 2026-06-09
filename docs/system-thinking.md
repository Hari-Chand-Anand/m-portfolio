# System Thinking — HCA RAG Assistant

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

We use Groq's **OpenAI-compatible API** via native `fetch` — no SDK needed, no new dependencies.

---

## Why the `model` Parameter Was Renamed

The first version of `get_price` used `model` as the parameter name:
```json
{ "machine_model": "DY-1201" }
```

This caused `tool_use_failed` on `llama-3.3-70b-versatile`. The word `model` is a reserved/overloaded concept for LLMs (it refers to themselves). Renaming to `machine_model` removed the ambiguity and fixed the failures on that model.

Lesson: **avoid parameter names that are LLM meta-concepts** (`model`, `prompt`, `role`, `system`, `content`).

---

## Why History Lives in the Browser

Session state (conversation history) is stored in the frontend (`let history = []`) and sent with each request. The backend is stateless.

Alternatives considered:

| Option | Problem |
|---|---|
| Server-side session (in-memory) | Backend restarts wipe all sessions |
| Redis/DB session store | Over-engineering for a single-user tool |
| **Browser-side history** | ✅ Simple, restartable backend, works for the use case |

The downside is that refreshing the page clears the conversation — acceptable for a sales assistant tool.

---

## Why the Price Sheet Uses a `key` Column

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

By keeping them separate in the API response (`answer` vs `cards`), the frontend can render them independently. The text doesn't need to contain model names or prices — the cards show those. This also means the AI doesn't need to format prices as Markdown tables or lists; it just needs to describe what it found.

---

## CORS: Why `origin === "null"` Not `!origin`

When a browser opens a `file://` page and that page makes a `fetch()` to `localhost`, the browser sends:
```
Origin: null
```
as a **string literal** — not a missing header. JavaScript's `!origin` check catches `undefined` and empty string but not the string `"null"`. We explicitly check `origin === "null"` to allow local file access.

---

## Known Limitations

| Limitation | Impact | Fix path |
|---|---|---|
| No vector search | Can't answer "what's the best machine for heavy denim" semantically — only keyword/category | Add embedding layer + pgvector |
| Price sheet key matching is fuzzy | Could match wrong machine if model codes overlap | Enforce exact model code in sheet |
| Duplicate price cards | AI retries with variations, both succeed | Deduplicate `cards` by model before returning |
| History lost on page refresh | No session persistence | Store in `localStorage` |
| Backend must be running | User has to use `start-rag.bat` | Deploy backend to a server |
