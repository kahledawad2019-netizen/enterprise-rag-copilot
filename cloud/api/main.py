"""
HTTP surface for the Enterprise Intelligence Copilot.

This module is a **transport adapter and nothing else**. It does not route
questions, validate SQL, decide what a user may read, or generate text. All of
that already exists in `enterprise_copilot` and is covered by that package's
tests; re-implementing any of it here would create a second copy of a safety
rule, and two copies drift.

What it does own:

* authenticating the caller (a bearer token the Cloudflare Worker presents),
* turning a persona name into a `UserContext`,
* narrowing the internal `Answer` / `CopilotTrace` objects into a response the
  browser is allowed to see.

That last point is deliberate. `Evidence` carries `access_group` and `tenant`
for auditing, and chunk text can be long. Neither belongs in a payload sent to
a browser, so the response model lists fields explicitly rather than dumping
the internal object.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# The copilot package lives alongside this file in the image. Adding it here
# rather than relying on the working directory means `uvicorn main:app` behaves
# the same from any cwd - the same class of bug the project already hit once
# with relative Qdrant paths.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent / "local-enterprise-copilot"
if (PROJECT_ROOT / "src").exists():
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from enterprise_copilot.config import get_settings  # noqa: E402
from enterprise_copilot.models.evidence import Answer, Evidence  # noqa: E402
from enterprise_copilot.retrieval.hybrid import UserContext  # noqa: E402
from enterprise_copilot.routing.orchestrator import Copilot, CopilotTrace  # noqa: E402

log = logging.getLogger("copilot.api")

SNIPPET_CHARS = 420

# ---------------------------------------------------------------------------
# Personas
#
# Mirrors app/_shared.py so the hosted UI exercises the same permission model
# the Streamlit app does. Each is a real tenant and a real access-group set:
# switching persona changes what retrieval returns, not what is displayed.
# ---------------------------------------------------------------------------

PERSONAS: dict[str, dict[str, Any]] = {
    "admin": {
        "label": "Administrator",
        "tenant": "all",
        "tenant_id": 1,
        "access_groups": ["public", "internal", "finance", "support", "security", "exec"],
        "description": "Sees everything. Useful as a baseline to compare the others against.",
        "is_admin": True,
    },
    "analyst_na": {
        "label": "Analyst — North America",
        "tenant": "NWC-NA",
        "tenant_id": 1,
        "access_groups": ["public", "internal", "finance"],
        "description": "Finance access, tenant 1 only. Cannot see support or security material.",
        "is_admin": False,
    },
    "analyst_eu": {
        "label": "Analyst — Europe",
        "tenant": "NWC-EU",
        "tenant_id": 2,
        "access_groups": ["public", "internal", "finance"],
        "description": "The same role in a different tenant. Ask the same question to see isolation.",
        "is_admin": False,
    },
    "support_na": {
        "label": "Support — North America",
        "tenant": "NWC-NA",
        "tenant_id": 1,
        "access_groups": ["public", "internal", "support"],
        "description": "Support material instead of finance. Pricing policy is out of reach.",
        "is_admin": False,
    },
    "guest": {
        "label": "Guest",
        "tenant": "all",
        "tenant_id": 1,
        "access_groups": ["public"],
        "description": "Public documents only. Most internal questions should be declined.",
        "is_admin": False,
    },
}

EXAMPLE_QUESTIONS = [
    "What is our refund policy for annual plans?",
    "Which five customers have the highest ARR?",
    "Show customers with more than three SLA breaches and summarise the SLA policy.",
    "What happened in INC-2025-0042?",
    "What is the maximum discount a sales rep can approve?",
    "Delete all customers",
]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SourceRef(BaseModel):
    id: str
    type: str
    title: str = ""
    version: str = ""
    section: str = ""
    effective_date: str = ""
    authority: str = ""
    score: float | None = None
    rerank_score: float | None = None
    retrieval_method: str = ""
    snippet: str = ""


class SqlDetail(BaseModel):
    generated: str | None = None
    executed: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    tenant_injected: bool = False
    tables: list[str] = Field(default_factory=list)
    join_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    provider: str = ""


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    persona: str = "admin"
    strategy: Literal["dense", "sparse", "hybrid", "reranked"] = "reranked"


class AskResponse(BaseModel):
    trace_id: str
    question: str
    route: str
    route_decided_by: str = ""
    route_confidence: float = 0.0
    status: str
    answer: str
    grounded: bool
    sources: list[SourceRef] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sql: SqlDetail | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    timings_ms: dict[str, float] = Field(default_factory=dict)
    versions: dict[str, str] = Field(default_factory=dict)


class PersonaOut(BaseModel):
    key: str
    label: str
    tenant: str
    tenant_id: int
    access_groups: list[str]
    description: str


class MetaResponse(BaseModel):
    app_name: str
    personas: list[PersonaOut]
    strategies: list[str]
    default_strategy: str
    sql_provider: str
    chat_model: str
    index_version: str
    document_count: int
    chunk_count: int
    example_questions: list[str]


class HealthCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    checks: list[HealthCheck]


# ---------------------------------------------------------------------------
# Lifespan: one Copilot per process
#
# Building it loads the embedder, opens the vector store and connects to the
# database. Embedded Qdrant is single-process, so this also means exactly one
# worker per container - see the Dockerfile, which does not pass --workers.
# ---------------------------------------------------------------------------

_copilot: Copilot | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _copilot
    settings = get_settings()

    from enterprise_copilot.observability.tracing import configure_logging

    configure_logging(settings)

    try:
        _copilot = Copilot(settings)
        log.info("Copilot ready")
    except Exception as exc:
        # Starting anyway is deliberate: /health must be able to report *why*
        # the backend is unusable. A container that exits on boot tells the
        # operator nothing except that it exited.
        log.error("Copilot failed to start: %s", exc)
        _copilot = None

    yield

    if _copilot is not None:
        _copilot.close()


app = FastAPI(
    title="Enterprise Intelligence Copilot API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,       # The Worker is the only client; nothing browses this.
    redoc_url=None,
    openapi_url=None,
)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def require_token(authorization: str = Header(default="")) -> None:
    """Reject anything that does not carry the Worker's shared secret.

    The container has a public URL, so this is the only thing standing between
    the copilot and the internet. Compared with `compare_digest` so a wrong
    token cannot be recovered one byte at a time from response timing.
    """
    expected = os.environ.get("BACKEND_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="BACKEND_TOKEN is not set on the backend; refusing to serve unauthenticated.",
        )

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")


def get_copilot() -> Copilot:
    if _copilot is None:
        raise HTTPException(
            status_code=503,
            detail="The copilot did not start. Check the backend logs and /health.",
        )
    return _copilot


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Probe every dependency. Deliberately unauthenticated so the platform's
    own health check can reach it; it reveals status, never configuration."""
    checks: list[HealthCheck] = []
    settings = get_settings()

    checks.append(
        HealthCheck(
            name="copilot",
            ok=_copilot is not None,
            detail="ready" if _copilot is not None else "failed to start",
        )
    )

    # Vector store
    try:
        from enterprise_copilot.retrieval.vector_store import QdrantVectorStore

        count = QdrantVectorStore(settings).count()
        checks.append(
            HealthCheck(name="vector_store", ok=count > 0, detail=f"{count} chunks indexed")
        )
    except Exception as exc:
        checks.append(HealthCheck(name="vector_store", ok=False, detail=_short(exc)))

    # Database
    try:
        from enterprise_copilot.database.connection import raw_connection

        with raw_connection(settings) as conn:
            conn.cursor().execute("SELECT 1")
        checks.append(HealthCheck(name="database", ok=True, detail="reachable"))
    except Exception as exc:
        checks.append(HealthCheck(name="database", ok=False, detail=_short(exc)))

    # Chat model
    try:
        import ollama

        client = ollama.Client(host=settings.ollama.host)
        models = [m.get("model", "") for m in client.list().get("models", [])]
        checks.append(
            HealthCheck(name="chat_model", ok=bool(models), detail=f"{len(models)} models available")
        )
    except Exception as exc:
        checks.append(HealthCheck(name="chat_model", ok=False, detail=_short(exc)))

    if all(c.ok for c in checks):
        status: Literal["ok", "degraded", "down"] = "ok"
    elif _copilot is None:
        status = "down"
    else:
        status = "degraded"

    return HealthResponse(status=status, checks=checks)


