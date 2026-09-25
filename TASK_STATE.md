# TASK_STATE

## Current Goal
Two working modes for the RAG copilot, one codebase:
- **LOCAL**: browser → `cloud/api/server.py` (UI + API, :8000) → hybrid retrieval (embedded Qdrant + BM25) → Ollama (chat `qwen3:4b-instruct-2507-q4_K_M`, embeddings `nomic-embed-text`)
- **CLOUD**: browser → same server in one Docker container on Render free → Groq (`llama-3.3-70b-versatile`) + fastembed ONNX (`nomic-ai/nomic-embed-text-v1.5-Q`), index baked at image build.

## Architecture Summary
- App: `local-enterprise-copilot/src/enterprise_copilot` (Python 3.12 venv at `local-enterprise-copilot/.venv`).
- API: `cloud/api/main.py` (FastAPI, API only; Worker path) + `cloud/api/server.py` (UI at `/`, API at `/api`).
- UI: `cloud/ui` React 19 + Vite. `npm run build:open` = no Clerk (local/demo). `npm run build` = Clerk (staging gateway).
- Providers: `llm/clients.py` (`build_chat_client`: ollama → OllamaChatClient; groq/openai → OpenAICompatChatClient; `build_embedding_client`: ollama | fastembed | cloudflare | openai).
- DATABASE_BACKEND=none → documents-only; ROUTER_LLM=auto → LLM routing only if SQL enabled or hosted model.

## Completed
- venv rebuilt (uv, Python 3.12). Tests: **442 passed, 1 skipped** (`pytest -m "not integration"`), ruff + mypy clean (src: 52 files; cloud/api 3 files).
- Local E2E via API + streaming works (answered, grounded, cites D1). Routing no longer uses LLM locally (was 50 s).
- Codex audit (15 findings) → fixed: #1 fail-open sparse filter, #2 error leakage, #3/#4 docs-only mode, #5/#6 reasoning models, #7 router schema, #9 nomic prefixes, #12 dedupe, #13 MMR, #15 SSE errors.
- Retrieval holdout NDCG (hybrid): local Ollama nomic 0.910, cloud fastembed nomic 0.901.
- Cloud rehearsal locally: health ok, retrieval 29 ms, RSS 310 MB (< Render 512 MB).
- UI rewritten (chat product, conversations in localStorage, streaming, cited-source chips, knowledge base upload, dev tools). Screenshots OK desktop + narrow.
- Root Dockerfile, .dockerignore, render.yaml, CI smoke test, run_local.py/.ps1/.sh.

## Current Task
Codex checkpoint review (full diff) running. Writing docs: docs/RAG_ARCHITECTURE.md, docs/DEPLOYMENT.md, .env.example.

## Next Tasks
1. Apply material Codex review findings.
2. Commit on branch `feat/local-ollama-cloud-groq` (stage by name!), push, open PR, watch CI (Cloud image smoke test).
3. Ask user: GROQ_API_KEY + Render account (Blueprint from render.yaml). Test Groq locally with key.
4. Deploy on Render; E2E test deployed URL; final Codex review; final report.

## Important Files
- `run_local.py`, `Dockerfile`, `render.yaml`, `cloud/api/{main,server,uploads}.py`, `cloud/ui/src/{App.tsx,api.ts,app.css,components/ChatMessage.tsx,components/KnowledgeBase.tsx}`
- `local-enterprise-copilot/src/enterprise_copilot/{llm/clients.py,config/settings.py,routing/orchestrator.py,generation/answerer.py,retrieval/embedder.py}`

## Commands That Work
- Local app: `.\run_local.ps1` or `local-enterprise-copilot/.venv/Scripts/python run_local.py` → http://127.0.0.1:8000
- Tests: `cd local-enterprise-copilot; .venv/Scripts/python -m pytest -m "not integration" -q -p no:cacheprovider --ignore="tests/pytest-of-LAPTOP WORLD" --basetemp="$TEMP/pt-rag"`
- API lint/types: `ruff check cloud/api run_local.py`; `mypy cloud/api/main.py cloud/api/server.py cloud/api/uploads.py --python-version 3.12 --ignore-missing-imports`
- UI: `cd cloud/ui; npm.cmd run build:open`
- Lock: `uv pip compile cloud/api/requirements.txt --python-version 3.13 --universal --output-file cloud/api/requirements.lock.txt`
- Screenshot: msedge --headless=new --screenshot (min viewport 492 px in headless)

## Commands That Failed
- qwen3:4b (Thinking-2507) with think=False → reasoning leaks into content. Solution: never send `think`.
- Bash heredoc with complex Python quoting → use Write/Edit tools.
- run_local.py respecting .env QDRANT_PATH → opened old index; now QDRANT_* forced for local.

## Decisions Made
- Keep existing provider abstraction; Groq = OpenAI-compatible provider with GROQ_* aliases.
- Hosting: Render free Docker (HF Docker Spaces now need PRO; Cloud Run needs billing; Fly paid).
- Single container serving UI + API (same origin), index baked at build time, no hosted vector DB.
- Public demo auth: API_AUTH_MODE=public + per-IP rate limit (12/min) — corpus is synthetic. Clerk/Worker gateway retained for locked-down deployments.
- Uploaded docs: public within deployment, ephemeral on Render free.

## Environment Variables
Cloud: GROQ_API_KEY (secret), GROQ_MODEL, RATE_LIMIT_PER_MINUTE, MAX_UPLOAD_MB, MAX_UPLOADED_DOCUMENTS, UPLOADS_ENABLED, API_AUTH_MODE, ROUTER_LLM. Rest defaulted in Dockerfile.
Local: OLLAMA_HOST, OLLAMA_CHAT_MODEL, OLLAMA_EMBEDDING_MODEL (launcher defaults).

## Deployment State
Not deployed yet. Needs user: Groq key + Render account.

## Known Bugs
- Local CPU generation ~60–100 s per answer (hardware); streaming mitigates.

## Codex Delegations
- ✅ Checkpoint 1 audit (task-muh6guzx-2dxa6p). ✅ 4 retrieval/router fixes (task-muh6s4dd-44lth7). ✅ API tests (task-muh7u7v7-zzt8ak, 19 tests).
- ⏳ Checkpoint 2–4 full-diff review (running).

## Do Not Forget
- Never commit: REVIEW-REQUEST.md, SESSION-HANDOFF.md, CLAUDE-CLOUD-DEPLOYMENT-PROMPT.md, CODEX-CONTINUE-PROMPT.md, .git-temp/, .tmp-npm-cache/, .wrangler-temp/, local-enterprise-copilot/tests/pytest-of-LAPTOP WORLD/.
- Stage by file name; `git diff --cached` secret check. main is protected → PR.
- git push may hang (Git Credential Manager GUI) — handoff note.
- Local server running in background on :8000 (task bmrmiflyx) — stop before tests that touch data/qdrant-local.
