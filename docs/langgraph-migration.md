# LangGraph + PostgreSQL Migration
## HCA Salesman Knowledge Bot — Production Memory Architecture

> **Goal:** Replace in-memory (client-side) chat history with server-side persistent memory using LangGraph + PostgreSQL. All existing functionality — tools, response format, UI cards, pricing, catalog — stays identical. Only the agent runtime and state storage change.

---

## 1. Architecture: Before vs After

### Before
```
Browser (rag.html)
  │  POST /api/chat { message, history[] }   ← history travels over the wire
  ▼
Node.js Express (server.js)
  │  chat(message, history)
  ▼
agent.js  ←  manual for-loop, in-memory state, dies on refresh
  │
Groq API (Llama 4)
```

### After
```
Browser (rag.html)
  │  POST /api/chat { message, thread_id }   ← only thread_id, no history blob
  ▼
Node.js Express (server.js)          ← all existing routes untouched
  │  proxy to Python agent
  ▼
Python FastAPI (python-agent/)
  │  LangGraph ReAct graph
  │  PostgresSaver checkpointer
  ▼
PostgreSQL (Docker)                  ← full conversation history persisted
  │
Groq API (Llama 4)                   ← same model, same system prompt
```

**What does NOT change:**
- All Node.js routes: `/api/machines`, `/api/price`, `/api/login`, `/api/admin/*`, `/api/debug`, `/api/admin/logs`
- Response shape seen by the frontend: `{ answer, cards, thread_id }`
- UI cards: `machine_list`, `machine_detail`, `price`
- JWT auth, Google Sheets pricing, query logging
- System prompt and tool behaviour

---

## 2. New File Structure

```
main-portfolio/
├── backend/
│   ├── server.js          ← MODIFIED (proxy /api/chat, remove agent import)
│   ├── agent.js           ← RETIRED (keep file, no longer imported)
│   └── package.json       ← unchanged
│
├── python-agent/          ← NEW directory
│   ├── main.py            ← FastAPI app
│   ├── agent.py           ← LangGraph graph + PostgresSaver
│   ├── tools.py           ← same 3 tools, now call Node.js API via httpx
│   ├── requirements.txt
│   └── Dockerfile
│
├── docker-compose.yml     ← NEW (postgres + python-agent)
├── rag.html               ← MODIFIED (4 lines — thread_id tracking)
├── .env (backend/.env)    ← add DATABASE_URL, PYTHON_AGENT_URL
└── .gitignore             ← add python-agent/.venv
```

---

## 3. Files to Create

### `python-agent/requirements.txt`
```
langgraph>=0.2.28
langgraph-checkpoint-postgres>=1.0.0
langchain-groq>=0.1.9
langchain-core>=0.2.0
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
httpx>=0.27.0
psycopg[binary,pool]>=3.1.0
python-dotenv>=1.0.0
```

---

### `python-agent/tools.py`
```python
import os
import httpx
from langchain_core.tools import tool

BASE = os.getenv("API_BASE", "http://localhost:3001")

@tool
def search_machines(q: str = "", brand: str = "", category: str = "") -> list:
    """
    Search HCA machine catalog by keyword, brand, or category.
    Use for finding machines, listing options, recommendations.
    Args:
        q: Keyword e.g. embroidery, lockstitch, button hole, overlock
        brand: DUKE or DUKEJIA
        category: e.g. embroidery, lockstitch
    """
    params = {k: v for k, v in {"q": q, "brand": brand, "category": category}.items() if v}
    try:
        res = httpx.get(f"{BASE}/api/machines", params=params, timeout=10)
        data = res.json()
        return data[:8]
    except Exception as e:
        return [{"error": str(e)}]


@tool
def get_machine_details(id: str) -> dict:
    """
    Get complete specs for one specific machine.
    Use when user asks about a specific model or wants full details.
    Args:
        id: Machine ID from search results e.g. duke-dk-1201-embroidery-machine
    """
    try:
        res = httpx.get(f"{BASE}/api/machines/{id}", timeout=10)
        if res.status_code == 404:
            return {"error": "Machine not found in catalog"}
        return res.json()
    except Exception as e:
        return {"error": str(e)}


@tool
def get_price(machine_model: str) -> dict:
    """
    Get quoted price for a machine model.
    Use whenever user asks about price, cost, or rate.
    If the first attempt returns an error, retry with variations:
    - Strip brand prefix (e.g. 'DUKEJIA DY-1201' → try 'DY-1201')
    - Try without spaces (e.g. 'DY 1201' → try 'DY-1201')
    Args:
        machine_model: Machine model name e.g. DY-1201, DUKE R5, DUKE R9
    """
    try:
        res = httpx.get(
            f"{BASE}/api/price/{machine_model}",
            timeout=10
        )
        if res.status_code == 404:
            return {"error": "Price not available for this model"}
        return res.json()
    except Exception as e:
        return {"error": str(e)}
```

