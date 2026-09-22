# Review request

You are reviewing work done by another agent (Claude) on branch `cloud-deploy`
between commits `3631618` (baseline) and `0b568a3` (HEAD). **48 files changed,
+12,158 / −33.** Thirty-six files are new; twelve existing files were modified.

Please be adversarial. I have flagged what I am least sure about at the end,
but do not confine yourself to that list — the things I did not think to
doubt are the ones worth finding.

---

## 1. The starting position

Two projects share this repository:

- `local-enterprise-copilot/` — the real one. Document RAG + Text-to-SQL over
  SQL Server, hybrid retrieval, an sqlglot SQL guard, a router that refuses
  destructive intent by rule, tracing, 19-document corpus, 8 T-SQL scripts,
  ~287k synthetic rows, a rigorous held-out evaluation.
- The repository root — an earlier, simpler Vanna + Streamlit prototype.

Three facts about the environment shaped everything:

1. **The repository had zero commits.** Everything was staged or untracked,
   no remote. Any concurrent edit was unrecoverable.
2. **This is not the machine the project was built on.** Both `.venv`s pointed
   at a deleted Python 3.13 under `C:\Users\ambit`; only Python 3.14 was
   present, which `pyproject.toml` excludes (`>=3.11,<3.14`); no SQL Server,
   no ODBC Driver 18, no Ollama. Stale `__pycache__` still held the old path
   `D:\khaled\RAG SYSTEM`.
3. **Nothing could be run**, so the documented "276 tests passing, NDCG@8
   0.946" could not be reproduced before changing anything.

## 2. The goal

The user wanted it deployed on Cloudflare with a professional UI, connected to
Vanna AI Cloud, and — later in the session — to a hosted chat API instead of
Ollama, because they do not want to depend on their own machine.

## 3. Decisions, and the reasoning to attack

### 3.1 Cloudflare cannot host the Python

Workers run V8 isolates (JS/TS/WASM, plus restricted Pyodide Python). The
copilot needs Streamlit, a native ODBC driver, a local inference runtime, an
embedded vector store, optionally PyTorch. Hyperdrive supports Postgres and
MySQL, not SQL Server.

**Chosen shape:** Cloudflare Pages (UI) → Cloudflare Worker (gateway) →
FastAPI container → Azure SQL + Groq + Cloudflare Workers AI + Vanna Cloud.

**Challenge:** is there a serverless path I dismissed too fast? Cloudflare
Containers were considered and treated as one of several container hosts.

### 3.2 Azure SQL, not Postgres

The user initially chose Neon Postgres. I recommended reversing that and they
agreed. Reasoning: the database layer is 2,198 lines of T-SQL with ~246
dialect-specific constructs, plus `DIALECT = "tsql"` in the guard, T-SQL in
the schema retriever's catalog queries, and ten human-verified SQL examples —
all of it currently *verified* (14/14 attack shapes blocked, tenant isolation
proven, 43 data validations). Translating it would put all of that back in
question, and it could not be checked here: no Docker, no local Postgres.
Azure SQL's free offer (100k vCore-seconds + 32 GB/month, for the lifetime of
the subscription) removed the cost argument.

**Challenge:** is "don't touch verified code" the right call, or is it
conservatism that locks the project into a more expensive, less
Cloudflare-native database? Note Hyperdrive is unusable this way.

### 3.3 The provider layer imitates Ollama instead of replacing it

`ollama.Client` was constructed in **seven** places, each with its own error
handling, streaming loop and option dict. I kept Ollama's method signatures
and return shapes and implemented them over HTTP, so each call site changed by
one line.

`cloud/../llm/clients.py`:
- `OpenAICompatChatClient` — Groq/OpenRouter/Together/vLLM/OpenAI
- `OpenAICompatEmbeddingClient`
- `CloudflareEmbeddingClient` — Workers AI

Uses `requests`, not the `openai` SDK: one POST plus an SSE loop.

**Challenge:** this is an adapter that imitates a third-party library's
interface, including its quirks (`keep_alive` accepted and ignored,
`num_ctx` dropped). Is that a reasonable trade for a one-line-per-site
migration with 323 tests riding on those sites, or is it a shape that will
rot? Specifically look at `_translate_options` and `_to_ollama_shape`.