@app.get("/meta", response_model=MetaResponse, dependencies=[Depends(require_token)])
def meta() -> MetaResponse:
    settings = get_settings()

    document_count = 0
    chunk_count = 0
    try:
        from enterprise_copilot.retrieval.vector_store import QdrantVectorStore

        chunk_count = QdrantVectorStore(settings).count()
        document_count = len(list(settings.documents_dir.glob("*.md")))
    except Exception as exc:  # a missing index must not break the whole UI
        log.warning("Could not read corpus counts: %s", exc)

    return MetaResponse(
        app_name=settings.app_name,
        personas=[
            PersonaOut(
                key=key,
                label=p["label"],
                tenant=p["tenant"],
                tenant_id=p["tenant_id"],
                access_groups=p["access_groups"],
                description=p["description"],
            )
            for key, p in PERSONAS.items()
        ],
        strategies=["dense", "sparse", "hybrid", "reranked"],
        default_strategy="reranked",
        sql_provider=settings.text_to_sql_provider,
        chat_model=settings.profile.chat_model,
        index_version=settings.vector_store.index_version,
        document_count=document_count,
        chunk_count=chunk_count,
        example_questions=EXAMPLE_QUESTIONS,
    )


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(require_token)])
def ask(
    body: AskRequest,
    request: Request,
    copilot: Copilot = Depends(get_copilot),
) -> AskResponse:
    persona = PERSONAS.get(body.persona)
    if persona is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona {body.persona!r}.")

    user = UserContext(
        user_name=persona["label"],
        tenant=persona["tenant"],
        access_groups=list(persona["access_groups"]),
        is_admin=persona["is_admin"],
    )

    # The identity Cloudflare Access verified, if there was one. Recorded on
    # the trace so an audit can tie a query to a person, not just a persona.
    caller = request.headers.get("X-Copilot-User", "")
    if caller:
        log.info("request from %s acting as %s", caller, body.persona)

    started = time.perf_counter()
    try:
        answer, trace = copilot.ask(
            body.question,
            user=user,
            tenant_id=persona["tenant_id"],
            strategy=body.strategy,
        )
    except Exception as exc:
        log.exception("ask failed")
        raise HTTPException(status_code=500, detail=_short(exc)) from exc

    elapsed = (time.perf_counter() - started) * 1000
    return _to_response(answer, trace, elapsed)


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    persona: str = "admin"
    limit: int = Field(default=8, ge=1, le=20)
    strategies: list[str] = Field(default_factory=lambda: ["dense", "sparse", "hybrid", "reranked"])


