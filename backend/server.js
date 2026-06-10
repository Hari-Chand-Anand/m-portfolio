import express from "express";
import cors from "cors";
import jwt from "jsonwebtoken";
import dotenv from "dotenv";
import { readFileSync, appendFileSync, mkdirSync, existsSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { randomUUID } from 'crypto';

dotenv.config();

const app = express();

/* =========================
   ✅ CORS FIX (Codespaces + Local)
   ========================= */
const allowOrigin = (origin) => {
  if (!origin || origin === "null") return true; // file:// pages send origin "null"
  return (
    origin.endsWith(".app.github.dev") ||
    origin.startsWith("http://localhost") ||
    origin.startsWith("http://127.0.0.1")
  );
};

app.use(
  cors({
    origin: (origin, cb) => {
      if (allowOrigin(origin)) return cb(null, true);
      return cb(new Error("CORS blocked: " + origin));
    },
    methods: ["GET", "POST", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization"],
  })
);
app.options("*", cors());
app.use(express.json());

const PORT = process.env.PORT || 3001;
const SHEET_ID = process.env.SHEET_ID;
const SHEET_GID = process.env.SHEET_GID || "0";

/* =========================
   ✅ Google Sheet CSV (LIVE)
   ========================= */
function sheetCsvUrl() {
  if (!SHEET_ID) throw new Error("Missing SHEET_ID in .env");
  return `https://docs.google.com/spreadsheets/d/${SHEET_ID}/gviz/tq?tqx=out:csv&gid=${SHEET_GID}`;
}

function parseCsv(csvText) {
  const rows = [];
  let row = [];
  let cur = "";
  let inQuotes = false;

  for (let i = 0; i < csvText.length; i++) {
    const ch = csvText[i];
    const next = csvText[i + 1];

    if (ch === '"' && inQuotes && next === '"') {
      cur += '"';
      i++;
      continue;
    }
    if (ch === '"') {
      inQuotes = !inQuotes;
      continue;
    }
    if (ch === "," && !inQuotes) {
      row.push(cur);
      cur = "";
      continue;
    }
    if ((ch === "\n" || ch === "\r") && !inQuotes) {
      if (ch === "\r" && next === "\n") i++;
      row.push(cur);
      rows.push(row);
      row = [];
      cur = "";
      continue;
    }
    cur += ch;
  }
  if (cur.length || row.length) {
    row.push(cur);
    rows.push(row);
  }

  const header = (rows.shift() || []).map((h) => String(h || "").trim());
  return rows
    .filter((r) => r.some((x) => String(x || "").trim() !== ""))
    .map((r) => {
      const obj = {};
      header.forEach((h, idx) => (obj[h] = (r[idx] ?? "").toString().trim()));
      return obj;
    });
}

async function readRowsLive() {
  const url = sheetCsvUrl();
  const res = await fetch(url);
  const text = await res.text();

  if (!res.ok) {
    throw new Error(`Google Sheet fetch failed: ${res.status} ${text.slice(0, 180)}`);
  }

  // If sharing is OFF, Google sometimes returns HTML instead of CSV
  if (text.trim().startsWith("<!doctype") || text.includes("<html")) {
    throw new Error(
      "Google Sheet not accessible. Set Share → Anyone with the link → Viewer."
    );
  }

  return parseCsv(text);
}

/* =========================
   ✅ Matching
   ========================= */
function norm(s) {
  return String(s || "")
    .trim()
    .toLowerCase()
    .replace(/[\u00A0]/g, " ")
    .replace(/[-_]/g, " ")
    .replace(/\(.*?\)/g, "")
    .replace(/\s+/g, " ");
}

function findByModel(rows, model) {
  const q = norm(model);

  // 1. Exact match on key column (actual model codes e.g. DY-1201H, GC-188)
  let row = rows.find((r) => norm(r["key"]) === q);
  if (row) return row;

  // 2. Exact match on model column (brand names)
  row = rows.find((r) => norm(r["model"]) === q);
  if (row) return row;

  // 3. Contains match on key column — "dy 1201" matches "dy 1201h"
  row = rows.find((r) => {
    const k = norm(r["key"]);
    return k.includes(q) || q.includes(k);
  });
  if (row) return row;

  // 4. Contains match on model column
  row = rows.find((r) => {
    const m = norm(r["model"]);
    return m.includes(q) || q.includes(m);
  });
  return row || null;
}

/* =========================
   ✅ Quote price from sheet
   (Your sheet has "quote price")
   ========================= */
function quoteFromRow(row) {
  const q = Number(row["quote price"]);
  return Number.isFinite(q) ? Math.round(q) : null;
}

/* =========================
   ✅ Auth
   ========================= */
function requireAdmin(req, res, next) {
  const auth = req.headers.authorization || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";

  if (!token) return res.status(401).json({ error: "Not logged in" });

  try {
    jwt.verify(token, process.env.JWT_SECRET);
    next();
  } catch {
    return res.status(401).json({ error: "Invalid/expired login" });
  }
}

app.post("/api/login", (req, res) => {
  const { password } = req.body || {};
  if (!password) return res.status(400).json({ error: "Password required" });

  if (password !== process.env.ADMIN_PASSWORD) {
    return res.status(401).json({ error: "Wrong password" });
  }

  const token = jwt.sign({ role: "admin" }, process.env.JWT_SECRET, { expiresIn: "12h" });
  res.json({ token });
});

/* =========================
   ✅ Routes
   ========================= */
app.get("/", (req, res) => {
  res.send("✅ Backend is running. Use /api/price/DUKE%20R9");
});

app.get("/api/debug", async (req, res) => {
  try {
    const rows = await readRowsLive();
    res.json({
      sheetCsv: sheetCsvUrl(),
      rowCount: rows.length,
      headers: rows[0] ? Object.keys(rows[0]) : [],
      sampleModels: rows.slice(0, 10).map((r) => r["model"]),
    });
  } catch (e) {
    res.status(500).json({ error: String(e.message || e) });
  }
});

app.get("/api/price/:model", async (req, res) => {
  try {
    const rows = await readRowsLive();
    const row = findByModel(rows, req.params.model);
    if (!row) return res.status(404).json({ error: "Model not found" });

    res.json({
      model: req.params.model,
      quote_price_inr: quoteFromRow(row),
      currency: "INR",
    });
  } catch (e) {
    res.status(500).json({ error: String(e.message || e) });
  }
});

app.get("/api/admin/price/:model", requireAdmin, async (req, res) => {
  try {
    const rows = await readRowsLive();
    const row = findByModel(rows, req.params.model);
    if (!row) return res.status(404).json({ error: "Model not found" });

    res.json({
      model: req.params.model,
      quote_price_inr: quoteFromRow(row),
      fx_override: row["FX_OVERRIDE (for testing)"] ?? null,
      live_currency: row["chinese live currency"] ?? row["live currency"] ?? null,
    });
  } catch (e) {
    res.status(500).json({ error: String(e.message || e) });
  }
});


// GET /api/admin/logs — view query log (admin only)
app.get('/api/admin/logs', requireAdmin, (req, res) => {
  try {
    if (!existsSync(LOG_FILE)) return res.json({ total: 0, entries: [] });

    const lines = readFileSync(LOG_FILE, 'utf8')
      .split('\n')
      .filter(Boolean)
      .map(l => JSON.parse(l));

    // Optional ?limit=N and ?offset=N for pagination
    const limit  = Math.min(parseInt(req.query.limit  || '200', 10), 1000);
    const offset = parseInt(req.query.offset || '0', 10);
    const total  = lines.length;
    const entries = lines.slice(-(offset + limit), total - offset).reverse();

    res.json({ total, entries });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Catalog routes ──────────────────────────────
const __filename = fileURLToPath(import.meta.url);
const __dirname  = dirname(__filename);
const PRODUCTS   = JSON.parse(
  readFileSync(join(__dirname, '../products.json'), 'utf8')
);

// ── Query Logging ────────────────────────────────
const LOG_DIR  = join(__dirname, '../logs');
const LOG_FILE = join(LOG_DIR, 'query_log.jsonl');

function logQuery(entry) {
  try {
    if (!existsSync(LOG_DIR)) mkdirSync(LOG_DIR, { recursive: true });
    appendFileSync(LOG_FILE, JSON.stringify(entry) + '\n', 'utf8');
  } catch (e) {
    console.error('⚠️  Log write failed:', e.message);
  }
}

// GET /api/machines — all machines, optional filters
app.get('/api/machines', (req, res) => {
  const { brand, category, q } = req.query;
  let results = PRODUCTS;

  if (brand)    results = results.filter(m => m.brand?.toLowerCase() === brand.toLowerCase());
  if (category) results = results.filter(m => m.category?.toLowerCase() === category.toLowerCase());
  if (q) {
    const query = q.toLowerCase();
    results = results.filter(m =>
      m.name?.toLowerCase().includes(query) ||
      m.model?.toLowerCase().includes(query) ||
      m.description?.toLowerCase().includes(query) ||
      (m.tags || []).some(t => t.toLowerCase().includes(query))
    );
  }

  res.json(results);
});

// GET /api/machines/:id — single machine full detail
app.get('/api/machines/:id', (req, res) => {
  const machine = PRODUCTS.find(m => m.id === req.params.id);
  if (!machine) return res.status(404).json({ error: 'Machine not found' });
  res.json(machine);
});

// ── Agent chat endpoint ──────────────────────────
const PYTHON_AGENT_URL = process.env.PYTHON_AGENT_URL || 'http://localhost:8000';

app.post('/api/chat', async (req, res) => {
  const { message, thread_id } = req.body;
  if (!message?.trim()) return res.status(400).json({ error: 'Message required' });

  const startTime = Date.now();
  const tid       = thread_id || randomUUID();

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
      history:   [],
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

app.listen(PORT, () => {
  console.log(`✅ Backend running at http://localhost:${PORT}`);
});
  