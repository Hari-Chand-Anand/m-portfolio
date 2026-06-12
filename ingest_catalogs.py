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
    import gdown
except ImportError:
    sys.exit("Missing: pip install gdown")


# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_file_id(drive_url: str) -> str | None:
    """Extract Google Drive file ID from a sharing URL."""
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", drive_url)
    return m.group(1) if m else None


def download_pdf(file_id: str, dest_path: str) -> bool:
    """Download a Google Drive PDF. Returns True on success."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        result = gdown.download(url, dest_path, quiet=True)
        return result is not None and os.path.getsize(dest_path) > 1000
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--brand",  help="Only ingest catalogs for this brand")
    parser.add_argument("--force",  action="store_true", help="Re-ingest already processed entries")
    parser.add_argument("--limit",  type=int, default=0, help="Max entries to process (0 = all)")
    args = parser.parse_args()

    print(f"\n🔌 Connecting to Postgres: {DATABASE_URL[:40]}...")
    conn = psycopg2.connect(DATABASE_URL)
    setup_db(conn)

    entries = load_catalog_urls()
    print(f"📋 Found {len(entries)} catalog entries")

    if args.brand:
        entries = [e for e in entries if e["brand"].lower() == args.brand.lower()]
        print(f"   Filtered to brand '{args.brand}': {len(entries)} entries")

    if args.limit:
        entries = entries[:args.limit]

    ok = skip = fail = 0

    for i, entry in enumerate(entries, 1):
        mid   = entry["id"]
        brand = entry["brand"]
        model = entry["model"]
        fid   = entry["file_id"]

        label = f"[{i}/{len(entries)}] {brand} {model}"

        if not args.force and already_ingested(conn, mid):
            print(f"  ⏭  {label} — already ingested")
            skip += 1
            continue

        print(f"  ↓  {label}")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            if not download_pdf(fid, tmp_path):
                print(f"    ✗  Download failed — skipping")
                fail += 1
                continue

            text = extract_text(tmp_path)
            if not text.strip():
                print(f"    ✗  No text extracted — skipping")
                fail += 1
                continue

            chunks = chunk_text(text)
            if args.force:
                delete_machine(conn, mid)
            insert_chunks(conn, mid, brand, model, chunks)
            print(f"    ✓  {len(chunks)} chunks stored ({len(text)} chars)")
            ok += 1

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # polite rate limiting
        time.sleep(0.5)

    conn.close()
    print(f"\n{'='*50}")
    print(f"Done.  ✅ {ok} ingested  |  ⏭ {skip} skipped  |  ✗ {fail} failed")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
