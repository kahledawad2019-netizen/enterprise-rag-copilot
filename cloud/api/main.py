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

import asyncio
import hmac
import json
import logging
import os
import queue
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# The copilot package lives alongside this file in the image. Adding it here
# rather than relying on the working directory means `uvicorn main:app` behaves
# the same from any cwd - the same class of bug the project already hit once
# with relative Qdrant paths.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent / "local-enterprise-copilot"
if (PROJECT_ROOT / "src").exists():
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
# Sibling modules (uploads.py) must import however this file was loaded -
# the tests load it by path, not as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from enterprise_copilot.config import get_settings  # noqa: E402
from enterprise_copilot.models.evidence import Answer, Evidence  # noqa: E402
from enterprise_copilot.retrieval.hybrid import UserContext  # noqa: E402
from enterprise_copilot.routing.orchestrator import Copilot, CopilotTrace  # noqa: E402

import uploads  # noqa: E402

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

# Questions the document corpus alone can answer, shown when no database is
# configured, so the first thing a visitor clicks works.
DOCUMENT_EXAMPLE_QUESTIONS = [
    "What is the refund policy for annual plans?",
    "What is the maximum discount a sales rep can approve?",
    "What happened in INC-2025-0042 and what was the root cause?",
    "How quickly must a P1 incident receive a first response?",
    "How is the customer health score calculated?",
    "Delete all customers",
]

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
    persona: str = "guest"
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
    # Which runtime this is, for the provider badge (LOCAL - OLLAMA or
    # CLOUD - GROQ). Deliberately no hosts, keys or paths.
    deployment_mode: Literal["local", "cloud"] = "local"
    llm_provider: str = ""
    embedding_provider: str = ""
    embedding_model: str = ""
    sql_enabled: bool = True
    upload_enabled: bool = False
    max_upload_mb: int = 0
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
# A plain Lock, not an RLock: the streaming endpoint takes it in a worker
# thread, and nothing re-enters it.
_copilot_lock = threading.Lock()

# How long a request waits for the copilot before a 503. The copilot is
# serialised (see /ask); without a bound, one slow generation would queue every
# other visitor indefinitely.
LOCK_TIMEOUT_SECONDS = float(os.environ.get("COPILOT_LOCK_TIMEOUT_SECONDS", "180"))


def _acquire() -> None:
    if not _copilot_lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
        raise HTTPException(status_code=503, detail="The copilot is busy. Please retry in a moment.")


@contextmanager
def _locked() -> Iterator[None]:
    _acquire()
    try:
        yield
    finally:
        _copilot_lock.release()


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


