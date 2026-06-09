import dotenv from "dotenv";
dotenv.config();

const API_KEY  = process.env.GROQ_API_KEY || process.env.GOOGLE_API_KEY;
const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";
const MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct";

const SYSTEM = `You are HCA's product assistant for Hari Chand Anand & Co. — an industrial sewing machinery company in India.
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
6. If a machine is not in the catalog after searching, say so clearly.`;

const TOOLS = [
  {
    type: "function",
    function: {
      name: "search_machines",
      description: "Search HCA machine catalog by keyword, brand, or category. Use for finding machines, listing options, recommendations.",
      parameters: {
        type: "object",
        properties: {
          q:        { type: "string", description: "Keyword e.g. embroidery, lockstitch, button hole, overlock" },
          brand:    { type: "string", description: "DUKE or DUKEJIA" },
          category: { type: "string", description: "Category e.g. embroidery, lockstitch" }
        }
      }
    }
  },
  {
    type: "function",
    function: {
      name: "get_machine_details",
      description: "Get complete specs for one specific machine. Use when user asks about a specific model or wants full details.",
      parameters: {
        type: "object",
        properties: {
          id: { type: "string", description: "Machine ID from search results e.g. duke-dk-1201-embroidery-machine" }
        },
        required: ["id"]
      }
    }
  },
  {
    type: "function",
    function: {
      name: "get_price",
      description: "Get quoted price for a machine model. Use whenever user asks about price, cost, or rate.",
      parameters: {
        type: "object",
        properties: {
          machine_model: { type: "string", description: "Machine model name e.g. DY-1201, DUKE R5, DUKE R9" }
        },
        required: ["machine_model"]
      }
    }
  }
];

const BASE = process.env.API_BASE || "http://localhost:3001";

async function runTool(name, args) {
  try {
    if (name === "search_machines") {
      const p = new URLSearchParams();
      if (args.q)        p.set("q", args.q);
      if (args.brand)    p.set("brand", args.brand);
      if (args.category) p.set("category", args.category);
      const res  = await fetch(`${BASE}/api/machines?${p}`);
      const data = await res.json();
      return data.slice(0, 8).map(m => ({
        id:          m.id,
        brand:       m.brand,
        model:       m.model,
        category:    m.category,
        operation:   m.operation,
        description: m.description,
        catalogUrl:  m.media?.catalogUrl
      }));
    }

    if (name === "get_machine_details") {
      const res = await fetch(`${BASE}/api/machines/${args.id}`);
      if (!res.ok) return { error: "Machine not found in catalog" };
      const m = await res.json();
      return {
        id: m.id, brand: m.brand, model: m.model,
        category: m.category, operation: m.operation,
        description: m.description, spec: m.spec,
        features: m.features, application: m.application,
        catalogUrl: m.media?.catalogUrl
      };
    }

    if (name === "get_price") {
      const res = await fetch(`${BASE}/api/price/${encodeURIComponent(args.machine_model)}`);
      if (!res.ok) return { error: "Price not available for this model" };
      return await res.json();
    }

    return { error: `Unknown tool: ${name}` };

  } catch (e) {
    return { error: e.message };
  }
}

export async function chat(userMessage, history = []) {
  if (!API_KEY) throw new Error("No API key found. Set GROQ_API_KEY in backend/.env");

  // Build message list: system + history (OpenAI format) + new user message
  const messages = [
    { role: "system", content: SYSTEM },
    ...history,
    { role: "user", content: userMessage }
  ];

  let finalText = "";
  const cards = [];

  for (let round = 0; round < 5; round++) {
    const res = await fetch(GROQ_URL, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${API_KEY}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ model: MODEL, messages, tools: TOOLS, tool_choice: "auto", parallel_tool_calls: false })
    });

    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Groq API error ${res.status}: ${errText}`);
    }

    const data    = await res.json();
    const message = data.choices[0].message;
    messages.push(message);

    const toolCalls = message.tool_calls || [];

    if (toolCalls.length === 0) {
      finalText = message.content || "";
      break;
    }

    // Execute all tool calls
    for (const tc of toolCalls) {
      const name   = tc.function.name;
      const args   = JSON.parse(tc.function.arguments || "{}");
      const result = await runTool(name, args);

      // Collect structured card data for the UI
      if (name === "search_machines" && Array.isArray(result) && result.length > 0) {
        cards.push({ type: "machine_list", machines: result });
      } else if (name === "get_machine_details" && result && !result.error) {
        cards.push({ type: "machine_detail", machine: result });
      } else if (name === "get_price" && result && !result.error) {
        cards.push({ type: "price", model: result.model, quote_price_inr: result.quote_price_inr, currency: result.currency });
      }

      messages.push({
        role:         "tool",
        tool_call_id: tc.id,
        content:      JSON.stringify(result)
      });
    }
  }

  // Keep last 20 messages (excluding system) for next turn
  const updatedHistory = messages.slice(1).slice(-20);
  return { answer: finalText, history: updatedHistory, cards };
}
