/**
 * Mock backend for UI development.
 *
 * The real stack needs a database, a vector store and a local model, none of
 * which a front-end change should require. This serves the same contract as
 * the Worker on the same port (8787), so `npm run dev` proxies to it without
 * any configuration change.
 *
 *   node mock/server.mjs        then, in another terminal, npm run dev
 *
 * The fixtures below are shaped after real responses from the Python backend,
 * including the awkward cases the UI has to handle: an injected tenant
 * predicate, a refusal, and a citation that does not resolve.
 */

import { createServer } from "node:http";

const PORT = 8787;

const META = {
  app_name: "Enterprise Intelligence Copilot",
  personas: [
    {
      key: "admin",
      label: "Administrator",
      tenant: "all",
      tenant_id: 1,
      access_groups: ["public", "internal", "finance", "support", "security", "exec"],
      description: "Sees everything.",
    },
    {
      key: "analyst_na",
      label: "Analyst — North America",
      tenant: "NWC-NA",
      tenant_id: 1,
      access_groups: ["public", "internal", "finance"],
      description: "Finance access, tenant 1 only.",
    },
    {
      key: "guest",
      label: "Guest",
      tenant: "all",
      tenant_id: 1,
      access_groups: ["public"],
      description: "Public documents only.",
    },
  ],
  strategies: ["dense", "sparse", "hybrid", "reranked"],
  default_strategy: "reranked",
  sql_provider: "vanna",
  chat_model: "llama3.1:8b",
  index_version: "v1",
  document_count: 19,
  chunk_count: 130,
  example_questions: [
    "What is our refund policy for annual plans?",
    "Which five customers have the highest ARR?",
    "Show customers with more than three SLA breaches and summarise the SLA policy.",
    "Delete all customers",
  ],
};

const HEALTH = {
  status: "ok",
  checks: [
    { name: "copilot", ok: true, detail: "ready" },
    { name: "vector_store", ok: true, detail: "130 chunks indexed" },
    { name: "database", ok: true, detail: "reachable" },
    { name: "chat_model", ok: true, detail: "2 models available" },
  ],
};

