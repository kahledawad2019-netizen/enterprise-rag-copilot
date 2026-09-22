# ADR-001: Runtime platform — Python 3.13, embedded Qdrant, no Docker

- **Status:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Lead engineer
- **Supersedes:** —

## Context

The specification asks for "Python 3.11 or a compatible stable version", Qdrant
as the primary vector database, and Docker where useful. The target machine was
inspected before choosing (`scripts/check_environment.py` reproduces this):

| Property | Value |
|---|---|
| OS | Windows 11 Home, build 26200 |
| CPU | AMD Ryzen AI 9 HX 370, 12 cores / 24 threads |
| RAM | 31.1 GB total (3.7 GB free at inspection time) |
| GPU | NVIDIA RTX 4070 Laptop, **8 GB VRAM**, driver 595.79, CC 8.9 |
| iGPU | AMD Radeon 890M (ignored for inference) |
| Disk | C: 51 GB free, D: 29 GB free |
| Docker | **not installed** |
| Python | 3.14.0 (default) and 3.13.14 (Store) — **no 3.11** |
| Ollama | 0.34.2 |
| SQL Server | 2025 Express Edition 17.0.1000.7, instance `.\SQLEXPRESS` |
| ODBC | Driver 17 and Driver 18 both installed |

Two facts constrain the design: Python 3.11 is not installed, and Docker is not
available.

## Decision

**1. Target Python 3.13, declare `>=3.11,<3.14`.**

Python 3.14 is the machine default but is too new for this stack — `chromadb`
and `onnxruntime` have no 3.14 wheels, which was confirmed by failed resolution
before this decision. Python 3.13.14 is present and resolves the entire
dependency set. This was verified rather than assumed, by dry-run resolving
`qdrant-client`, `sqlglot`, `faker`, `rank-bm25`, the OpenTelemetry packages,
`structlog`, `pytest`, `python-docx`, `pypdf`, and separately `arize-phoenix`
(20.14.0) and `sentence-transformers` (6.1.0, pulling torch 2.14.0). All
resolved on 3.13.

The upper bound `<3.14` is deliberate: it stops a future contributor from
creating the venv with the machine's *default* interpreter and hitting the
wheel gap.

**2. Run Qdrant in embedded (local path) mode by default.**

Docker is absent, and installing Docker Desktop is an intrusive change to a
machine the project does not own. The spec explicitly permits "Qdrant
local/path mode as a lightweight fallback". `qdrant-client` supports
`QdrantClient(path=...)`, which runs the engine in-process against a local
directory with the same Python API as the server.

`QDRANT_MODE=server` remains supported and is a one-line `.env` change, so
moving to a real Qdrant service later requires no code change.

**3. Keep the reranker on CPU and make it optional.**

The 8 GB VRAM budget is consumed by the chat model (~5.5 GB for an 8B model at
Q4). Loading a cross-encoder onto the same GPU risks an OOM mid-request. With
24 CPU threads, reranking ~30 candidates on CPU costs roughly 1–2 s, which is
acceptable next to local LLM generation. The reranker is an optional extra
(`pip install -e ".[rerank]"`) because it pulls PyTorch (~2.5 GB) and disk is
the tightest resource on this machine.

## Consequences

**Positive**
- No Docker dependency; `git clone` → `pip install` → run.
- The embedded/server split is behind one interface, so the migration path is open.
- Dependency feasibility was proven before any code was written.

**Negative**
- Embedded Qdrant is single-process: the Streamlit app and an ingestion job
  cannot hold the same path open simultaneously. Ingestion must complete before
  the app starts, or `QDRANT_MODE=server` must be used. This is documented in
  `docs/troubleshooting.md`.
- Python 3.13 rather than 3.11 means less community mileage on this exact
  combination; the pinned lock file mitigates surprise upgrades.
- CPU reranking adds latency that a GPU reranker would not.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Install Python 3.11 | Adds a third interpreter to the machine for no functional gain; 3.13 resolves everything. |
| Install Docker Desktop | Intrusive, licence-encumbered for some orgs, and unnecessary given embedded mode. |
| ChromaDB instead of Qdrant | The spec names Qdrant. Chroma also lacks Qdrant's payload-filtering model, which the tenant/version filters rely on. |
| Reranker on GPU | Contends with the chat model for the same 8 GB. Revisit on a 16 GB+ GPU. |