---

### `python-agent/agent.py`
```python
import json
import os
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from tools import search_machines, get_machine_details, get_price

SYSTEM_PROMPT = """You are HCA's product assistant for Hari Chand Anand & Co. — an industrial sewing machinery company in India.
You help salespeople and customers find the right machine and price instantly.

Rules:
1. Only use data returned by your tools. Never guess specs or prices.
2. For prices: ALWAYS call get_price. If the first attempt returns an error, retry with variations:
   - Strip brand prefix (e.g. "DUKEJIA DY-1201" → try "DY-1201")
   - Try without spaces (e.g. "DY 1201" → try "DY-1201")
   - Try the key/code from search results if available
   Make at least 2 attempts before saying price is unavailable.
3. When a user asks about a machine AND its price, call search_machines and get_price in the same turn.
4. For deal pricing: say "For the best deal, I'll connect you with our sales team."
5. Be direct and brief — salespeople are on live calls.
6. If a machine is not in the catalog after searching, say so clearly."""

TOOLS = [search_machines, get_machine_details, get_price]

# ── LLM ─────────────────────────────────────────────────────────────────
llm = ChatGroq(
    model="meta-llama/llama-4-scout-17b-16e-instruct",
    api_key=os.getenv("GROQ_API_KEY"),
    max_retries=2,
)

# ── Postgres checkpointer ────────────────────────────────────────────────
DB_URI = os.getenv("DATABASE_URL")

_pool = None
_graph = None


def get_graph():
    """Lazy-init: build graph + pool once, reuse across requests."""
    global _pool, _graph
    if _graph is not None:
        return _graph

    _pool = ConnectionPool(
        conninfo=DB_URI,
        max_size=20,
        kwargs={"autocommit": True},
    )
    checkpointer = PostgresSaver(_pool)
    checkpointer.setup()   # creates langgraph_checkpoint tables (idempotent)

    _graph = create_react_agent(
        model=llm,
        tools=TOOLS,
        state_modifier=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )
    return _graph


# ── Cards extraction ─────────────────────────────────────────────────────
def extract_cards(messages: list) -> list:
    """
    Parse tool messages from the final LangGraph state and build
    the same card structures the frontend expects.
    """
    cards = []

    # Map tool_call_id → tool name from AI messages
    tool_name_map: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_name_map[tc["id"]] = tc["name"]

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        tool_name = tool_name_map.get(msg.tool_call_id, "")
        try:
            content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content

            if tool_name == "search_machines" and isinstance(content, list) and content:
                # Filter out error-only results
                machines = [m for m in content if "error" not in m]
                if machines:
                    cards.append({"type": "machine_list", "machines": machines})

            elif tool_name == "get_machine_details" and isinstance(content, dict) and "error" not in content:
                cards.append({"type": "machine_detail", "machine": content})

            elif tool_name == "get_price" and isinstance(content, dict) and "error" not in content:
                cards.append({
                    "type":            "price",
                    "model":           content.get("model"),
                    "quote_price_inr": content.get("quote_price_inr"),
                    "currency":        content.get("currency", "INR"),
                })
        except Exception:
            pass

    return cards


# ── Public interface ─────────────────────────────────────────────────────
def run_chat(message: str, thread_id: str) -> dict:
    """
    Invoke the graph for one user message.
    Returns { answer, cards, history_turns }.
    Thread state (full history) is loaded/saved automatically via PostgresSaver.
    """
    from langchain_core.messages import HumanMessage

    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke(
        {"messages": [HumanMessage(content=message)]},
        config=config,
    )

    messages      = result["messages"]
    answer        = messages[-1].content if messages else ""
    cards         = extract_cards(messages)
    history_turns = sum(1 for m in messages if isinstance(m, AIMessage))

    return {
        "answer":        answer,
        "cards":         cards,
        "history_turns": history_turns,
    }
```