class ScoredChunkOut(BaseModel):
    rank: int
    doc_id: str
    title: str = ""
    version: str = ""
    section: str = ""
    score: float
    dense_score: float | None = None
    dense_rank: int | None = None
    sparse_score: float | None = None
    sparse_rank: int | None = None
    rerank_score: float | None = None
    snippet: str = ""


class StrategyRun(BaseModel):
    strategy: str
    elapsed_ms: float
    results: list[ScoredChunkOut]
    error: str | None = None


class RetrieveResponse(BaseModel):
    query: str
    runs: list[StrategyRun]


@app.post("/retrieve", response_model=RetrieveResponse, dependencies=[Depends(require_token)])
def retrieve(body: RetrieveRequest, copilot: Copilot = Depends(get_copilot)) -> RetrieveResponse:
    """Run the same query through several strategies so they can be compared.

    This is the project's most informative screen: it is where "hybrid beats
    dense" stops being a claim and becomes something you can watch happen.
    Try `INC-2025-0042` - dense returns the wrong incident, sparse gets it
    right at rank 1, and hybrid keeps the sparse answer.
    """
    persona = PERSONAS.get(body.persona)
    if persona is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona {body.persona!r}.")

    user = UserContext(
        user_name=persona["label"],
        tenant=persona["tenant"],
        access_groups=list(persona["access_groups"]),
        is_admin=persona["is_admin"],
    )

    runs: list[StrategyRun] = []
    for strategy in body.strategies:
        started = time.perf_counter()
        try:
            results, _ = copilot.retriever.retrieve(
                body.query, strategy=strategy, user=user, limit=body.limit
            )
        except Exception as exc:
            # One failing strategy must not lose the others: a comparison with
            # three of four columns is still a useful comparison.
            log.warning("retrieval failed for %s: %s", strategy, exc)
            runs.append(
                StrategyRun(
                    strategy=strategy,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    results=[],
                    error=_short(exc),
                )
            )
            continue

        runs.append(
            StrategyRun(
                strategy=strategy,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                results=[
                    ScoredChunkOut(
                        rank=rank,
                        doc_id=item.chunk.doc_id,
                        title=item.chunk.title,
                        version=item.chunk.version,
                        section=item.chunk.section_path,
                        score=round(item.score, 4),
                        dense_score=_round(item.dense_score),
                        dense_rank=item.dense_rank,
                        sparse_score=_round(item.sparse_score),
                        sparse_rank=item.sparse_rank,
                        rerank_score=_round(item.rerank_score),
                        snippet=item.chunk.text[:SNIPPET_CHARS],
                    )
                    for rank, item in enumerate(results, start=1)
                ],
            )
        )

    return RetrieveResponse(query=body.query, runs=runs)


