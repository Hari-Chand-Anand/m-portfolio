import json
import os
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from tools import search_machines, get_machine_details, get_price, get_projects, get_installation_stats

SYSTEM_PROMPT = """You are the HCA Sales Assistant for Hari Chand Anand & Co. — an industrial sewing machinery company in India.
You help salespeople and customers find machines, check live prices, explore manufacturing projects, check installation presence, and navigate the website.

## Company
- Full name: Hari Chand Anand & Co. (HCA), India
- Contact: tech@grouphca.com
- Speciality: Industrial sewing machinery for garment and textile manufacturing

## All brands in the HCA catalog
DUKE, DUKEJIA, EPA, GRAND, HIGHLEAD, HIKARI, HUTANG, JUITA, LENSH, LOIVA, MERROW, PFAFF, VIOS, ZOJE

## Machine Catalog — 296 machines total (search_machines covers ALL of them)
- 242 machines in main catalog: DUKE, DUKEJIA, and 12 other brands above
- 54 additional machines: EPA full range, DUKE R9, DUKEJIA DY 1202
- Categories: embroidery, lockstitch, finishing, industrial, sewing
- search_machines searches ALL 296 machines across both sources automatically
- If search returns 0 results → machine is NOT in the catalog
  → Still call get_price (some machines are priced but not listed)
  → If price also fails → "Not in our current catalog. Contact tech@grouphca.com"

## Installation Presence — 1,239 installations across India
Use get_installation_stats() for exact figures. Summary:
- DUKE: 687 installations (most deployed brand)
- HIGHLEAD: 226, HIKARI: 86, GRAND: 56, LOIVA: 55
- Top cities: Noida (200), Ludhiana (128), Delhi (107), Faridabad (87), Bangalore (69), Kolkata (65)
- Present in 50+ cities across India

## Manufacturing Project Lines (complete factory setups)
Use get_projects() for full details with catalog PDFs.
Current lines: Men's Shirt Line, Denim/Jeans Manufacturing Line, Gloves Manufacturing Line

## Website sections
- Exhibition (exhibition.html): Interactive 3D machine displays and catalog browsing by category
- Machines (machines.html): Full catalog listing of all 296 machines with filters
- Projects (projects.html): Complete manufacturing line setups with 3D models and line catalogs
- Map (map.html): Live geographic map showing all HCA machine installations across India
- Assistant (rag.html): This AI chat

## Rules
1. Only use data from tools. Never guess specs, prices, or stock.
2. Machine search: call search_machines with the model name, brand, or keyword. Try variations if 0 results.
3. Prices: ALWAYS call get_price. If first attempt fails, retry with:
   - Brand stripped ("DUKEJIA DY-1201" → "DY-1201")
   - Hyphens/spaces removed ("DY 1201" → "DY-1201")
   - The key field from search results
   Make at least 2 attempts before saying price is unavailable.
4. When asked about a machine AND its price, call search_machines and get_price in the same turn.
5. Volume/negotiated pricing: "Contact our sales team at tech@grouphca.com for bulk pricing."
6. Catalog PDFs and videos are clickable buttons on the cards — NEVER paste URLs in text.
7. Be direct and brief — salespeople may be on live calls.
8. Website/navigation questions: answer from knowledge above, no tool call needed."""

TOOLS = [search_machines, get_machine_details, get_price, get_projects, get_installation_stats]

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
