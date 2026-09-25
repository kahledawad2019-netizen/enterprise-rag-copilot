# RAG architecture

How a question becomes a cited answer in this repository - from documents
(vector RAG), from the company database (Text-to-SQL) or from both - what runs
where in the two deployment modes, and what is known to be weak.

## At a glance

| | Local (private) | Cloud (public demo) |
|---|---|---|
| Entry point | `run_local.py` → `cloud/api/server.py` | `cloud/streamlit/streamlit_app.py` on Streamlit Community Cloud (free, no card); `Dockerfile` for any Docker host |
| Frontend | React 19 + Vite (`cloud/ui`), served by the same process | Streamlit chat (same engine); the Docker image serves the React build |
| Backend | FastAPI (`cloud/api/main.py`) wrapping `enterprise_copilot.Copilot` | same |
| Chat model | Ollama, auto-picked (default `qwen3:4b-instruct-2507-q4_K_M`) | Groq `qwen/qwen3.8-27b` |
| Embeddings | Ollama `nomic-embed-text` (768-d) | fastembed ONNX `nomic-ai/nomic-embed-text-v1.5-Q` (768-d), in-process |
| Vector DB | embedded Qdrant, `local-enterprise-copilot/data/qdrant-local` | embedded Qdrant, built into the image |
| Sparse index | BM25 (`rank-bm25`), cached pickle next to the manifest | same |
| Structured data | DuckDB file, read-only, built on first run (`DATABASE_BACKEND=duckdb`) | same, built on first start (Streamlit) or at image build (Docker) |
| Text-to-SQL | Vanna (Chroma store) on Ollama; native generator if Vanna is absent | Vanna on Groq (Streamlit); native generator (Docker image) |
| Routing | rules + heuristics (`ROUTER_LLM=never`, saves ~50 s on CPU) | rules + LLM intent screen + LLM route classification |
| Auth | none (localhost, single user) | public demo + per-IP rate limit |
| Leaves the machine | nothing | prompts (question, retrieved passages, schema and query results) go to Groq |

One pipeline serves both modes. Only the two provider clients differ; there
is no second copy of retrieval, prompting or citation logic.

## The pipeline

```
documents (Markdown with YAML front matter, PDF, DOCX; uploads)
  │ ingestion/parsers.py       parse, clean, extract sections, metadata
  │ ingestion/chunking.py      structure-aware chunks (~400 tok, 60 overlap),
  │                            tables kept whole, breadcrumb prefix,
  │                            parent (whole section) + child chunks
  │ retrieval/embedder.py      model-specific prefixes (nomic: search_document:)
  ▼
Qdrant (cosine) + BM25 cache     manifest records model, dimension, version
  ▲
question
  │ routing/router.py          1. deterministic refusal rules (destructive /
  │                               exfiltration intent) - always
  │                            2. DIRECT rules: greeting / thanks / "what can
  │                               you do" -> template reply, nothing retrieved
  │                            3. semantic intent screen + LLM route
  │                               classification - only when ROUTER_LLM
  │                               decides it is worth it (see below)
  │                            4. heuristics fallback
  │   routes: document_rag | text_to_sql | multi_source (HYBRID) | clarify |
  │           refuse | direct
  │
  ├─ text_to_sql / multi_source ──────────────────────────────────────────┐
  │ text_to_sql/schema_retriever.py  relevant tables, join paths, glossary │
  │                                  definitions, golden SQL examples      │
  │ text_to_sql/vanna_provider.py    Vanna (Chroma) or native.py generates │
  │                                  PostgreSQL through the same chat      │
  │                                  client (Ollama or Groq)               │
  │ security/sql_guard.py            AST check: one SELECT, allowed        │
  │                                  schemas, no blocked columns; injects  │
  │                                  the tenant predicate and a row limit  │
  │ database/read_only_runner.py     audit, timeout, row cap, redaction;   │
  │                                  DuckDB: transpile to DuckDB, read-only│
  │                                  file handle                           │
  │ orchestrator (self-correction)   DB error -> model -> new SQL -> guard │
  │                                  again (max SQL_EXECUTION_RETRIES=2)   │
  │   -> evidence S1 (rows + the SQL that produced them)                   │
  └────────────────────────────────────────────────────────────────────────┘
  │   multi_source runs the document and SQL halves in parallel
  │ retrieval/hybrid.py        dense (Qdrant) + sparse (BM25), both
  │                            permission- and version-filtered BEFORE ranking
  │ retrieval/fusion.py        RRF (k=60) → dedupe (full-text hash) →
  │                            authority preference → optional cross-encoder
  │                            rerank → MMR (λ=0.7) → parent expansion
  │ models/evidence.py         evidence package, labelled D1..Dn (documents)
  │                            and S1..Sn (SQL rows); conflict detection
  │ generation/prompts.py      evidence inside <evidence> delimiters, marked as
  │                            data; injection rule stated before and after
  │ generation/answerer.py     batch or streamed generation
  │ generation/citations.py    every [Dn] checked against the package;
  │                            ungrounded / fabricated citations flagged
  ▼
answer + validated citations + trace (observability/tracing.py)
```