@app.middleware("http")
async def limit_upload_body(request: Request, call_next: Any) -> Response:
    """Refuse an oversized upload from its headers, before the body is read.

    FastAPI parses a multipart body - spooling it to disk - before any
    endpoint dependency runs, so a size check in the endpoint only runs after
    an anonymous caller has already written whatever they sent. The declared
    Content-Length is checked here instead, and a body without one is refused
    (browsers always send it for a file upload). The server enforces that the
    body matches its declared length.
    """
    if request.method == "POST" and request.url.path.endswith("/documents/upload"):
        allowed = _max_upload_mb() * 1024 * 1024 + 64 * 1024  # file + multipart framing
        declared = request.headers.get("content-length")
        if declared is None:
            return JSONResponse(
                status_code=411, content={"error": "length_required", "detail": "Content-Length is required."}
            )
        if not declared.isdigit() or int(declared) > allowed:
            return JSONResponse(
                status_code=413,
                content={"error": "too_large", "detail": f"The file is larger than {_max_upload_mb()} MB."},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticatedCaller:
    """Identity asserted by the Worker and its server-side permission mapping."""

    email: str
    persona_key: str | None
    demo_mode: bool


def _identity_map() -> dict[str, str]:
    """Email -> persona mapping supplied to the backend, never by the browser."""
    raw = os.environ.get("COPILOT_IDENTITY_MAP_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="COPILOT_IDENTITY_MAP_JSON is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=500,
            detail="COPILOT_IDENTITY_MAP_JSON must be a JSON object.",
        )
    mapping = {str(email).strip().lower(): str(persona) for email, persona in payload.items()}
    unknown = sorted({persona for persona in mapping.values() if persona not in PERSONAS})
    if unknown:
        raise HTTPException(
            status_code=500,
            detail=f"Identity map contains unknown persona(s): {', '.join(unknown)}.",
        )
    return mapping


def auth_mode() -> str:
    """`token` (default): only a gateway holding BACKEND_TOKEN may call.

    `public`: anyone may call. For a public demo over a synthetic corpus and
    for the single-user local app. Personas are then selectable exactly as in
    demo mode, and the expensive endpoints are rate limited per address.
    """
    return os.environ.get("API_AUTH_MODE", "token").strip().lower()


_RATE_WINDOW_SECONDS = 60.0
_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()


def _client_address(request: Request) -> str:
    """The rate-limit bucket key.

    X-Forwarded-For is client-controlled except for the entries appended by
    proxies we trust, so the leftmost entry can be anything a caller likes.
    With TRUSTED_PROXY_HOPS=N (the cloud image sets 1, for Render's proxy)
    the address the outermost trusted proxy saw is the N-th entry from the
    right. With 0 - direct exposure, or local - the header is ignored.
    """
    hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0") or 0)
    if hops > 0:
        entries = [e.strip() for e in request.headers.get("x-forwarded-for", "").split(",") if e.strip()]
        if len(entries) >= hops:
            return entries[-hops]
    return request.client.host if request.client else "unknown"


def _prune_rate_buckets(now: float) -> None:
    """Drop buckets whose last hit is outside the window. Caller holds the lock."""
    for key in [k for k, v in _rate_hits.items() if not v or now - v[-1] > _RATE_WINDOW_SECONDS]:
        del _rate_hits[key]


def rate_limit(request: Request) -> None:
    """Per-address limit on the expensive endpoints.

    Generation spends the operator's hosted-model quota, so a public demo
    without this is one script away from being unusable for everyone else.
    In memory and per process: the container runs a single worker by design.
    """
    default = "12" if auth_mode() == "public" else "0"
    limit = int(os.environ.get("RATE_LIMIT_PER_MINUTE", default) or 0)
    if limit <= 0:
        return
    now = time.monotonic()
    key = _client_address(request)
    with _rate_lock:
        hits = _rate_hits[key]
        while hits and now - hits[0] > _RATE_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(
                status_code=429, detail="Too many requests. Please wait a minute and try again."
            )
        hits.append(now)
        # Bound memory under address churn: prune by age once the table is
        # large, and if it is still large (a flood of fresh addresses) start
        # over rather than grow without limit.
        if len(_rate_hits) > 10_000:
            _prune_rate_buckets(now)
            if len(_rate_hits) > 10_000:
                _rate_hits.clear()


def require_caller(
    authorization: str = Header(default=""),
    asserted_email: str = Header(default="", alias="X-Copilot-User"),
) -> AuthenticatedCaller:
    """Authenticate the Worker and bind its identity to server-side permissions.

    The container has a public URL, so this is the only thing standing between
    the copilot and the internet. Compared with `compare_digest` so a wrong
    token cannot be recovered one byte at a time from response timing.
    """
    if auth_mode() == "public":
        return AuthenticatedCaller(email="public", persona_key=None, demo_mode=True)

    expected = os.environ.get("BACKEND_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="BACKEND_TOKEN is not set on the backend; refusing to serve unauthenticated.",
        )

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")

    if get_settings().demo_mode:
        return AuthenticatedCaller(
            email=asserted_email.strip().lower() or "local-demo",
            persona_key=None,
            demo_mode=True,
        )

    email = asserted_email.strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="The gateway did not assert a user identity.")

    persona_key = _identity_map().get(email)
    if persona_key is None:
        raise HTTPException(status_code=403, detail="This identity has no copilot role mapping.")
    return AuthenticatedCaller(email=email, persona_key=persona_key, demo_mode=False)


