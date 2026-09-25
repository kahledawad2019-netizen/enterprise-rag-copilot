# TASK_STATE

## Current Goal
Enterprise **hybrid** RAG: unstructured documents (vector RAG) + structured data (Text-to-SQL via Vanna) with intent routing, in two modes from one codebase:
- **LOCAL**: browser → `cloud/api/server.py` (UI + API, :8000) → router (rules + heuristics) → hybrid retrieval (embedded Qdrant + BM25) and/or Vanna → read-only DuckDB → Ollama (`qwen3:4b-instruct-2507-q4_K_M`, embeddings `nomic-embed-text`). One command: `.\run_local.ps1`.
- **CLOUD**: Streamlit Community Cloud (free, no card) → same engine → Groq (`qwen/qwen3.8-27b`, 429 fallback to gpt-oss-20b/120b) + fastembed + DuckDB built on first start + Vanna on Groq. Docker image (React UI) kept for any Docker host.

## Architecture Summary
- Engine: `local-enterprise-copilot/src/enterprise_copilot` (Python 3.12 venv `local-enterprise-copilot/.venv`).
- Routes: document_rag | text_to_sql | multi_source (HYBRID, parallel) | clarify | refuse | **direct** (template, no retrieval).
- SQL path: schema_retriever → Vanna (`_ChatBridge` → `build_chat_client`) or native → SQLGuard (postgres dialect, tenant injection, row limit) → ReadOnlyRunner (DuckDB: sqlglot transpile + read-only handle + interrupt timeout) → self-correction on DB error (max 2, re-guarded).
- DATABASE_BACKEND: duckdb (default local/cloud) | postgresql (Neon) | sqlserver | none. DuckDB built from `sql/postgres` + synthetic generator (`database/duckdb_store.py`, `scripts/build_duckdb.py`).
- Docs: `docs/RAG_ARCHITECTURE.md`, `docs/DATABASE_SCHEMA.md`, `docs/DEPLOYMENT.md`.

## Completed
- Phase 1 (earlier): local Ollama + cloud Groq modes, production UI, security review, PR #6 (CI green), Streamlit deployment (c3968a5).
- Phase 2 (this goal):
  - DuckDB backend: full dataset 18 s / 24 MB (600 customers, 237k usage rows, 8.7k invoices, 4k tickets); 9/10 golden SQL run unchanged (10th is a parameter template); all 6 views; engine refuses writes.
  - Vanna works with Groq and Ollama (LLM bridge), per-backend Chroma store.
  - DIRECT route; parallel HYBRID gather; thread-local tracer; SQL self-correction loop (SQL_EXECUTION_RETRIES=2).
  - Groq 429 → fallback models (measured: 8k TPM per model, ~5k tokens per grounded answer).
  - Streamlit: SQL on, Data & SQL panel, limits 6/session/min, 8/app/min. AppTest from fresh start: 96 s first start; ARR NA→tenant 1, EU→tenant 2 (isolation verified); hybrid cites D1-D5 + S1.
  - run_local: duckdb default, ROUTER_LLM=never (heuristics 12/14 correct), auto-installs duckdb/vanna in old venvs.
  - Dockerfile builds DuckDB at image build (native generator in the image).
  - Tests: 27 new (tests/test_duckdb_backend.py) pass; full suite 452 passed, 14 failures are all environment (old .env Ollama models not pulled, SQL Server login) - not regressions.

## Current Task
Codex review of phase 2 + Codex-written Vanna bridge tests running; Docker image build running. Then fix findings, commit, push, CI.

## Next Tasks
1. Apply verified Codex findings; re-run tests.
2. Commit (stage by name), push, watch CI (Cloud image smoke test now builds DuckDB).
3. User redeploys Streamlit app (same branch/file path) - first start ~2-3 min on Streamlit.
4. Final report.