### Providers (`enterprise_copilot/llm/clients.py`)

`build_chat_client(settings)` returns an object with Ollama's `chat()` shape,
so every call site is provider-agnostic:

- `LLM_PROVIDER=ollama` → `OllamaChatClient`: the Ollama client plus
  reasoning-model handling. Reasoning builds (qwen3 thinking, deepseek-r1) put
  their chain of thought on a separate channel, which is discarded; any inline
  `<think>` block is stripped, including across stream chunks; the token budget
  is raised so reasoning cannot starve the answer; an empty answer is an error,
  not a blank reply.
- `LLM_PROVIDER=groq` → `OpenAICompatChatClient` against
  `https://api.groq.com/openai/v1`, key from `GROQ_API_KEY`, model from
  `GROQ_MODEL`. Server-side only; mid-stream error events and truncated streams
  raise instead of ending an answer silently.
- `LLM_PROVIDER=openai` → any other OpenAI-compatible endpoint.
- **Rate limits.** Groq's free tier gives each model about 8k tokens per
  minute, and a grounded SQL answer uses about 5k (routing, intent screen,
  SQL, answer: measured). On HTTP 429 the client retries the same request on
  `GROQ_FALLBACK_MODELS` (default `openai/gpt-oss-20b,openai/gpt-oss-120b`),
  each with its own budget. A stream only falls back before its first token.
- **Vanna** uses this same client (`_ChatBridge` in `vanna_provider.py`)
  instead of Vanna's vendor-specific LLM classes. One provider switch covers
  answers, routing and SQL generation.

`build_embedding_client` does the same for `ollama`, `fastembed`,
`cloudflare` and `openai`.

The local launcher discovers installed Ollama models and picks the configured
one if present, otherwise the best installed instruct model
(`PREFERRED_OLLAMA_CHAT_MODELS`). Override with `OLLAMA_CHAT_MODEL`.

### Routing cost (`ROUTER_LLM`)

The router's semantic screen and LLM classification are two extra model calls
per question. On a local CPU model they cost about 50 seconds, and in a
documents-only deployment every route ends in document search anyway. So
`ROUTER_LLM=auto` (default) uses the model only when SQL is enabled or the
model is hosted and fast. The deterministic refusal rules run in every mode.

### Structured data (`DATABASE_BACKEND=duckdb`)

The default in both shipped modes. It is one file, built from the same
PostgreSQL scripts as Neon plus the deterministic synthetic generator
(`database/duckdb_store.py`, `docs/DATABASE_SCHEMA.md`). The model writes
PostgreSQL, the guard validates PostgreSQL, and only the approved statement is
transpiled to DuckDB by sqlglot and run on a **read-only** connection with a
timeout. SQL Server and PostgreSQL/Neon remain supported with
`DATABASE_BACKEND=sqlserver|postgresql`.

### Documents-only mode (`DATABASE_BACKEND=none`)

Text-to-SQL needs SQL Server or PostgreSQL. Without one, data questions still
search the documents (the KPI glossary and policies often answer part of
them) and the model is told explicitly that live figures are unavailable, so
it states that rather than inventing numbers. `/health` reports the database
as "disabled", not failed.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | readiness: copilot, index chunk count, database, chat model, data locality |
| `GET /api/meta` | UI bootstrap: mode (local/cloud), provider, model, personas, example questions |
| `POST /api/ask` | batch answer |
| `POST /api/ask/stream` | SSE: `sources` → `token`* → `done` (or `error`); `done` carries `sql` (generated/executed SQL, guard verdict), `columns`, `rows` and the route - the developer metadata |
| `POST /api/retrieve` | strategy comparison (Retrieval lab) |
| `GET /api/documents` | corpus list, filtered server-side by persona |
| `POST /api/documents/upload` | validated upload + incremental indexing |
| `GET /api/evaluation`, `GET /api/traces` | developer views |

