# Deployment

Two supported ways to run the copilot, from the same code:

- **Local / private**: Ollama on your machine; nothing leaves it.
- **Cloud / public**: one Docker container on Render's free plan, with Groq for
  chat.

A third, locked-down option (Cloudflare Pages + Worker with Clerk sign-in in
front of a private API container) is documented in `cloud/README.md`.

---

## Local (Ollama)

Requirements: [Ollama](https://ollama.com), Python 3.12 (or `uv`), Node.js 20+
(only for the first UI build).

```powershell
ollama serve                    # skip if the Ollama app is already running
.\run_local.ps1                 # Windows
./run_local.sh                  # macOS / Linux / Git Bash
```

Open **http://127.0.0.1:8000**. The first run creates the virtualenv,
installs dependencies, builds the index (~30 s) and builds the UI; later runs
start in a few seconds.

What the launcher does:

1. Checks Ollama is reachable. If not, it says so and exits.
2. Picks the chat model: `OLLAMA_CHAT_MODEL` if set and installed, otherwise
   the best installed instruct model. Recommended:
   `ollama pull qwen3:4b-instruct-2507-q4_K_M` (2.5 GB, fast on CPU).
3. Checks the embedding model (`nomic-embed-text`); `--pull` fetches it.
4. Builds the index if missing or built with another embedding model;
   otherwise re-indexes only changed documents.
5. Builds the embedded DuckDB analytics database on first run (~18 s) and
   installs Vanna if missing.
6. Serves UI + API.

Data questions ("Which five customers have the highest ARR?") are answered
by Text-to-SQL over that database. Routing uses rules and heuristics
(`ROUTER_LLM=never`) because model routing costs about 50 s per question on
a CPU. Set `DATABASE_BACKEND=sqlserver` or `postgresql` in
`local-enterprise-copilot/.env` to use a real server instead.

Useful switches:

```powershell
$env:OLLAMA_CHAT_MODEL = "qwen3:4b"      # use a specific installed model
.\run_local.ps1 --rebuild-index          # force a clean index
.\run_local.ps1 --port 8080 --no-browser
```

The badge in the sidebar reads **LOCAL — OLLAMA** with the model name.

---

## Cloud on Streamlit Community Cloud (free, no card)

The hosts that run the Docker image (Render, Cloud Run, Oracle, Fly.io) all
ask for a card at signup or deploy time. Streamlit Community Cloud does not,
so it is the default free host. `cloud/streamlit/streamlit_app.py` is a second
web layer over the **same engine**: the same retrieval, permission filters,
prompts, refusal rules, SQL guard and citation validation. It uses Groq for
chat and SQL generation (through Vanna), fastembed in-process for embeddings,
and a read-only DuckDB file for the company database. There is no database
account to create.

1. https://share.streamlit.io → sign in with GitHub → **Create app → Deploy a
   public app from GitHub**.
2. Repository `kahledawad2019-netizen/enterprise-rag-copilot`, branch `main`
   (or the feature branch before it is merged), main file path
   `cloud/streamlit/streamlit_app.py`.
3. **Advanced settings**: Python **3.12**. Secrets:

   ```toml
   GROQ_API_KEY = "gsk_..."
   # optional
   GROQ_MODEL = "qwen/qwen3.8-27b"
   GROQ_FALLBACK_MODELS = "openai/gpt-oss-20b,openai/gpt-oss-120b"
   TEXT_TO_SQL_PROVIDER = "vanna"   # or "native"
   ```

4. Deploy. The first start installs dependencies (a few minutes). It then
   downloads the embedding model, builds the document index and the DuckDB
   database, and trains Vanna's store (about 1.5 minutes, measured locally at
   96 s). Later visits reuse all of it until the app restarts. Answers then
   take about 3-14 s.

Under each answer, **Data & SQL** shows the executed query and its rows, and
**Sources** shows the cited passages.

What differs from the Docker deployment:
- The Streamlit chat UI replaces the React one.
- There are no document upload, retrieval lab or evaluation pages.
- The app sleeps after 12 hours without visitors.

Limits are sized to Groq's free tier: about 8k tokens per minute per model,
about 5k per grounded answer, and 3 models with fallback. That allows 6
questions per minute per visitor session and 8 per minute for the whole app
(`RATE_LIMIT_PER_MINUTE`, `GLOBAL_RATE_LIMIT_PER_MINUTE`). The key lives in
Streamlit's secret store and is only read on the server.

Run it locally the same way:

```bash
export GROQ_API_KEY=gsk_...    # or .streamlit/secrets.toml
local-enterprise-copilot/.venv/Scripts/python -m streamlit run cloud/streamlit/streamlit_app.py
```