## Important Files
- New: `database/duckdb_store.py`, `scripts/build_duckdb.py`, `tests/test_duckdb_backend.py`, `docs/DATABASE_SCHEMA.md`
- Changed: `config/settings.py` (DuckDBSettings, execution_dialect, sql_execution_retries, fallback models), `llm/clients.py` (RateLimitedError, fallback), `routing/{router,orchestrator}.py`, `observability/tracing.py`, `database/{connection,read_only_runner,synthetic_loader}.py`, `text_to_sql/{schema_retriever,vanna_provider}.py`, `cloud/streamlit/*`, `run_local.py(+.ps1/.sh)`, `Dockerfile`, `.dockerignore`, `cloud/api/requirements*.txt`, `.env.example`.

## Commands That Work
- Local app: `.\run_local.ps1` → http://127.0.0.1:8000
- Build DB: `local-enterprise-copilot/.venv/Scripts/python local-enterprise-copilot/scripts/build_duckdb.py --full --rebuild`
- Streamlit locally: `GROQ_API_KEY=... local-enterprise-copilot/.venv/Scripts/python -m streamlit run cloud/streamlit/streamlit_app.py`
- Tests: `cd local-enterprise-copilot; .venv/Scripts/python -m pytest tests -q -p no:cacheprovider --ignore="tests/pytest-of-LAPTOP WORLD" --basetemp="$TEMP/pt-rag"`
- Lock: `uv pip compile cloud/api/requirements.txt --python-version 3.13 --universal --output-file cloud/api/requirements.lock.txt`

## Commands That Failed
- DuckDB: cross-schema FKs unsupported → dropped + recorded in ai.schema_relationships.
- Regex `^\s*(REVOKE|GRANT)` with IGNORECASE matched prose in comments → case-sensitive, `[ \t]*`.
- Bash heredoc Python edits turn `\n` escapes into real newlines → use Edit/Write for strings with escapes.

## Decisions Made
- Structured store: DuckDB (embedded, Postgres-like, real schemas, read-only mode) instead of SQLite (no schemas) or Neon (account + credentials). Neon/SQL Server still supported.
- Model writes PostgreSQL; transpile only after the guard approves.
- Local routing without LLM (CPU cost); cloud routing with LLM + intent screen.
- Vanna in Streamlit (per goal); native generator in the Docker image (image size).
- Hosting: Streamlit Community Cloud (Render/Oracle/GCP need a card; HF Docker/Gradio need PRO).

## Environment Variables
Cloud (Streamlit secrets): GROQ_API_KEY (required), GROQ_MODEL, GROQ_FALLBACK_MODELS, TEXT_TO_SQL_PROVIDER. App forces DATABASE_BACKEND=duckdb, LLM_PROVIDER=groq, EMBEDDING_PROVIDER=fastembed.
Local: OLLAMA_*, DATABASE_BACKEND, DUCKDB_PATH, ROUTER_LLM, SQL_EXECUTION_RETRIES (launcher defaults).

## Deployment State
Streamlit app: user deploying from branch feat/local-ollama-cloud-groq, main file cloud/streamlit/streamlit_app.py (documents-only version pushed in c3968a5; SQL version pending push).

## Known Bugs / Limits
- Local CPU generation ~40–100 s per answer.
- Groq free tier ≈ 4-5 grounded answers/min across 3 models.
- DuckDB has no RLS: tenant isolation = guard-injected predicate (tested).

## Codex Delegations
- ✅ Hosting research (partial; usage limit hit).
- ⏳ Phase-2 review (task-muhhmxg2-0rphl8). ⏳ Vanna bridge tests (new file tests/test_vanna_bridge.py only).
- ✅ Earlier: checkpoint audits and fixes (see git history).

## Do Not Forget
- Never commit: REVIEW-REQUEST.md, SESSION-HANDOFF.md, CLAUDE-CLOUD-DEPLOYMENT-PROMPT.md, CODEX-CONTINUE-PROMPT.md, SESSION-*.md, .git-temp/, .tmp-npm-cache/, .wrangler-temp/, local-enterprise-copilot/tests/pytest-of-LAPTOP WORLD/.
- Stage by file name; `git diff --cached` secret check. main is protected → PR.
