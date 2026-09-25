# RAG architecture

How a question becomes a cited answer in this repository, what runs where in
the two deployment modes, and what is known to be weak.

## At a glance

| | Local (private) | Cloud (public demo) |
|---|---|---|
| Entry point | `run_local.py` → `cloud/api/server.py` | `Dockerfile` → `cloud/api/server.py` on Render |
| Frontend | React 19 + Vite (`cloud/ui`), served by the same process | same build |
| Backend | FastAPI (`cloud/api/main.py`) wrapping `enterprise_copilot.Copilot` | same |
| Chat model | Ollama, auto-picked (default `qwen3:4b-instruct-2507-q4_K_M`) | Groq `qwen/qwen3.8-27b` |
| Embeddings | Ollama `nomic-embed-text` (768-d) | fastembed ONNX `nomic-ai/nomic-embed-text-v1.5-Q` (768-d), in-process |
| Vector DB | embedded Qdrant, `local-enterprise-copilot/data/qdrant-local` | embedded Qdrant, built into the image |
| Sparse index | BM25 (`rank-bm25`), cached pickle next to the manifest | same |
| SQL | disabled (`DATABASE_BACKEND=none`) unless configured | disabled |
| Auth | none (localhost, single user) | public demo + per-IP rate limit |
| Leaves the machine | nothing | prompts (question + retrieved passages) go to Groq |

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
  │                            2. semantic intent screen + LLM route
  │                               classification - only when ROUTER_LLM
  │                               decides it is worth it (see below)
  │                            3. heuristics fallback
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
| `POST /api/ask/stream` | SSE: `sources` → `token`* → `done` (or `error`) |
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

**Optional**
- Conversations are single-turn at the API (the UI keeps history for display);
  follow-up questions are not rewritten with context.
- No reranker in either shipped mode (PyTorch is too large for the free tier;
  the evaluation found reranking not significantly better than hybrid).
