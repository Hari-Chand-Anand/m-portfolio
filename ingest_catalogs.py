"""
HCA Catalog PDF Ingestion Script
=================================
Run this once (and again whenever catalogs are updated) to download PDFs
from Google Drive, extract technical text, and store in Postgres for RAG search.

Prerequisites:
    pip install pdfplumber gdown psycopg2-binary

Usage:
    python ingest_catalogs.py

    # Ingest only specific brand:
    python ingest_catalogs.py --brand EPA

    # Force re-ingest all (even already processed):
    python ingest_catalogs.py --force

The script reads DATABASE_URL from backend/.env automatically.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# ── Load .env ─────────────────────────────────────────────────────────────────
ENV_FILE = Path(__file__).parent / "backend" / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://hca:hca_secret@localhost:5432/hca_agent"
)

# ── Imports (fail early with clear messages) ───────────────────────────────────
try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    sys.exit("Missing: pip install psycopg2-binary")

try:
    import pdfplumber
except ImportError:
    sys.exit("Missing: pip install pdfplumber")

try:
    import requests
except ImportError:
    sys.exit("Missing: pip install requests")


# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_file_id(drive_url: str) -> str | None:
    """Extract Google Drive file ID from a sharing URL."""
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", drive_url)
    return m.group(1) if m else None


def download_pdf(file_id: str, dest_path: str) -> bool:
    """Download a Google Drive PDF using requests (handles confirm redirect)."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
    try:
        resp = session.get(url, stream=True, timeout=30)
        if resp.status_code != 200:
            print(f"    ⚠  HTTP {resp.status_code}")
            return False
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            print(f"    ⚠  Got HTML instead of PDF — file not publicly shared")
            return False
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(32768):
                f.write(chunk)
        size = os.path.getsize(dest_path)
        if size < 1000:
            print(f"    ⚠  File too small ({size} bytes) — likely an error page")
            return False
        return True
    except Exception as e:
        print(f"    ⚠  Download failed: {e}")
        return False


def extract_text(pdf_path: str) -> str:
    """Extract all text from a PDF using pdfplumber."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text.strip())
            return "\n\n".join(pages)
    except Exception as e:
        print(f"    ⚠  PDF extract failed: {e}")
        return ""


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    """Split text into overlapping chunks for better search recall."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


# ── Database setup ─────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_docs (
    id          SERIAL PRIMARY KEY,
    machine_id  TEXT NOT NULL,
    brand       TEXT NOT NULL,
    model       TEXT NOT NULL,
    chunk_index INT  NOT NULL,
    content     TEXT NOT NULL,
    tsv         TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    ingested_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS catalog_docs_tsv_idx ON catalog_docs USING GIN (tsv);
CREATE INDEX IF NOT EXISTS catalog_docs_machine_idx ON catalog_docs (machine_id);
CREATE INDEX IF NOT EXISTS catalog_docs_brand_idx ON catalog_docs (brand);
"""


def setup_db(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    print("✅ Database table ready")


def already_ingested(conn, machine_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM catalog_docs WHERE machine_id = %s LIMIT 1", (machine_id,))
        return cur.fetchone() is not None


def delete_machine(conn, machine_id: str):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM catalog_docs WHERE machine_id = %s", (machine_id,))
    conn.commit()


def insert_chunks(conn, machine_id: str, brand: str, model: str, chunks: list[str]):
    rows = [(machine_id, brand.strip(), model.strip(), i, chunk)
            for i, chunk in enumerate(chunks)]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO catalog_docs (machine_id, brand, model, chunk_index, content) VALUES %s",
            rows
        )
    conn.commit()


# ── Main ───────────────────────────────────────────────────────────────────────

def load_catalog_urls() -> list[dict]:
    """Load all catalog entries from products.json and data/machines.json."""
    base = Path(__file__).parent
    entries = []

    for json_path, url_key in [
        (base / "products.json",       lambda m: m.get("media", {}).get("catalogUrl", "")),
        (base / "data" / "machines.json", lambda m: m.get("catalogUrl", "")),
    ]:
        data = json.loads(json_path.read_text())
        for m in data:
            url = url_key(m).strip()
            if not url or "example.com" in url or "drive.google.com" not in url:
                continue
            fid = extract_file_id(url)
            if not fid:
                continue
            entries.append({
                "id":      m.get("id", ""),
                "brand":   m.get("brand", ""),
                "model":   m.get("model", "").strip(),
                "file_id": fid,
            })

    return entries


def build_machine_text(m: dict, source: str) -> str:
    """Build a rich searchable text document from a machine's JSON data."""
    parts = []
    brand   = m.get("brand", "")
    model   = m.get("model") or m.get("name", "")
    cat     = m.get("category") or m.get("type", "")
    desc    = m.get("description") or m.get("short", "")

    if brand:  parts.append(f"Brand: {brand}")
    if model:  parts.append(f"Model: {model}")
    if cat:    parts.append(f"Category: {cat}")
    if desc:   parts.append(f"Description: {desc}")

    # products.json: spec is a dict
    spec = m.get("spec")
    if isinstance(spec, dict):
        parts.append("Specifications:")
        for k, v in spec.items():
            parts.append(f"  {k}: {v}")

    # data/machines.json: tags, operation, stockQty etc.
    if m.get("operation"):   parts.append(f"Operation: {m['operation']}")
    if m.get("tags"):        parts.append(f"Tags: {', '.join(m['tags'])}")
    if m.get("equivalents"): parts.append(f"Equivalent models: {', '.join(m['equivalents'])}")

    return "\n".join(parts)


