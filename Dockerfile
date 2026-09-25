# syntax=docker/dockerfile:1
#
# The public cloud demo: UI + API in one container, Groq for chat, in-process
# ONNX embeddings, and the document index baked into the image.
#
#   docker build -t northwind-copilot .
#   docker run -p 8000:8000 -e GROQ_API_KEY=gsk_... northwind-copilot
#
# Why one container: the free hosts that can run this Python stack (Render,
# for one) give one service, and serving the UI from the API process means one
# URL, no CORS, and no build-time API address. The Cloudflare Worker + Clerk
# gateway path (cloud/api/Dockerfile) remains for a locked-down deployment.
#
# Why the index is built here: the corpus ships with the image, so indexing at
# build time makes a cold start instant and removes any need for a hosted
# vector database. The only secret the running container needs is
# GROQ_API_KEY, and nothing in the image contains it.

# ---------------------------------------------------------------------------
# Stage 1 - the UI
# ---------------------------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY cloud/ui/package.json cloud/ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY cloud/ui/ ./
# build:open = no sign-in provider (see cloud/ui/.env.open).
RUN npm run build:open

# ---------------------------------------------------------------------------
# Stage 2 - Python dependencies
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY cloud/api/requirements.lock.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install -r requirements.lock.txt

# ---------------------------------------------------------------------------
# Stage 3 - runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/local-enterprise-copilot/src" \
    HOME=/home/copilot \
    # --- cloud defaults; every one can be overridden by the host ---
    LLM_PROVIDER=groq \
    GROQ_MODEL=llama-3.3-70b-versatile \
    EMBEDDING_PROVIDER=fastembed \
    EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5-Q \
    EMBEDDING_CACHE_DIR=/app/models \
    COPILOT_PROFILE=lite \
    DATABASE_BACKEND=none \
    QDRANT_MODE=embedded \
    QDRANT_PATH=/app/local-enterprise-copilot/data/qdrant \
    QDRANT_INDEX_VERSION=cloud-nomic-v1 \
    API_AUTH_MODE=public \
    COPILOT_DEMO_MODE=true \
    RATE_LIMIT_PER_MINUTE=12 \
    # Render's proxy appends the client address to X-Forwarded-For.
    TRUSTED_PROXY_HOPS=1 \
    MAX_UPLOAD_MB=5 \
    MAX_UPLOADED_DOCUMENTS=25 \
    UI_DIST_DIR=/app/cloud/ui/dist \
    OBS_LOG_FORMAT=console

COPY --from=builder /opt/venv /opt/venv
RUN useradd --create-home --uid 10001 copilot
WORKDIR /app

COPY --chown=copilot:copilot local-enterprise-copilot/src/ local-enterprise-copilot/src/
COPY --chown=copilot:copilot local-enterprise-copilot/scripts/build_index.py local-enterprise-copilot/scripts/build_index.py
COPY --chown=copilot:copilot local-enterprise-copilot/data/documents/ local-enterprise-copilot/data/documents/
COPY --chown=copilot:copilot local-enterprise-copilot/evals/baselines/ local-enterprise-copilot/evals/baselines/
COPY --chown=copilot:copilot cloud/api/ cloud/api/
COPY --from=ui --chown=copilot:copilot /ui/dist/ cloud/ui/dist/

RUN mkdir -p /app/models \
             /app/local-enterprise-copilot/data/qdrant \
             /app/local-enterprise-copilot/data/logs \
             /app/local-enterprise-copilot/data/traces \
             /app/local-enterprise-copilot/data/manifests \
             /app/local-enterprise-copilot/data/generated \
 && chown -R copilot:copilot /app/models /app/local-enterprise-copilot/data

USER copilot

# Download the embedding model and build the index once, at build time.
RUN python local-enterprise-copilot/scripts/build_index.py --rebuild \
 && python local-enterprise-copilot/scripts/build_index.py --validate

WORKDIR /app/cloud/api
EXPOSE 8000

# One worker: embedded Qdrant allows a single client per folder. Hosts such as
# Render inject PORT; 8000 otherwise.
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=8).status == 200 else 1)"