---

### `python-agent/main.py`
```python
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from agent import get_graph, run_chat


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up: create pool + run checkpointer.setup() at startup
    get_graph()
    yield


app = FastAPI(title="HCA Python Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    thread_id: str


class ChatResponse(BaseModel):
    answer:        str
    cards:         list
    history_turns: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message required")
    try:
        result = run_chat(req.message, req.thread_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

---

### `python-agent/Dockerfile`
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system deps for psycopg binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Single worker is fine for start; scale with --workers if needed
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

---

### `docker-compose.yml` (project root)
```yaml
version: '3.9'

services:

  postgres:
    image: postgres:16-alpine
    container_name: hca_postgres
    environment:
      POSTGRES_DB:       hca_agent
      POSTGRES_USER:     hca
      POSTGRES_PASSWORD: hca_secret
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U hca -d hca_agent"]
      interval: 5s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  python-agent:
    build: ./python-agent
    container_name: hca_python_agent
    env_file: ./backend/.env
    environment:
      DATABASE_URL: postgresql://hca:hca_secret@postgres:5432/hca_agent
      # Node.js runs on the HOST — use host.docker.internal (Mac/Windows)
      # On Linux: use your machine's LAN IP or add extra_hosts below
      API_BASE: http://host.docker.internal:3001
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped

  # ── Linux-only: uncomment these two blocks ──────────────────────────
  # extra_hosts:
  #   - "host.docker.internal:host-gateway"

volumes:
  postgres_data:
```

---

## 4. Files to Modify

### `backend/.env` — add 2 lines
```
PORT=3001

SHEET_ID=1ZgrFVcvyDhrDNVorfkYJ4xDG-b2mhu96_ihGAQlNtvQ
SHEET_GID=0

ADMIN_PASSWORD=12345
JWT_SECRET=1234567890

GROQ_API_KEY=gsk_your_groq_api_key_here

# ── NEW ──────────────────────────────────────────────
PYTHON_AGENT_URL=http://localhost:8000
DATABASE_URL=postgresql://hca:hca_secret@localhost:5432/hca_agent
```

---

### `backend/server.js` — two changes

**Change 1:** Replace the agent import at the top (around line 286):

Remove:
```javascript
import { chat } from './agent.js';
```

Add (at the top of the file, with the other imports):
```javascript
import { randomUUID } from 'crypto';
```

And set the Python agent URL after the `const PORT` line:
```javascript
const PYTHON_AGENT_URL = process.env.PYTHON_AGENT_URL || 'http://localhost:8000';
```

**Change 2:** Replace the `/api/chat` endpoint entirely:

Old:
```javascript
app.post('/api/chat', async (req, res) => {
  const { message, history } = req.body;
  if (!message?.trim()) return res.status(400).json({ error: 'Message required' });

  const startTime = Date.now();
  try {
    const result = await chat(message, history || []);

    logQuery({
      timestamp:      new Date().toISOString(),
      query:          message,
      answer:         result.answer,
      tools_used:     (result.cards || []).map(c => c.type),
      response_ms:    Date.now() - startTime,
      history_turns:  Math.floor((history || []).length / 2)
    });

    res.json(result);
  } catch (e) {
    console.error('Agent error:', e);

    logQuery({
      timestamp:     new Date().toISOString(),
      query:         message,
      error:         e.message,
      tools_used:    [],
      response_ms:   Date.now() - startTime,
      history_turns: Math.floor((history || []).length / 2)
    });

    res.status(500).json({ error: e.message });
  }
});
```

New:
```javascript
app.post('/api/chat', async (req, res) => {
  const { message, thread_id } = req.body;
  if (!message?.trim()) return res.status(400).json({ error: 'Message required' });

  const startTime = Date.now();
  const tid       = thread_id || randomUUID();   // first message gets a new thread

  try {
    const pyRes = await fetch(`${PYTHON_AGENT_URL}/chat`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message, thread_id: tid }),
    });

    if (!pyRes.ok) {
      const errText = await pyRes.text();
      throw new Error(`Python agent ${pyRes.status}: ${errText}`);
    }

    const result = await pyRes.json();

    logQuery({
      timestamp:     new Date().toISOString(),
      thread_id:     tid,
      query:         message,
      answer:        result.answer,
      tools_used:    (result.cards || []).map(c => c.type),
      response_ms:   Date.now() - startTime,
      history_turns: result.history_turns || 0,
    });

    res.json({
      answer:    result.answer,
      cards:     result.cards || [],
      thread_id: tid,
      history:   [],            // kept for frontend compat; state lives in postgres now
    });

  } catch (e) {
    console.error('Agent error:', e);

    logQuery({
      timestamp:   new Date().toISOString(),
      thread_id:   tid,
      query:       message,
      error:       e.message,
      tools_used:  [],
      response_ms: Date.now() - startTime,
    });

    res.status(500).json({ error: e.message });
  }
});
```

---

### `rag.html` — 4-line change in the `<script>` block

**Line to add** (after `let history = [];`):
```javascript
let threadId = null;
```

**In `send()` — update the fetch body:**

Old:
```javascript
body: JSON.stringify({ message: text, history })
```

New:
```javascript
body: JSON.stringify({ message: text, thread_id: threadId })
```

**In the success handler — capture the thread_id:**

Old:
```javascript
history = data.history || [];
addBotRow(data.answer, data.cards || []);
```

New:
```javascript
threadId = data.thread_id || threadId;   // persist thread across turns
history  = data.history  || [];          // kept for rollback compat
addBotRow(data.answer, data.cards || []);
```

---

### `.gitignore` — add Python entries
```
node_modules/
.env
logs/
*.log
python-agent/.venv/
python-agent/__pycache__/
**/__pycache__/
*.pyc
```

---

## 5. Step-by-Step Setup

```bash
# 1. Start postgres + python-agent
docker compose up -d --build

