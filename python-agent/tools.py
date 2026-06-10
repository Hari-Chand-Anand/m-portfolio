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
        if not data:
            return [{"message": "No machines found. Available categories: embroidery, lockstitch, finishing, industrial, sewing."}]
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
def get_projects() -> list:
    """
    Get all HCA manufacturing project lines (complete factory setups).
    Use when asked about production lines, complete setups, shirt line,
    denim line, gloves line, or what manufacturing solutions HCA offers.
    Returns project title and catalog URL for each line.
    """
    try:
        res = httpx.get(f"{BASE}/api/projects", timeout=10)
        data = res.json()
        if not data:
            return [{"message": "No project lines found."}]
        return data
    except Exception as e:
        return [{"error": str(e)}]


@tool
def get_installation_stats(brand: str = "", city: str = "") -> dict:
    """
    Get geographic installation statistics for HCA machines across India.
    Use when asked: where are machines installed, which cities, how many
    installations, geographic presence, market reach, or city-wise data.
    Args:
        brand: Optional brand filter e.g. DUKE, DUKEJIA, HIGHLEAD, EPA
        city: Optional city filter e.g. Noida, Ludhiana, Delhi, Bangalore
    """
    try:
        params = {k: v for k, v in {"brand": brand, "city": city}.items() if v}
        res = httpx.get(f"{BASE}/api/installations/stats", params=params, timeout=10)
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