The copilot is serialised by one lock (the tracer is process-global); waits
are bounded (`COPILOT_LOCK_TIMEOUT_SECONDS`, 503 after). Streaming runs
generation in its own thread, which owns the lock for the whole answer.

Errors reaching the browser are stage-level messages ("Document search was
unavailable"); exception text, hosts, paths and provider error bodies stay in
the server log and the trace.

## Retrieval quality (measured, held-out set, n = 96, k = 8)

| Configuration | Dense NDCG | Hybrid NDCG | Hybrid MRR |
|---|---|---|---|
| Local: Ollama nomic-embed-text, no prefixes | 0.874 | 0.907 | 0.880 |
| Local: Ollama nomic-embed-text, with prefixes (shipped) | 0.861 | 0.910 | 0.883 |
| Cloud: fastembed nomic v1.5 Q, with prefixes (shipped) | 0.856 | 0.901 | 0.874 |

Filter correctness (permission and version leaks) is 1.000 for every
strategy. The prefix change is within noise on this set; it is kept because
the model is trained with them and both modes now embed identically. Hybrid
beats dense by ~4-5% NDCG in every configuration, which is why it is the
default path. The committed baseline in `evals/baselines/` describes the
older qwen3-embedding index; the Evaluation page marks it historical.

Re-run: `cd local-enterprise-copilot && .venv/Scripts/python scripts/evaluate_retrieval.py --holdout`
(with the same environment the launcher sets).

## Security properties

- **Prompt injection**: retrieved text is delimited and declared as data; the
  corpus includes a deliberate injection document (`security-test-prompt-injection.md`)
  and the generation tests assert it is not obeyed. No document content is
  ever executed.
- **Permissions**: persona access groups and tenants filter both dense and
  sparse search before ranking; a failed permission lookup denies everything
  (it previously failed open for BM25).
- **Uploads**: extension allow-list, content sniffing (PDF magic, DOCX zip),
  size cap, count cap, UTF-8 check, slugged filenames confined to
  `data/documents/uploads/`, uploader front matter discarded (it could claim a
  restricted access group or a curated `doc_id`), `doc_id` namespace `DOC-UPL-*`.
- **Secrets**: `GROQ_API_KEY` is read from the environment only, lives in the
  host's secret store, never reaches the browser bundle, logs or error bodies.
- **Public demo**: the corpus is synthetic ("Northwind Cloud"); personas are
  selectable on purpose to demonstrate permission filtering. For real data use
  `API_AUTH_MODE=token` behind the Clerk + Cloudflare Worker gateway
  (`cloud/worker`, `cloud/README.md`).

## Known weaknesses

**Important**
- Local generation is CPU-bound here: ~60-100 s for a full answer with a 4B
  model. Streaming shows sources immediately and text as it is produced.
- Uploads on the Render free plan are ephemeral (lost on restart/redeploy) and
  shared by every visitor. Fine for a demo, not a document store.
- The copilot is serialised per process; concurrent visitors queue. Scaling
  means more instances with server-mode Qdrant, and request-scoped tracing.
- Parent expansion can insert a whole long section into the prompt; context is
  not budgeted against the model's window (Codex audit #14).
- BM25 cache staleness under a shared server-mode Qdrant (audit #11) - not
  reachable in the shipped single-process modes, which reload after uploads.

- Groq free tier: about 4-5 grounded answers per minute across the three
  fallback models. The Streamlit app limits each session to 6 questions per
  minute and the whole app to 8.
- DuckDB has no row-level security. Tenant isolation rests on the guard's
  injected predicate (tested) plus a read-only handle; PostgreSQL adds RLS as
  a second layer.

**Optional**
- Conversations are single-turn at the API (the UI keeps history for display);
  follow-up questions are not rewritten with context.
- No reranker in either shipped mode (PyTorch is too large for the free tier;
  the evaluation found reranking not significantly better than hybrid).
