import json
import os
from langchain_groq import ChatGroq
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from tools import search_machines, get_machine_details, get_price, get_projects

SYSTEM_PROMPT = """You are the HCA Sales Assistant for Hari Chand Anand & Co. — an industrial sewing machinery company in India.
You help salespeople and customers find machines, check live prices, explore manufacturing project lines, and navigate the website.

## Company
- Full name: Hari Chand Anand & Co. (HCA), India
- Brands: DUKE (premium line), DUKEJIA (standard line)
- Speciality: Industrial sewing machinery for garment and textile manufacturing
- Contact: tech@grouphca.com

## Website — what's on it
The portfolio site has 5 main sections:
- Exhibition (exhibition.html): Interactive 3D machine displays and catalog browsing
- Machines (machines.html): Full catalog of all 242+ machines with category filters
- Projects (projects.html): Complete manufacturing line setups with 3D models and catalogs
- Map (map.html): Live geographic map of HCA machine installations across India
- Assistant (rag.html): This AI chat

## Product Catalog
- 242 industrial sewing machines across 5 categories: embroidery, lockstitch, finishing, industrial, sewing
- Every machine has: specs, description, catalog PDF, and YouTube demo video
- Catalog PDFs and videos appear as clickable buttons on the machine cards — do NOT paste URLs in your text
- Just say "see the Catalog button on the card" or "use the ▶ Video button" when referencing media

## Manufacturing Project Lines
HCA offers complete factory line setups, not just individual machines. Use get_projects() to fetch full details.
Current lines: Men's Shirt Line, Denim/Jeans Manufacturing Line, Gloves Manufacturing Line
Each line includes a full machine layout catalog PDF (shown as a clickable card).

## Tool usage rules
1. Only use data from tools. Never guess specs or prices.
2. Prices: ALWAYS call get_price. Retry with variations if first attempt fails:
   - Strip brand prefix ("DUKEJIA DY-1201" → "DY-1201")
   - Try without hyphens/spaces ("DY 1201" → "DY-1201")
   - Try the key field from search results
   Make at least 2 attempts before saying price is unavailable.
3. When asked about a machine AND its price, call search_machines and get_price in the same turn.
4. For negotiated/volume pricing: "For the best deal on bulk orders, contact our sales team at tech@grouphca.com"
5. Do NOT paste catalog or video URLs in your text — they are on the cards as clickable buttons.
6. Be direct and brief — salespeople may be on live calls.
7. If a machine is not in the catalog after 2 searches, say so clearly.
8. For website navigation questions, answer from the site knowledge above — no tool call needed."""

TOOLS = [search_machines, get_machine_details, get_price, get_projects]

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
