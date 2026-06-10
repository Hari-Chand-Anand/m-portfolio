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