@app.get("/evaluation", dependencies=[Depends(require_token)])
def evaluation() -> dict[str, Any]:
    """The measured numbers, read from the committed evaluation runs.

    Served from disk rather than recomputed: a full evaluation takes minutes
    of model time, and a dashboard that silently re-runs it on every page load
    is a dashboard nobody opens twice.
    """
    import json

    results_dir = PROJECT_ROOT / "evals" / "results"
    runs: list[dict[str, Any]] = []
    if results_dir.exists():
        for path in sorted(results_dir.glob("*.json"), reverse=True)[:20]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("could not read %s: %s", path.name, exc)
                continue
            runs.append({"file": path.name, "kind": path.name.split("_")[0], "payload": payload})

    return {"runs": runs, "report": _HELD_OUT_SUMMARY}


# The headline figures from docs/evaluation_report.md. Held here so the
# dashboard can render them without parsing prose, and kept in one place so
# there is a single thing to update when the evaluation is re-run.
_HELD_OUT_SUMMARY: dict[str, Any] = {
    "cases": 96,
    "k": 8,
    "bootstrap_resamples": 10000,
    "strategies": [
        {"name": "dense", "ndcg": 0.856, "ci": [0.809, 0.900], "mrr": 0.811, "recall": 0.990, "median_ms": 49},
        {"name": "sparse", "ndcg": 0.877, "ci": [0.833, 0.918], "mrr": 0.839, "recall": 0.990, "median_ms": 4},
        {"name": "hybrid", "ndcg": 0.925, "ci": [0.882, 0.961], "mrr": 0.906, "recall": 0.979, "median_ms": 54},
        {"name": "reranked", "ndcg": 0.946, "ci": [0.914, 0.974], "mrr": 0.928, "recall": 1.000, "median_ms": 1454},
    ],
    "comparisons": [
        {"pair": "hybrid vs dense", "delta": 0.069, "ci": [0.029, 0.110], "p": 0.0002, "significant": True},
        {"pair": "reranked vs dense", "delta": 0.091, "ci": [0.055, 0.131], "p": 0.00005, "significant": True},
        {"pair": "hybrid vs sparse", "delta": 0.047, "ci": [0.010, 0.085], "p": 0.014, "significant": True},
        {"pair": "reranked vs sparse", "delta": 0.070, "ci": [0.033, 0.109], "p": 0.0002, "significant": True},
        {"pair": "sparse vs dense", "delta": 0.021, "ci": [-0.033, 0.078], "p": 0.448, "significant": False},
        {"pair": "reranked vs hybrid", "delta": 0.023, "ci": [-0.013, 0.059], "p": 0.217, "significant": False},
    ],
    "headline": (
        "Hybrid retrieval is a real improvement over dense: +0.069 NDCG, the interval "
        "excludes zero, and it survives Holm-Bonferroni correction. Reranking is NOT "
        "statistically distinguishable from hybrid - the interval [-0.013, +0.059] "
        "contains zero - and costs 36x the latency. That second result contradicts "
        "what was claimed during development."
    ),
}


