# syntax=docker/dockerfile:1
#
# Root entry point for container platforms that only auto-detect ./Dockerfile.
# Keep this image definition aligned with cloud/api/Dockerfile. The repository
# root must remain the build context because both cloud/api and
# local-enterprise-copilot are copied into the image.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY cloud/api/requirements.lock.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install -r requirements.lock.txt

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/local-enterprise-copilot/src"

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 copilot
WORKDIR /app

COPY --chown=copilot:copilot local-enterprise-copilot/src/ local-enterprise-copilot/src/
COPY --chown=copilot:copilot local-enterprise-copilot/data/documents/ local-enterprise-copilot/data/documents/
COPY --chown=copilot:copilot local-enterprise-copilot/evals/baselines/ local-enterprise-copilot/evals/baselines/
COPY --chown=copilot:copilot local-enterprise-copilot/sql/ local-enterprise-copilot/sql/
COPY --chown=copilot:copilot cloud/api/ cloud/api/

RUN mkdir -p /app/local-enterprise-copilot/data/qdrant \
             /app/local-enterprise-copilot/data/logs \
             /app/local-enterprise-copilot/data/traces \
             /app/local-enterprise-copilot/data/manifests \
 && chown -R copilot:copilot /app/local-enterprise-copilot/data

USER copilot
WORKDIR /app/cloud/api

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"
