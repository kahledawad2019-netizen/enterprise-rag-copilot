# syntax=docker/dockerfile:1
#
# Backend container for the Enterprise Intelligence Copilot.
#
# Built from the repository root, not from this directory, because the image
# needs both cloud/api/ and local-enterprise-copilot/:
#
#   docker build -f cloud/api/Dockerfile -t copilot-api .
#
# Two stages. The builder gets a compiler for any package without a wheel; the
# runtime does not, so a build toolchain is never shipped to production.

# ---------------------------------------------------------------------------
# Stage 1 - dependencies
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential unixodbc-dev curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY cloud/api/requirements.lock.txt .

# Installed into a virtualenv so the whole dependency tree can be copied to
# the runtime stage as one directory.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install -r requirements.lock.txt

# ---------------------------------------------------------------------------
# Stage 2 - runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/local-enterprise-copilot/src"

# ---------------------------------------------------------------------------
# Microsoft ODBC Driver 18, which pyodbc is a thin wrapper over. Without this
# the container starts fine and then fails on the first query with IM002,
# "Data source name not found and no default driver specified" - an error that
# names nothing useful.
#
# The Debian major version is read from the image rather than hardcoded, so a
# base bump from bookworm to trixie does not silently install the wrong repo.
# Microsoft publishes this package for Debian 9 through 13.
#
# libgssapi-krb5-2 is listed as optional in Microsoft's instructions but is
# required on -slim images, which do not ship it.
# ---------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -sSL -O "https://packages.microsoft.com/config/debian/$(grep VERSION_ID /etc/os-release | cut -d '"' -f 2 | cut -d '.' -f 1)/packages-microsoft-prod.deb" \
 && dpkg -i packages-microsoft-prod.deb \
 && rm packages-microsoft-prod.deb \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 libgssapi-krb5-2 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Never run as root. A container that can write its own code is one bug away
# from persisting an attacker's.
RUN useradd --create-home --uid 10001 copilot
WORKDIR /app

# The copilot package, its documents and its SQL. The vector index is NOT
# copied: it is built at deploy time or mounted, because it is large, it is
# regenerable, and baking it in means a stale index ships with every image.
COPY --chown=copilot:copilot local-enterprise-copilot/src/          local-enterprise-copilot/src/
COPY --chown=copilot:copilot local-enterprise-copilot/data/documents/ local-enterprise-copilot/data/documents/
COPY --chown=copilot:copilot local-enterprise-copilot/evals/baselines/ local-enterprise-copilot/evals/baselines/
COPY --chown=copilot:copilot local-enterprise-copilot/sql/          local-enterprise-copilot/sql/
COPY --chown=copilot:copilot cloud/api/                             cloud/api/

# Writable paths the app expects. Created here so the app never has to create
# a directory it may not have permission for.
RUN mkdir -p /app/local-enterprise-copilot/data/qdrant \
             /app/local-enterprise-copilot/data/logs \
             /app/local-enterprise-copilot/data/traces \
             /app/local-enterprise-copilot/data/manifests \
 && chown -R copilot:copilot /app/local-enterprise-copilot/data

USER copilot
WORKDIR /app/cloud/api

EXPOSE 8000

# Deliberately one worker. Embedded Qdrant is single-process: a second worker
# in the same container opens a second client against the same path and one of
# them silently gets an empty index. Scale by running more containers, or set
# QDRANT_MODE=server and point at a shared Qdrant.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"