@app.get("/traces", dependencies=[Depends(require_token)])
def traces(limit: int = 50) -> dict[str, Any]:
    """The most recent traces, newest first. Attributes are already redacted."""
    import json

    trace_dir = get_settings().observability.trace_dir
    records: list[dict[str, Any]] = []
    if trace_dir.exists():
        for path in sorted(trace_dir.glob("traces_*.jsonl"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                log.warning("could not read %s: %s", path.name, exc)
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
                if len(records) >= limit:
                    return {"traces": records}
    return {"traces": records}


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Error shape the UI already understands: {error, detail}."""
    codes = {400: "bad_request", 401: "unauthorized", 500: "internal", 503: "unavailable"}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": codes.get(exc.status_code, "error"), "detail": str(exc.detail)},
    )


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _to_response(answer: Answer, trace: CopilotTrace, elapsed_ms: float) -> AskResponse:
    package = answer.evidence
    sources = [_to_source(e) for e in (package.all_evidence if package else [])]

    sql_detail: SqlDetail | None = None
    if trace.generated_sql or trace.sql_blocked_reason:
        sql_detail = SqlDetail(
            generated=trace.generated_sql,
            executed=trace.generated_sql,
            blocked=bool(trace.sql_blocked_reason),
            block_reason=trace.sql_blocked_reason,
            tenant_injected=any("tenant filter was missing" in w for w in answer.warnings),
            warnings=[w for w in answer.warnings if "tenant" not in w.lower()],
            provider=get_settings().text_to_sql_provider,
        )

    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    for evidence in package.sql_evidence if package else []:
        structured = evidence.structured or {}
        rows = structured.get("rows", []) or []
        columns = structured.get("columns", []) or (list(rows[0].keys()) if rows else [])
        if sql_detail is not None:
            sql_detail.executed = structured.get("sql", sql_detail.executed)
            sql_detail.tables = structured.get("tables", []) or []
            sql_detail.join_count = int(structured.get("join_count", 0) or 0)
        break

    timings = {k: round(v, 1) for k, v in trace.stage_ms.items()}
    timings["total"] = round(elapsed_ms, 1)

    settings = get_settings()
    return AskResponse(
        trace_id=trace.trace_id,
        question=answer.question,
        route=trace.routing.route.value if trace.routing else "document_rag",
        route_decided_by=trace.routing.decided_by if trace.routing else "",
        route_confidence=float(trace.routing.confidence) if trace.routing else 0.0,
        status=answer.status.value,
        answer=answer.text,
        grounded=answer.is_grounded,
        sources=sources,
        conflicts=list(package.conflicts) if package else [],
        notes=list(package.notes) if package else [],
        warnings=list(answer.warnings) + list(trace.errors),
        sql=sql_detail,
        columns=columns,
        rows=rows,
        row_count=trace.sql_row_count or len(rows),
        truncated=len(rows) >= settings.database.max_result_rows,
        timings_ms=timings,
        versions={
            "chat_model": answer.model or settings.profile.chat_model,
            "prompt_version": answer.prompt_version,
            "index_version": settings.vector_store.index_version,
            "sql_provider": settings.text_to_sql_provider,
        },
    )


def _to_source(evidence: Evidence) -> SourceRef:
    """Narrow one piece of evidence to what the browser may see.

    `access_group` and `tenant` are deliberately dropped: they exist so an
    answer can be audited server-side, and telling a browser which access group
    a document belongs to is a small disclosure with no upside.
    """
    text = evidence.text or ""
    return SourceRef(
        id=evidence.evidence_id,
        type=evidence.evidence_type.value,
        title=evidence.source_title or evidence.source_id,
        version=evidence.version,
        section=evidence.section,
        effective_date=evidence.effective_date,
        authority=evidence.authority,
        score=evidence.retrieval_score,
        rerank_score=evidence.rerank_score,
        retrieval_method=evidence.retrieval_method,
        snippet=text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else ""),
    )


def _short(exc: Exception) -> str:
    """Error text for a client. Truncated, and never a connection string."""
    text = str(exc)
    for marker in ("PWD=", "Password=", "password="):
        if marker in text:
            return f"{type(exc).__name__}: [redacted connection error]"
    return f"{type(exc).__name__}: {text[:200]}"


def _dev_token() -> str:
    """Used only by `python main.py` for a local run."""
    return secrets.token_urlsafe(24)


if __name__ == "__main__":
    import uvicorn

    if not os.environ.get("BACKEND_TOKEN"):
        token = _dev_token()
        os.environ["BACKEND_TOKEN"] = token
        print(f"\nBACKEND_TOKEN not set. Using a throwaway one for this run:\n  {token}\n")

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
