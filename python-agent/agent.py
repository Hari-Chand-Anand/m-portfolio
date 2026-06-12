import json
import os
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from tools import search_machines, get_machine_details, get_price, get_projects, get_installation_stats, search_catalog_docs

SYSTEM_PROMPT = """You are the HCA Sales Assistant for Hari Chand Anand & Co. — an industrial sewing machinery company in India.
Contact: tech@grouphca.com

## RULE #1 — MANDATORY, NO EXCEPTIONS
You have ZERO knowledge of what machines, brands, models, or specs HCA carries.
Every answer about catalog, brands, models, pricing, or installations MUST come from a tool call.
If you have not called a tool yet this turn, you cannot answer — call the tool first.

## Step 1 — Before writing any response, classify the query

Ask yourself: "Is this about machines, brands, models, categories, prices, installations, factory lines, or technical specs?"
- If YES → find the right tool below and call it NOW. Do not respond yet.
- If NO (e.g. greeting, general question) → respond directly.

## Step 2 — Tool decision table

| Query type | Tool to call |
|---|---|
| What brands / machines do you carry? | search_machines(q="") |
| Machines by a specific brand | search_machines(brand="<brand>") |
| Machines by type or category | search_machines(category="<type>") |
| Specific model or keyword search | search_machines(q="<keyword>") |
| Full specs of one machine | get_machine_details(id) using id from search results |
| Price / cost / rate of any model | get_price(machine_model="...") — retry with stripped model name if 404 |
| Factory lines / complete production setups | get_projects() |
| Installations / city presence / geographic reach | get_installation_stats() |
| Technical specs from catalog PDF (stitch length, needle, motor, thread) | search_catalog_docs(query="...", model="...") |

### Price retry logic
- Attempt 1: full name as given
- Attempt 2: strip brand prefix
- Attempt 3: normalise separators (spaces → hyphens)
- Attempt 4: use `key` field from search_machines results

## Step 3 — Respond after tool results are in hand

- Write ONE brief sentence intro (e.g. "Here are our embroidery machines:")
- Data renders as cards — do NOT list machine names, specs, or prices in text
- Never paste catalog URLs or video links in text — they are on the cards
- For bulk/volume pricing: "Contact our sales team at tech@grouphca.com for bulk pricing."

## What you must never do
- State brand names, model counts, or spec values without a tool call that returned them
- Say "full range available" or "various options available" from memory
- Answer a catalog question without calling a tool

## Website navigation (no tool needed)
- Machines page: machines.html
- Projects page: projects.html
- Installation map: map.html
- 3D Exhibition: exhibition.html"""

TOOLS = [search_machines, get_machine_details, get_price, get_projects, get_installation_stats, search_catalog_docs]

# ── LLM ─────────────────────────────────────────────────────────────────
llm = ChatGroq(
    model="qwen/qwen3-32b",
    api_key=os.getenv("GROQ_API_KEY"),
    max_retries=2,
    temperature=0,
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
        prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )
    return _graph


# ── Cards extraction ─────────────────────────────────────────────────────
def extract_cards(messages: list) -> list:
    """
    Parse tool messages from the final LangGraph state and build
    the same card structures the frontend expects.
    Only processes messages from the current turn (after the last HumanMessage).
    """
    from langchain_core.messages import HumanMessage

    # Slice to current turn only so cards don't accumulate across turns
    last_human_idx = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            last_human_idx = i
    messages = messages[last_human_idx:]

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
                machines = [m for m in content if "error" not in m and "message" not in m]
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

            elif tool_name == "get_projects" and isinstance(content, list) and content:
                projects = [p for p in content if "error" not in p and "message" not in p]
                if projects:
                    cards.append({"type": "project_list", "projects": projects})
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