def _resolve_persona(caller: AuthenticatedCaller, requested: str | None) -> tuple[str, dict[str, Any]]:
    """Only demo mode may choose a persona; production identity owns exactly one."""
    key = (requested or "guest") if caller.demo_mode else caller.persona_key
    persona = PERSONAS.get(key or "")
    if persona is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona {key!r}.")
    return str(key), persona


def _chunk_count() -> int:
    """How many chunks are indexed, without opening a second Qdrant client.

    Embedded Qdrant permits one client per storage folder. The copilot already
    holds one open for the life of the process, so anything else that wants to
    read the index must borrow it — constructing a second `QdrantVectorStore`
    raises "already accessed by another instance of Qdrant client" and makes a
    perfectly good index look broken. This is the same single-process
    constraint that keeps the container at one uvicorn worker.
    """
    if _copilot is None:
        from enterprise_copilot.retrieval.vector_store import QdrantVectorStore

        # Nothing holds the lock when the copilot failed to start, so opening
        # a client here is safe and is the only way to report the index at all.
        return QdrantVectorStore(get_settings()).count()
    return _copilot.retriever.store.count()


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
def health(response: Response) -> HealthResponse:
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

    # Vector store. Reuses the copilot's open client rather than opening a
    # second one: embedded Qdrant allows a single client per storage folder,
    # so constructing one here fails with "already accessed by another
    # instance" and reports a healthy index as broken.
    try:
        count = _chunk_count()
        checks.append(
            HealthCheck(name="vector_store", ok=count > 0, detail=f"{count} chunks indexed")
        )
    except Exception as exc:
        log.warning("vector-store health check failed: %s", _short(exc))
        checks.append(HealthCheck(name="vector_store", ok=False, detail="unavailable"))

    # Database. A documents-only deployment has none and must not report
    # itself unready for lacking one.
    if not settings.sql_enabled:
        checks.append(HealthCheck(name="database", ok=True, detail="disabled (documents only)"))
    else:
        try:
            from enterprise_copilot.database.connection import raw_connection

            with raw_connection(settings) as conn:
                conn.cursor().execute("SELECT 1")
            checks.append(HealthCheck(name="database", ok=True, detail="reachable"))
        except Exception as exc:
            log.warning("database health check failed: %s", _short(exc))
            checks.append(HealthCheck(name="database", ok=False, detail="unavailable"))

    # Chat model. Which provider is in play decides what "reachable" means,
    # so probe accordingly rather than always asking Ollama - a container
    # configured for Groq has no Ollama, and reporting that as a failure would
    # mark a perfectly healthy deployment degraded.
    try:
        from enterprise_copilot.llm import build_chat_client

        if settings.llm.provider == "ollama":
            client = build_chat_client(settings)
            models = [m.get("model", "") for m in client.list().get("models", [])]
            wanted = settings.chat_model
            ok = wanted in models or f"{wanted}:latest" in models
            detail = f"ollama, {wanted} " + ("installed" if ok else "NOT installed")
        else:
            # Constructing validates the key and model without spending a
            # token. A real generation on every health check would be billed
            # once per probe, forever.
            build_chat_client(settings)
            detail = "configured"
            ok = True
        checks.append(HealthCheck(name="chat_model", ok=ok, detail=detail))
    except Exception as exc:
        log.warning("chat-model health check failed: %s", _short(exc))
        checks.append(HealthCheck(name="chat_model", ok=False, detail="unavailable"))

    # Whether anything leaves the machine. Not a failure - a disclosure, so an
    # operator can see the posture without reading the container's env.
    try:
        from enterprise_copilot.llm import describe_providers

        posture = describe_providers(settings)
        checks.append(
            HealthCheck(
                name="data_locality",
                ok=True,
                detail=(
                    f"chat={posture['chat_provider']}, "
                    f"embeddings={posture['embedding_provider']}, "
                    f"leaves machine: {posture['leaves_machine']}"
                ),
            )
        )
    except Exception as exc:
        log.warning("data-locality health check failed: %s", _short(exc))
        checks.append(HealthCheck(name="data_locality", ok=False, detail="unavailable"))

    if all(c.ok for c in checks):
        status: Literal["ok", "degraded", "down"] = "ok"
    elif _copilot is None:
        status = "down"
    else:
        status = "degraded"

    # This endpoint is readiness, not merely proof that Python is alive. The
    # container must not receive traffic with an empty index or dead database.
    if status != "ok":
        response.status_code = 503

    return HealthResponse(status=status, checks=checks)