### 3.4 Specific small decisions

- **Groq rejects `temperature=0`** and silently substitutes `1e-8`. The
  project asks for `0.0` wherever it wants determinism, so the client sends
  `1e-8` itself rather than depending on one vendor's undocumented coercion.
- **`format="json"` → `response_format`.** The router depends on it: without
  it a model wraps JSON in prose, the parse fails, and the router falls back
  to its heuristic *while still appearing to consult the model*.
- **A hosted provider with no `LLM_MODEL` is refused at construction**, because
  the profile's name is an Ollama tag and `llama3.1:8b` on Groq is a 404 that
  reads like a network fault.
- **The Cloudflare embedding response parser accepts three shapes** and names
  what it actually received when it recognises none. Cloudflare publishes
  bge-m3's request schema but not a stable response schema.
- **`@cf/baai/bge-m3`** chosen for embeddings: 1024-dim (same as the local
  `qwen3-embedding:0.6b`, so the collection's vector size is unchanged),
  multilingual (the eval's `multilingual` category is where sparse collapses
  to 0.333), free tier, same Cloudflare account.

**Challenge:** the 1024-dim match is convenient but the vectors are still
different. Is there any way the "same dimension" framing could lead someone to
skip the rebuild? I think the docs are loud enough. Check.

### 3.5 Disclosure

`README.md` and `docs/security.md` claimed nothing leaves the machine. That is
true by default and false with any hosted provider, so both now carry a table
of what each setting sends and what it never sends, and `/health` reports the
live posture as a `data_locality` check.

The line drawn: **query results are never sent**; only
`VANNA_ALLOW_LLM_TO_SEE_DATA` can cross it, and it defaults to false.

**Challenge:** is that line drawn in the right place? Sending the schema and
the business glossary to a third party is a real disclosure that I may be
under-weighting relative to sending rows.

## 4. What was built

| | |
|---|---|
| `cloud/worker/` | Worker gateway: CORS allow-list, Access JWT verification against team JWKS, rate limiting, 64 KB body cap, streaming proxy |
| `cloud/ui/` | React 19 + Vite, 5 sections: Copilot, Retrieval, Corpus, Evaluation, System. ~81 KB gzipped |
| `cloud/ui/mock/` | Mock backend on the Worker's port for UI work without the stack |
| `cloud/api/` | FastAPI adapter + Dockerfile (two-stage, non-root, msodbcsql18, one worker) |
| `llm/clients.py` | The provider layer above, + 26 tests |
| `vanna_cloud_provider.py` | Vanna Cloud, hybrid and cloud modes |
| Docs | `hosted_models.md`, `vanna_cloud.md`, `cloud/README.md` runbook |

**The FastAPI adapter is transport only.** It does not route, validate SQL, or
decide what a user may read — all of that already exists and is tested.
Re-implementing any of it would create a second copy of a safety rule.

## 5. Defects found and fixed

In the pre-existing project:

1. **`PyYAML` was never declared** in `pyproject.toml`, though
   `ingestion/parsers.py` imports it at module scope. Present transitively on
   the original machine; on a clean install **the entire test suite failed to
   collect**.
2. **The root `.gitignore`'s `data/` rule matched at every depth**, so it
   swallowed `local-enterprise-copilot/data/documents/` — the 19-document
   corpus the whole system retrieves from. It would not have been committed.
3. **`data/vanna_chroma/` and `data/manifests/*.pkl` were not ignored** —
   ~600 KB of regenerable binaries plus a pickle would have been frozen into
   the first commit.

In my own code, found by running it:

4. **The Worker advertised `GET /api/documents`; the backend did not serve
   it.** Proxied to a bare FastAPI 404.
5. **`/health` and `/meta` each opened their own `QdrantVectorStore`.**
   Embedded Qdrant permits one client per folder and the copilot already holds
   one, so the second raised "already accessed by another instance" and
   reported a healthy 130-chunk index as broken.
6. **Flex children on the new pages shrank instead of overflowing**, silently
   clipping the bottom of every chart card with nothing to scroll.
7. **Trace timing bars rendered invisible** — a `span` inside a `span`; the
   outer was a grid item and blockified, the inner was not, and an inline box
   ignores width/height.
8. **The mock returned 0-based ranks** (`Array.map` passes the index; the
   backend uses `enumerate(..., start=1)`).

## 6. What was verified, and how

Against the **running** backend, not a mock:

| | |
|---|---|
| `/health` | boots; reports degraded with real reasons |
| vector store | **130 chunks** — the index survived the machine move |
| auth | 401 without a bearer token, serves with one |
| `/documents` | admin 19/0 · analyst_na 14/5 · support_na 13/6 · **guest 5/14**, and a guest sees neither the pricing policy nor the injection test document |
| `/retrieve` | sparse returns `DOC-PM-2025-0042` at rank 1 in **14 ms** — the project's headline retrieval claim, on real data |
| degradation | with no embedder, dense errors and sparse still returns results |

Worker under `wrangler dev`: unknown route → its own 404; CORS allowed origin
→ header present; CORS denied origin → **header absent**; body >64 KB → 413;
backend unreachable → clean 503 with no URL leak.

Tests: **349 passed, 17 failed, 27 skipped.** The 17 are unchanged from the
baseline and are all missing local infrastructure (no SQL Server, no Ollama) —
not logic. 26 of the passing tests are new.

## 7. What is NOT verified — the important part

- **The system has never answered a single question end to end.** No LLM has
  ever run in this environment. Routing, SQL generation, answer generation and
  query execution are all unexercised.
- **The Worker → backend proxy hop.** `workerd` has no outbound network here —
  miniflare could not fetch its own `Request.cf` — so every `fetch()` failed
  regardless of URL while `curl` to the same address returned 200.
- **Access JWT verification.** Needs a real Zero Trust application.
- **The Dockerfile has never been built.** No Docker on this machine.
- **The evaluation numbers on the Evaluation page** describe the *old*
  embedder and model. They are currently served from a hardcoded summary of
  `docs/evaluation_report.md`. After the index is rebuilt with bge-m3 they
  will be wrong, and the page will present them as current.

That last one is the thing I would most like a second opinion on. See
`_HELD_OUT_SUMMARY` in `cloud/api/main.py`.

## 8. Specific things I want challenged

1. **`_HELD_OUT_SUMMARY` is a hardcoded copy of published figures.** It will
   silently go stale the moment the embedder changes. Should the endpoint
   refuse to serve figures whose `index_version` does not match the live one?
2. **`llm/clients.py` imitates a third-party interface.** Right call, or will
   it rot?
3. **The Cloudflare response parser guesses among three shapes.** Reasonable
   defensiveness, or should it fail fast on one documented shape?
4. **`/documents` re-parses YAML front matter** rather than using
   `ingestion.parsers`, to avoid chunking and hashing just to render a list.
   Duplicate parsing logic — acceptable?
5. **Every call site still passes Ollama-shaped options** (`num_ctx`,
   `keep_alive`) that a hosted provider drops. Silent no-ops. Should they warn?
6. **`build_chat_client` returns `Any`.** Deliberate — it returns either an
   `ollama.Client` or my adapter, and typing their union would mean declaring
   a protocol matching Ollama's surface. Worth doing anyway?
7. **Secrets.** `LLM_API_KEY`, `EMBEDDING_CLOUDFLARE_API_TOKEN`,
   `VANNA_API_KEY` are `SecretStr`; `describe_providers()` is the only
   sanctioned renderer and a test asserts it cannot emit a key. Is there a
   path that leaks one — a trace, a log line, an error message?
8. **The 17 failing tests fail hard rather than skipping**, unlike the 27 that
   guard properly. Should they be marked so a clean machine shows green?

## 9. Coordination

All work is on `cloud-deploy`. `main` holds the baseline commit and has not
been touched — `git checkout main -- <path>` restores anything. The two
`.gitignore` corrections and the `PyYAML` declaration are the only changes to
pre-existing project files that are not additive; everything else adds.

Please report what you would change, what you think is wrong, and anything in
section 7 you believe I am under-stating.
