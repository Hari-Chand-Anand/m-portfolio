import os
import httpx
from langchain_core.tools import tool

BASE = os.getenv("API_BASE", "http://localhost:3001")

@tool
def search_machines(q: str = "", brand: str = "", category: str = "") -> list:
    """
    Search the HCA machine catalog.
    ALWAYS call this tool for any question about machines, brands, or categories.
    Never answer catalog questions from memory -- call this first, every time.
    Args:
        q: Keyword search term
        brand: Filter by brand name (e.g. DUKE, HIGHLEAD, HIKARI, EPA, GRAND, LOIVA, MERROW, PFAFF, ZOJE, VIOS, JUITA, HUTANG, LENSH, DUKEJIA)
        category: Filter by category
    """
    params = {k: v for k, v in {"q": q, "brand": brand, "category": category}.items() if v}
    try:
        res = httpx.get(f"{BASE}/api/machines", params=params, timeout=10)
        data = res.json()
        if not data:
            return [{"message": "No machines found for that query."}]

        # No filters -- return a brand-grouped summary so the agent surfaces
        # all 15 brands rather than just the first N machines from the list.
        if not q and not brand and not category:
            brand_counts = {}
            brand_samples = {}
            for m in data:
                b = m.get("brand", "Unknown")
                brand_counts[b] = brand_counts.get(b, 0) + 1
                if b not in brand_samples:
                    brand_samples[b] = m.get("model") or m.get("name") or ""
            summary = [
                {"brand": b, "total_machines": brand_counts[b], "example_model": brand_samples[b]}
                for b in sorted(brand_counts, key=lambda x: -brand_counts[x])
            ]
            return summary

        # Filtered search -- return up to 20 results
        return data[:20]
    except Exception as e:
        return [{"error": str(e)}]


@tool
def get_machine_details(id: str) -> dict:
    """
    Get complete specs for one specific machine by its ID.
    Use when the user asks for full details or specifications of a specific model.
    Args:
        id: Machine ID from search results
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
    Get all HCA manufacturing project lines and complete factory setups.
    Call this when asked about production lines, complete setups, or manufacturing solutions.
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
    Call this for any question about where machines are installed, city presence,
    number of installations, or geographic reach.
    Args:
        brand: Optional brand filter
        city: Optional city filter
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
    Get the quoted price for a machine model.
    Call this whenever the user asks about price, cost, or rate of any machine.
    If first attempt fails, retry with stripped brand prefix or normalised model name.
    Args:
        machine_model: Machine model name
    """
    try:
        res = httpx.get(f"{BASE}/api/price/{machine_model}", timeout=10)
        if res.status_code == 404:
            return {"error": "Price not available for this model"}
        return res.json()
    except Exception as e:
        return {"error": str(e)}


@tool
def search_catalog_docs(query: str, model: str = "") -> list:
    """
    Search technical documentation extracted from machine catalog PDFs.
    Call this for detailed technical specs, stitch types, thread specifications,
    needle requirements, motor specs, tension settings, or any technical detail
    not available from basic catalog search.
    Args:
        query: Technical term to search for e.g. stitch length, needle type, motor power
        model: Optional machine model to narrow search e.g. DY-1201
    """
    try:
        params = {k: v for k, v in {"query": query, "model": model}.items() if v}
        res = httpx.get(f"{BASE}/api/catalog-docs/search", params=params, timeout=10)
        if res.status_code == 404:
            return [{"message": "Catalog documentation not yet indexed."}]
        data = res.json()
        if not data:
            return [{"message": "No technical documentation found for that query."}]
        return data[:10]
    except Exception as e:
        return [{"error": str(e)}]