@app.get("/meta", response_model=MetaResponse)
def meta(caller: AuthenticatedCaller = Depends(require_caller)) -> MetaResponse:
    settings = get_settings()

    document_count = 0
    chunk_count = 0
    try:
        chunk_count = _chunk_count()
        document_count = len(_document_paths(settings.documents_dir))
    except Exception as exc:  # a missing index must not break the whole UI
        log.warning("Could not read corpus counts: %s", exc)

    # Outside demo mode a caller owns exactly one persona. Routing through
    # _resolve_persona rather than indexing PERSONAS directly keeps that rule
    # in one place and lets the types carry it: persona_key is Optional only
    # because demo mode leaves it unset, and require_caller has already
    # returned 403 for an unmapped identity by the time this runs.
    if caller.demo_mode:
        visible_personas = PERSONAS
    else:
        key, persona = _resolve_persona(caller, None)
        visible_personas = {key: persona}
    upload_enabled = _uploads_enabled()
    return MetaResponse(
        app_name=settings.app_name,
        deployment_mode="local" if settings.llm.provider == "ollama" else "cloud",
        llm_provider=settings.llm.provider,
        embedding_provider=settings.embeddings.provider,
        embedding_model=settings.embedding_model,
        sql_enabled=settings.sql_enabled,
        upload_enabled=upload_enabled,
        max_upload_mb=_max_upload_mb() if upload_enabled else 0,
        personas=[
            PersonaOut(
                key=key,
                label=p["label"],
                tenant=p["tenant"],
                tenant_id=p["tenant_id"],
                access_groups=p["access_groups"],
                description=p["description"],
            )
            for key, p in visible_personas.items()
        ],
        strategies=["dense", "sparse", "hybrid", "reranked"],
        default_strategy="reranked",
        sql_provider=settings.text_to_sql_provider,
        # The profile carries the local Ollama default. Hosted deployments can
        # override it (for example with Groq/Qwen), so report the resolved
        # model that the running clients actually use.
        chat_model=settings.chat_model,
        index_version=settings.vector_store.index_version,
        document_count=document_count,
        chunk_count=chunk_count,
        example_questions=EXAMPLE_QUESTIONS if settings.sql_enabled else DOCUMENT_EXAMPLE_QUESTIONS,
    )


def _user_for(persona: dict[str, Any]) -> UserContext:
    return UserContext(
        user_name=persona["label"],
        tenant=persona["tenant"],
        access_groups=list(persona["access_groups"]),
        is_admin=persona["is_admin"],
    )


