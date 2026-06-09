# How It Works — HCA RAG Assistant

## Overview

A user types a question in `rag.html`. The message travels to the Express backend, which runs an AI agent loop. The agent calls internal tools to fetch real catalog data and live prices, then returns both a text answer and structured card data. The frontend renders the cards visually alongside the text.

---

## Request Flow

```
User types message
      │
      ▼
rag.html (frontend)
  POST /api/chat  { message, history }
      │
      ▼
backend/server.js
  calls chat() from agent.js
      │
      ▼
backend/agent.js  ◄─── Agent Loop (max 5 rounds)
  │
  ├─ Sends messages to Groq API (Llama 4 Scout)
  │    model decides which tool(s) to call
  │
  ├─ Tool: search_machines
  │    └─ GET /api/machines?q=...&brand=...&category=...
  │         └─ filters products.json (242 machines)
  │
  ├─ Tool: get_machine_details
  │    └─ GET /api/machines/:id
  │         └─ returns full specs from products.json
  │
  ├─ Tool: get_price
  │    └─ GET /api/price/:model
  │         └─ fetches live CSV from Google Sheets
  │              └─ parses CSV → finds row by key/model → returns quote_price
  │
  └─ Loop ends when model produces a text response (no more tool calls)
      │
      ▼
Returns: { answer, history, cards }
      │
      ▼
rag.html renders:
  - Text bubble (AI answer)
  - Machine cards (brand, model, category, description, catalog link)
  - Price card (₹ formatted, "Live · Google Sheets" indicator)
```

---

## The Agent Loop

The agent does not make a single LLM call. It loops:

```
Round 1:  LLM receives user message
          LLM decides → call search_machines("embroidery")
          Tool runs → returns 8 machine objects
          Tool results added to message history

Round 2:  LLM receives tool results
          LLM decides → no more tools needed, write final answer
          Loop exits

Final:    { answer: "Here are 8 embroidery machines...", cards: [{ type: "machine_list", machines: [...] }] }
```

For a price query with a first-attempt miss:
```
Round 1:  call get_price("DUKEJIA DY-1201")  →  error: not found
Round 2:  retry get_price("DY-1201")         →  ₹5,06,000 ✓
Round 3:  write final answer
```

Maximum 5 rounds per request to prevent infinite loops.

---

## Price Matching Logic

The Google Sheet has two relevant columns:

| model   | key       | quote price |
|---------|-----------|-------------|
| DUKEJIA | DY-1201H  | 506000      |
| DUKE    | dy430gt   | 35200       |
| HIKARI  | HX-6818TD | 120000      |

`findByModel()` searches in this order:
1. **Exact key match** — `norm("DY-1201H") === norm(query)`
2. **Exact model match** — `norm("DUKEJIA") === norm(query)`
3. **Key contains match** — `"dy 1201h".includes("dy 1201")` ✓ (this is how DY-1201 finds DY-1201H)
4. **Model contains match** — fallback

`norm()` strips hyphens, extra spaces, non-breaking spaces, and lowercases everything before comparing.

---

## Conversation Memory

History is stored in the browser (`let history = []`) and sent with every request. The backend appends the new turn and returns the updated history. Only the last 20 messages are kept (10 turns) to stay within the model's context window.

The history format is OpenAI-compatible:
```json
[
  { "role": "user",      "content": "show me embroidery machines" },
  { "role": "assistant", "tool_calls": [...] },
  { "role": "tool",      "tool_call_id": "...", "content": "[...]" },
  { "role": "assistant", "content": "Here are 8 embroidery machines..." }
]
```

---

## Card Rendering

The frontend receives `cards[]` alongside the text answer. Each card has a `type`:

| type | Rendered as |
|---|---|
| `machine_list` | Grid of compact machine cards (brand, model, category, description, catalog link) |
| `machine_detail` | Single detailed machine card |
| `price` | Price card with large ₹ amount + "Live · Google Sheets" indicator |

Cards are rendered below the AI text bubble in the same message row.