# 2. Verify python agent is healthy
curl http://localhost:8000/health
# → {"status":"ok"}

# 3. Run a test query directly against the Python agent
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "price of DUKE R9", "thread_id": "test-001"}'
# → {"answer":"...","cards":[...],"history_turns":1}

# 4. Start Node.js backend (unchanged command)
cd backend && node server.js

# 5. Open rag.html in browser and test the chat
```

---

## 6. What Gets Stored in PostgreSQL

LangGraph's `PostgresSaver` automatically creates and manages these tables:

| Table | Content |
|---|---|
| `checkpoints` | Full serialized graph state per thread snapshot |
| `checkpoint_blobs` | Message blobs (HumanMessage, AIMessage, ToolMessage) |
| `checkpoint_writes` | Pending writes / intermediate states |

Each conversation = one `thread_id`. History loads automatically on every call — no manual SQL needed.

To inspect conversations directly:
```sql
-- List all threads
SELECT DISTINCT thread_id FROM checkpoints ORDER BY 1;

-- See message count per thread
SELECT thread_id, COUNT(*) as snapshots FROM checkpoints GROUP BY 1;
```

---

## 7. Query Log — New Fields

After migration, each `query_log.jsonl` entry gains `thread_id` and accurate `history_turns`:
```json
{
  "timestamp":     "2026-06-10T09:14:22.411Z",
  "thread_id":     "a3f1c8e2-...",
  "query":         "price of DY-1201H",
  "answer":        "The DY-1201H is priced at ₹...",
  "tools_used":    ["price"],
  "response_ms":   1187,
  "history_turns": 3
}
```

---

## 8. Rollback Plan

If something breaks, revert to the old agent in under 2 minutes:

1. In `server.js` — restore the old `/api/chat` block and re-add `import { chat } from './agent.js';`
2. In `rag.html` — restore `body: JSON.stringify({ message: text, history })` and `history = data.history || []`
3. `docker compose stop python-agent` (postgres can stay running, harmless)
4. Restart Node.js

`agent.js` is kept (not deleted) specifically for this reason.

---

## 9. Production Hardening Checklist

- [ ] Rotate `ADMIN_PASSWORD` and `JWT_SECRET` from dev defaults before deploying
- [ ] Set strong `POSTGRES_PASSWORD` in docker-compose (not `hca_secret`)
- [ ] Add postgres port `5432` to firewall rules — should NOT be public-facing
- [ ] Set `PYTHON_AGENT_URL` to internal network address in production (not `localhost`)
- [ ] Add `uvicorn --workers 2` once load justifies it
- [ ] Set up postgres backups (`pg_dump` cron or managed DB service)
- [ ] Add log rotation for `logs/query_log.jsonl` (logrotate or weekly script)