const MULTI_SOURCE = {
  trace_id: "a71f3c9e2b04",
  question: "Show customers with more than three SLA breaches and summarise the SLA policy.",
  route: "multi_source",
  route_decided_by: "model",
  route_confidence: 0.91,
  status: "answered",
  grounded: true,
  answer: `Fifteen customers in your tenant have recorded more than three SLA breaches [S1]. The largest by breach count are **Aurora Systems** (11), **Beacon Retail** (9) and **Continental Freight** (8).

On the policy itself, the current **Service Level Agreement, v2.0**, effective 1 March 2026, sets these first-response targets [D1, 2.1]:

- Enterprise, P1 incidents: **15 minutes**
- Enterprise, P2 incidents: 1 hour
- Business tier, P1 incidents: 1 hour

A breach is recorded when first response exceeds the target, measured from ticket creation rather than from assignment [D2, 3.4]. Credits are applied automatically at the end of the billing period and do not require a claim [D1, 5.2].

Note that v1.0 of the same policy set the Enterprise P1 target at 30 minutes. Breach counts before 1 March 2026 were measured against that older target [D3].`,
  sources: [
    {
      id: "D1",
      type: "document",
      title: "Service Level Agreement",
      version: "2.0",
      section: "2.1 Response targets",
      effective_date: "2026-03-01",
      authority: "policy",
      score: 0.912,
      rerank_score: 0.977,
      retrieval_method: "hybrid",
      snippet:
        "Enterprise customers receive a first response to P1 incidents within 15 minutes, measured from the moment the ticket is created. P2 incidents receive a first response within one hour.",
    },
    {
      id: "D2",
      type: "document",
      title: "Support Escalation Guide",
      version: "1.3",
      section: "3.4 Measuring first response",
      effective_date: "2026-01-15",
      authority: "guidance",
      score: 0.847,
      rerank_score: 0.901,
      retrieval_method: "hybrid",
      snippet:
        "First response is measured from ticket creation, not from assignment. A ticket that sits unassigned is already consuming the response window.",
    },
    {
      id: "D3",
      type: "document",
      title: "Service Level Agreement",
      version: "1.0",
      section: "2.1 Response targets",
      effective_date: "2024-06-01",
      authority: "policy",
      score: 0.731,
      rerank_score: 0.688,
      retrieval_method: "dense",
      snippet:
        "SUPERSEDED. Enterprise customers receive a first response to P1 incidents within 30 minutes.",
    },
    {
      id: "S1",
      type: "sql_result",
      title: "analytics.vw_customer_risk",
      version: "",
      section: "",
      effective_date: "",
      authority: "",
      score: null,
      rerank_score: null,
      retrieval_method: "text_to_sql",
      snippet: "15 rows returned",
    },
  ],
  conflicts: [
    "DOC-SLA appears at versions 1.0, 2.0; the most recent effective date takes precedence.",
    "Evidence includes both binding policy and advisory guidance. Where they differ, the policy is authoritative.",
  ],
  notes: [],
  warnings: [],
  sql: {
    generated:
      "SELECT customer_name, sla_breaches, health_score\nFROM analytics.vw_customer_risk\nWHERE sla_breaches > 3\nORDER BY sla_breaches DESC",
    executed:
      "SELECT TOP (5000) customer_name, sla_breaches, health_score\nFROM analytics.vw_customer_risk\nWHERE sla_breaches > 3 AND tenant_id = 1 /* injected */\nORDER BY sla_breaches DESC",
    blocked: false,
    block_reason: null,
    tenant_injected: true,
    tables: ["analytics.vw_customer_risk"],
    join_count: 0,
    warnings: ["query reads a curated analytics view; business definitions are encoded there"],
    provider: "vanna",
  },
  columns: ["customer_name", "sla_breaches", "health_score"],
  rows: [
    { customer_name: "Aurora Systems", sla_breaches: 11, health_score: 34.2 },
    { customer_name: "Beacon Retail", sla_breaches: 9, health_score: 41.8 },
    { customer_name: "Continental Freight", sla_breaches: 8, health_score: 38.05 },
    { customer_name: "Dunmore Logistics", sla_breaches: 7, health_score: 52.4 },
    { customer_name: "Elmwood Health", sla_breaches: 6, health_score: 47.9 },
    { customer_name: "Fairview Media", sla_breaches: 5, health_score: 61.33 },
    { customer_name: "Granite Manufacturing", sla_breaches: 5, health_score: 55.7 },
    { customer_name: "Harborline Shipping", sla_breaches: 4, health_score: 66.2 },
  ],
  row_count: 15,
  truncated: false,
  timings_ms: {
    routing: 4480,
    retrieval: 121,
    sql_generation: 18240,
    sql_execution: 36,
    generation: 9110,
    total: 31987,
  },
  versions: {
    chat_model: "llama3.1:8b",
    prompt_version: "answer-v3",
    index_version: "v1",
    sql_provider: "vanna",
  },
};

const REFUSAL = {
  ...MULTI_SOURCE,
  trace_id: "ff20ab7c1d93",
  question: "Delete all customers",
  route: "refuse",
  route_decided_by: "rule",
  route_confidence: 1.0,
  status: "refused",
  grounded: false,
  answer:
    "I am read-only and cannot modify the database. This request was refused before any SQL was generated, so nothing was sent to the server.",
  sources: [],
  conflicts: [],
  notes: [],
  warnings: [],
  sql: null,
  columns: [],
  rows: [],
  row_count: 0,
  timings_ms: { routing: 1.2, total: 1.4 },
};

const server = createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");

  const send = (status, body) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };

  if (url.pathname === "/api/meta") return send(200, META);
  if (url.pathname === "/api/health") return send(200, HEALTH);

  if (url.pathname === "/api/ask" && req.method === "POST") {
    let raw = "";
    req.on("data", (chunk) => (raw += chunk));
    req.on("end", () => {
      let question = "";
      try {
        question = JSON.parse(raw).question ?? "";
      } catch {
        /* fall through to the default fixture */
      }
      const destructive = /\b(delete|drop|truncate|update|insert)\b/i.test(question);
      // A little latency, so the loading state is actually visible.
      setTimeout(
        () => send(200, { ...(destructive ? REFUSAL : MULTI_SOURCE), question }),
        destructive ? 200 : 1200,
      );
    });
    return;
  }

  send(404, { error: "not_found", detail: `No mock route for ${url.pathname}` });
});

server.listen(PORT, () => {
  console.log(`Mock copilot backend on http://127.0.0.1:${PORT}`);
  console.log("Vite proxies /api here. Ask anything; say 'delete' to see a refusal.");
});