def index_json_catalog(conn, force: bool = False, brand_filter: str = ""):
    """Index all 296 machines from products.json + data/machines.json into catalog_docs."""
    base = Path(__file__).parent
    total_ok = total_skip = 0

    for json_path in [base / "products.json", base / "data" / "machines.json"]:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        source = json_path.name

        for m in data:
            brand = m.get("brand", "")
            if brand_filter and brand.upper() != brand_filter.upper():
                continue

            machine_id = m.get("id", "")
            if not machine_id:
                continue

            # Skip if already indexed and not forcing
            if not force and already_ingested(conn, machine_id):
                total_skip += 1
                continue

            text = build_machine_text(m, source)
            if not text.strip():
                total_skip += 1
                continue

            if force:
                delete_machine(conn, machine_id)

            chunks = chunk_text(text, chunk_size=400, overlap=50)
            model_name = m.get("model") or m.get("name", "")
            insert_chunks(conn, machine_id, brand, model_name, chunks)
            total_ok += 1

    print(f"   JSON catalog: {total_ok} indexed, {total_skip} skipped")


def ingest_pdfs(conn, force: bool = False, brand_filter: str = ""):
    """Download catalog PDFs from Drive and ingest their text into catalog_docs."""
    entries = load_catalog_urls()

    if brand_filter:
        entries = [e for e in entries if e["brand"].upper() == brand_filter.upper()]

    print(f"\n📄 PDF ingestion: {len(entries)} machines with catalog PDFs")
    ok = skip = fail = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, entry in enumerate(entries, 1):
            machine_id = entry["id"]
            brand      = entry["brand"]
            model      = entry["model"]
            file_id    = entry["file_id"]

            print(f"  [{i}/{len(entries)}] {brand} {model} ... ", end="", flush=True)

            if not force and already_ingested(conn, machine_id):
                print("skipped (already indexed)")
                skip += 1
                continue

            pdf_path = os.path.join(tmpdir, f"{machine_id}.pdf")
            if not download_pdf(file_id, pdf_path):
                fail += 1
                continue

            text = extract_text(pdf_path)
            if not text.strip():
                print("no text extracted")
                fail += 1
                continue

            if force:
                delete_machine(conn, machine_id)

            chunks = chunk_text(text)
            insert_chunks(conn, machine_id, brand, model, chunks)
            print(f"done ({len(chunks)} chunks)")
            ok += 1
            time.sleep(0.5)  # be polite to Drive

    print(f"\n   PDF results: {ok} indexed, {skip} skipped, {fail} failed")
    return ok, fail


def main():
    parser = argparse.ArgumentParser(description="Ingest HCA machine catalog into Postgres for RAG search")
    parser.add_argument("--brand", default="", help="Only ingest machines for this brand (e.g. EPA, DUKE)")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if already indexed")
    parser.add_argument("--json-only", action="store_true", help="Only index JSON specs, skip PDF download")
    args = parser.parse_args()

    brand_filter = args.brand.strip().upper()

    print(f"Connecting to Postgres...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
    except Exception as e:
        sys.exit(f"Cannot connect to Postgres: {e}")

    setup_db(conn)

    # Step 1: Index structured JSON specs for all machines (fast, no download)
    print(f"\n📋 Indexing JSON catalog specs...")
    index_json_catalog(conn, force=args.force, brand_filter=brand_filter)

    # Step 2: Download and index PDF catalogs (slower, requires Drive access)
    if not args.json_only:
        ingest_pdfs(conn, force=args.force, brand_filter=brand_filter)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