@app.post("/ask", response_model=AskResponse)
def ask(
    body: AskRequest,
    request: Request,
    caller: AuthenticatedCaller = Depends(require_caller),
    copilot: Copilot = Depends(get_copilot),
    _: None = Depends(rate_limit),
) -> AskResponse:
    persona_key, persona = _resolve_persona(caller, body.persona)
    user = _user_for(persona)

    # The identity Cloudflare Access verified, if there was one. Recorded on
    # the trace so an audit can tie a query to a person, not just a persona.
    request_id = request.headers.get("X-Request-Id", "")
    log.info("request %s from %s as %s", request_id, caller.email, persona_key)

    started = time.perf_counter()
    try:
        # The current tracer and lazy SQL provider are process-global mutable
        # state. Serialize requests until those internals become request-scoped;
        # otherwise concurrent users can mix spans and provider state.
        with _locked():
            answer, trace = copilot.ask(
                body.question,
                user=user,
                tenant_id=persona["tenant_id"],
                strategy=body.strategy,
            )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("ask failed (request %s)", request_id)
        detail = "The request failed. Check the protected backend logs"
        if request_id:
            detail += f" using request id {request_id}"
        raise HTTPException(status_code=500, detail=detail + ".") from exc

    elapsed = (time.perf_counter() - started) * 1000
    return _to_response(answer, trace, elapsed)