---

## Cloud (Render + Groq)

### Why this shape

| Need | Choice | Why |
|---|---|---|
| Run the Python RAG stack (FastAPI, Qdrant client, ONNX) | **Render free web service (Docker)** | Free, no card, builds from GitHub, 512 MB is enough (measured 310 MB RSS). |
| Chat model | **Groq** | Free tier, very fast, OpenAI-compatible. Default model `qwen/qwen3.8-27b`: 0.3 s for a grounded answer in testing. |
| Embeddings | **fastembed (ONNX) in the container** | No GPU, no second API key; same nomic model family as local. |
| Vector store | **Embedded Qdrant, built into the image** | The corpus ships with the code, so the index is built at image build; no hosted vector DB to provision. |
| Frontend | **Served by the same container** | One URL, no CORS, no build-time API address. |

Rejected: **Hugging Face Spaces** (Docker Spaces now require a paid PRO plan);
**Cloudflare Workers** (cannot run this Python stack; Containers need Workers
Paid); **Vercel** (serverless Python size and time limits; the embedding model
alone is 130 MB); **Google Cloud Run** (needs a billing account); **Fly.io /
Railway** (no longer free). The trade-off accepted with Render: the service
sleeps after 15 minutes idle and the next visit waits about a minute.

### Steps

1. Get a free Groq key at https://console.groq.com/keys.
2. Push this repository to GitHub (already done if you are reading this there).
3. Render dashboard → **New → Blueprint** → select the repository. Render reads
   `render.yaml`, asks for `GROQ_API_KEY`, and builds the `Dockerfile`.
4. Wait for the build (about 5-8 minutes: UI build, dependencies, embedding
   model download, index build) and the first health check.
5. Open `https://<service-name>.onrender.com`. The badge reads
   **CLOUD — GROQ**.

Verify:

```bash
curl https://<service>.onrender.com/api/health     # {"status":"ok", ...}
```

### Environment variables

| Variable | Where | Default | Notes |
|---|---|---|---|
| `GROQ_API_KEY` | Render secret | — | **Required.** Never in a file, never in the UI bundle. |
| `GROQ_MODEL` | render.yaml | `qwen/qwen3.8-27b` | Any chat model your Groq key can use (`GET /openai/v1/models`). Prefer one that cites as `[D1]`; see below. |
| `RATE_LIMIT_PER_MINUTE` | render.yaml | `12` | Per client address, on ask/upload/retrieve. |
| `API_AUTH_MODE` | Dockerfile | `public` | `token` requires the gateway's bearer token. |
| `UPLOADS_ENABLED` | Dockerfile | `true` | Uploaded files are public within the deployment and ephemeral on Render free. |
| `MAX_UPLOAD_MB` / `MAX_UPLOADED_DOCUMENTS` | Dockerfile | `5` / `25` | |
| `ROUTER_LLM` | — | `auto` | `always` / `never` to force LLM routing. |
| `DATABASE_BACKEND` | Dockerfile | `none` | `postgresql` + `POSTGRES_DSN` enables Text-to-SQL (see `cloud/NEON-DEPLOYMENT.md`). |

### Running the cloud image anywhere else

```bash
docker build -t northwind-copilot .
docker run -p 8000:8000 -e GROQ_API_KEY=gsk_... northwind-copilot
```

Any host that runs a Dockerfile and injects `PORT` works the same way
(Koyeb, Fly.io, Cloud Run, Azure Container Apps).

### CI

`.github/workflows/ci.yml` builds this exact image on every push and boots it:
health must be `ok`, the UI shell must be served, retrieval over the baked
index must return the refund policy, and an `/api/ask` with a dummy Groq key
must fail without the key or the provider URL in the response.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Launcher: "Ollama is not reachable" | Start `ollama serve` or the Ollama app. Custom host: set `OLLAMA_HOST`. |
| Launcher: "No chat model is installed" | `ollama pull qwen3:4b-instruct-2507-q4_K_M` |
| Local answers take a minute | CPU inference. Use a smaller/instruct model, or a GPU. Sources appear first; text streams. |
| Cloud `/api/health` 503, copilot "failed to start" | `GROQ_API_KEY` missing. Set it in the Render dashboard and redeploy. |
| Cloud answers "language model is unavailable" | Groq rejected the call (bad key, rate limit). Details are in the Render logs, not the browser. |
| First cloud request slow | Free plan wake-up (~1 min after 15 min idle). |
| 429 "Too many requests" | The per-address rate limit. Raise `RATE_LIMIT_PER_MINUTE`. |