def _sse(kind: str, data: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/ask/stream")
def ask_stream(
    body: AskRequest,
    request: Request,
    caller: AuthenticatedCaller = Depends(require_caller),
    copilot: Copilot = Depends(get_copilot),
    _: None = Depends(rate_limit),
) -> StreamingResponse:
    """The same answer as /ask, streamed as server-sent events.

    Events, in order: `sources` (route and evidence, before the first token),
    `token` (answer text), then `done` (the full /ask response with citations
    validated) or `error` (a safe message). A CPU-bound local model can take a
    minute; sources at once and text as it arrives beats a spinner.

    Generation runs in its own thread, which takes and releases the copilot
    lock itself; the HTTP response only drains a queue. Iterating the
    generator from the threadpool instead would acquire the lock in one
    thread and release it from another.

    The producer stops - releasing the lock - when the client goes away (Stop
    button, closed tab) or after STREAM_DEADLINE_SECONDS. Breaking out closes
    the model stream too, so an abandoned answer does not keep the model busy
    and every other visitor queued behind it.
    """
    persona_key, persona = _resolve_persona(caller, body.persona)
    user = _user_for(persona)
    log.info("stream request as %s", persona_key)

    events: queue.Queue[str | None] = queue.Queue()
    cancelled = threading.Event()
    deadline = float(os.environ.get("STREAM_DEADLINE_SECONDS", "300"))

    def produce() -> None:
        started = time.perf_counter()
        try:
            with _locked():
                trace: CopilotTrace | None = None
                for kind, payload in copilot.ask_stream(
                    body.question,
                    user=user,
                    tenant_id=persona["tenant_id"],
                    strategy=body.strategy,
                ):
                    if kind == "prepared":
                        trace = payload.trace
                        evidence = payload.package.all_evidence if payload.package else []
                        events.put(
                            _sse(
                                "sources",
                                {
                                    "trace_id": trace.trace_id,
                                    "route": trace.routing.route.value if trace.routing else "",
                                    "sources": [_to_source(e).model_dump() for e in evidence],
                                },
                            )
                        )
                    elif kind == "token":
                        events.put(_sse("token", {"text": payload}))
                    elif kind == "answer" and trace is not None:
                        elapsed = (time.perf_counter() - started) * 1000
                        events.put(_sse("done", _to_response(payload, trace, elapsed).model_dump()))
                    if cancelled.is_set():
                        log.info("stream stopped: client disconnected")
                        break  # closes the generator chain, down to the model stream
                    if time.perf_counter() - started > deadline:
                        events.put(
                            _sse(
                                "error",
                                {"error": "timeout", "detail": "The answer took too long and was stopped."},
                            )
                        )
                        break
        except HTTPException as exc:
            events.put(_sse("error", {"error": "unavailable", "detail": exc.detail}))
        except Exception:
            log.exception("streamed ask failed")
            events.put(_sse("error", {"error": "internal", "detail": "The request failed."}))
        finally:
            events.put(None)

    threading.Thread(target=produce, name="ask-stream", daemon=True).start()

    async def drain() -> AsyncIterator[str]:
        idle = 0.0
        try:
            while True:
                try:
                    item = events.get_nowait()
                except queue.Empty:
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(0.05)
                    idle += 0.05
                    if idle >= 15:
                        idle = 0.0
                        yield ": keep-alive\n\n"  # proxies drop idle connections
                    continue
                idle = 0.0
                if item is None:
                    return
                yield item
        finally:
            # Runs on normal end, on disconnect, and when the server cancels
            # the response task. The producer checks this between tokens.
            cancelled.set()

    return StreamingResponse(
        drain(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    persona: str = "guest"
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


@app.post("/retrieve", response_model=RetrieveResponse)
def retrieve(
    body: RetrieveRequest,
    caller: AuthenticatedCaller = Depends(require_caller),
    copilot: Copilot = Depends(get_copilot),
    _limited: None = Depends(rate_limit),
) -> RetrieveResponse:
    """Run the same query through several strategies so they can be compared.

    This is the project's most informative screen: it is where "hybrid beats
    dense" stops being a claim and becomes something you can watch happen.
    Try `INC-2025-0042` - dense returns the wrong incident, sparse gets it
    right at rank 1, and hybrid keeps the sparse answer.
    """
    _, persona = _resolve_persona(caller, body.persona)

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
            with _locked():
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
                    error=f"{type(exc).__name__}: retrieval unavailable",
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


class DocumentOut(BaseModel):
    doc_id: str
    title: str
    doc_type: str = ""
    version: str = ""
    effective_date: str = ""
    status: str = ""
    authority: str = ""
    department: str = ""
    owner: str = ""
    supersedes: str | None = None
    superseded_by: str | None = None
    related_docs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    words: int = 0
    readable: bool = True
    source: Literal["corpus", "upload"] = "corpus"


@app.get("/documents")
def documents(
    persona: str = "guest",
    caller: AuthenticatedCaller = Depends(require_caller),
) -> dict[str, Any]:
    """The corpus, filtered by what this persona is allowed to see.

    Filtering happens here rather than in the browser. Sending the full list
    and hiding rows client-side would put the titles of documents a guest
    cannot read into a payload a guest receives, which is the same disclosure
    the retrieval filters exist to prevent.

    `access_group` and `tenant` are not returned. They decide the filter; they
    are not the browser's business.
    """
    persona_key, p = _resolve_persona(caller, persona)

    allowed_groups = {g.lower() for g in p["access_groups"]}
    tenant = str(p["tenant"]).lower()

    settings = get_settings()
    out: list[DocumentOut] = []
    hidden = 0

    for path in _document_paths(settings.documents_dir):
        try:
            meta, body = _read_metadata(path)
        except Exception as exc:
            log.warning("could not parse %s: %s", path.name, exc)
            continue

        group = str(meta.get("access_group", "public")).lower()
        doc_tenant = str(meta.get("tenant", "all")).lower()

        if not p["is_admin"]:
            if group not in allowed_groups:
                hidden += 1
                continue
            if doc_tenant not in ("all", tenant):
                hidden += 1
                continue

        out.append(
            DocumentOut(
                doc_id=str(meta.get("doc_id", path.stem)),
                title=str(meta.get("title", path.stem)),
                doc_type=str(meta.get("doc_type", "")),
                version=str(meta.get("version", "")),
                effective_date=str(meta.get("effective_date", "")),
                status=str(meta.get("status", "")),
                authority=str(meta.get("authority", "")),
                department=str(meta.get("department", "")),
                owner=str(meta.get("owner", "")),
                supersedes=_opt(meta.get("supersedes")),
                superseded_by=_opt(meta.get("superseded_by")),
                related_docs=_as_list(meta.get("related_docs")),
                tags=_as_list(meta.get("tags")),
                words=len(body.split()),
                source="upload" if path.parent.name == uploads.UPLOAD_SUBDIR else "corpus",
            )
        )

    return {
        "documents": [d.model_dump() for d in out],
        "total": len(out),
        "hidden_by_permissions": hidden,
        "persona": persona_key,
    }


def _document_paths(root: Path) -> list[Path]:
    """The curated corpus plus anything uploaded, in a stable order."""
    paths = sorted(root.glob("*.md"))
    upload_dir = root / uploads.UPLOAD_SUBDIR
    if upload_dir.exists():
        paths += sorted(
            p for p in upload_dir.iterdir() if p.suffix.lower() in {".md", ".pdf", ".docx"}
        )
    return paths


def _read_metadata(path: Path) -> tuple[dict[str, Any], str]:
    if path.suffix.lower() == ".md":
        return _read_front_matter(path)
    import yaml

    sidecar = path.with_suffix(".meta.yaml")
    meta = yaml.safe_load(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    return meta or {}, ""


def _uploads_enabled() -> bool:
    return os.environ.get("UPLOADS_ENABLED", "true").strip().lower() in {"1", "true", "yes"}


def _max_upload_mb() -> int:
    return max(1, int(os.environ.get("MAX_UPLOAD_MB", "5")))


def _remove_upload(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_suffix(".meta.yaml").unlink(missing_ok=True)


@app.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    _caller: AuthenticatedCaller = Depends(require_caller),
    copilot: Copilot = Depends(get_copilot),
    _: None = Depends(rate_limit),
) -> dict[str, Any]:
    """Add a document to the knowledge base and index it immediately.

    Indexing is incremental - only new or changed documents are embedded - and
    reuses the copilot's own vector-store client, because embedded Qdrant
    allows one per folder. The BM25 index is reloaded afterwards so sparse
    search sees the new text too.

    Everything from the capacity check to the cleanup runs under the copilot
    lock, in one worker thread. Checking capacity, writing the file and
    indexing are then one step as far as any other upload or question can
    tell: no request can index another's half-written file, and two requests
    cannot both pass the limit. The request body itself is size-capped before
    it is parsed (see `limit_upload_body`).
    """
    from starlette.concurrency import run_in_threadpool

    if not _uploads_enabled():
        raise HTTPException(status_code=403, detail="Uploads are disabled on this deployment.")

    settings = get_settings()
    max_bytes = _max_upload_mb() * 1024 * 1024
    content = await file.read(max_bytes + 1)
    filename = file.filename or "document"
    try:
        uploads.validate(filename, content, max_bytes=max_bytes)
    except uploads.UploadRejectedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def ingest() -> dict[str, Any]:
        from enterprise_copilot.ingestion.pipeline import IngestionPipeline

        with _locked():
            doc_id = uploads.doc_id_for(content)
            if uploads.existing_upload(settings.documents_dir, doc_id) is not None:
                # The id is derived from the content: this exact file is
                # already indexed. Nothing to do, and nothing to overwrite.
                return {"doc_id": doc_id, "title": uploads.title_for(filename),
                        "chunks_written": 0, "already_indexed": True}

            limit = int(os.environ.get("MAX_UPLOADED_DOCUMENTS", "25"))
            if uploads.count_uploads(settings.documents_dir) >= limit:
                raise HTTPException(
                    status_code=409, detail="This deployment's upload limit is reached."
                )

            path, doc_id = uploads.store(
                filename, content, settings.documents_dir, max_bytes=max_bytes
            )
            pipeline = IngestionPipeline(
                settings, embedder=copilot.retriever.embedder, store=copilot.retriever.store
            )
            try:
                report, manifest = pipeline.build_index()
                if any(name == path.name for name, _ in report.errors):
                    raise uploads.UploadRejectedError("The document could not be read.")
            except Exception:
                # Undo everything this upload committed: the file, any vector
                # batches already upserted, and the sparse index derived from
                # them. Otherwise a half-indexed document stays searchable.
                _remove_upload(path)
                try:
                    copilot.retriever.store.delete_document(doc_id)
                    pipeline._rebuild_sparse_index()
                except Exception:
                    log.exception("rollback of a failed upload was incomplete")
                raise
            finally:
                copilot.retriever.sparse = copilot.retriever._load_sparse()

            return {
                "doc_id": doc_id,
                "title": uploads.title_for(filename),
                "chunks_written": report.chunks_created,
                "chunk_count": manifest.chunk_count,
            }

    try:
        return await run_in_threadpool(ingest)
    except HTTPException:
        raise
    except uploads.UploadRejectedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("indexing the upload failed")
        raise HTTPException(status_code=500, detail="The document could not be indexed.") from exc


def _read_front_matter(path: Path) -> tuple[dict[str, Any], str]:
    """YAML front matter and the body, without the ingestion pipeline.

    Deliberately not `ingestion.parsers`: that chunks, hashes and validates
    against a pydantic model, which is the right thing when building an index
    and far too much work to render a list.
    """
    import yaml

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text

    _, _, rest = text.partition("---")
    front, separator, body = rest.partition("---")
    if not separator:
        return {}, text
    return yaml.safe_load(front) or {}, body


def _opt(value: Any) -> str | None:
    return None if value in (None, "", "null") else str(value)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


@app.get("/evaluation")
def evaluation(_: AuthenticatedCaller = Depends(require_caller)) -> dict[str, Any]:
    """Return a versioned evaluation baseline with explicit freshness status.

    Served from disk rather than recomputed: a full evaluation takes minutes
    of model time, and a dashboard that silently re-runs it on every page load
    is a dashboard nobody opens twice.
    """
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

    baseline_path = (
        PROJECT_ROOT / "evals" / "baselines" / "document_rag_holdout_qwen3_v1.json"
    )
    if not baseline_path.exists():
        raise HTTPException(status_code=503, detail="The evaluation baseline is unavailable.")

    report = json.loads(baseline_path.read_text(encoding="utf-8"))
    settings = get_settings()
    live_provider = settings.embeddings.provider
    live_model = settings.embedding_model
    live_index_version = settings.vector_store.index_version
    stale_reasons: list[str] = []
    if report["evaluated_embedding_provider"] != live_provider:
        stale_reasons.append(
            "embedding provider changed from "
            f"{report['evaluated_embedding_provider']} to {live_provider}"
        )
    if report["evaluated_embedding_model"] != live_model:
        stale_reasons.append(
            "embedding model changed from "
            f"{report['evaluated_embedding_model']} to {live_model}"
        )
    if report["evaluated_index_version"] != live_index_version:
        stale_reasons.append(
            "index version changed from "
            f"{report['evaluated_index_version']} to {live_index_version}"
        )

    report.update(
        {
            "current": not stale_reasons,
            "stale_reasons": stale_reasons,
            "live_embedding_provider": live_provider,
            "live_embedding_model": live_model,
            "live_index_version": live_index_version,
        }
    )
    return {"runs": runs, "report": report}


@app.get("/traces")
def traces(
    limit: int = 50,
    caller: AuthenticatedCaller = Depends(require_caller),
) -> dict[str, Any]:
    """The most recent traces, newest first. Attributes are already redacted."""
    _, persona = _resolve_persona(caller, None)
    if not persona["is_admin"]:
        raise HTTPException(status_code=403, detail="Administrator role required.")

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
    codes = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        409: "conflict",
        429: "rate_limited",
        500: "internal",
        503: "unavailable",
    }
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
        warnings=list(answer.warnings) + _public_errors(trace.errors),
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


def _public_errors(errors: list[str]) -> list[str]:
    """Stage-level messages for the browser.

    The raw exception text - which can hold hosts, storage paths or a
    provider's error body - stays in the server log and the trace.
    """
    messages = {
        "retrieval": "Document search was unavailable for this question.",
        "generation": "The language model was unavailable.",
    }
    out: list[str] = []
    for error in errors:
        stage = error.split(":", 1)[0].strip().lower()
        if stage in messages:
            message = messages[stage]
        elif "sql" in stage:
            message = "The database step could not complete."
        else:
            message = "A pipeline step failed."
        if message not in out:
            out.append(message)
    return out


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
