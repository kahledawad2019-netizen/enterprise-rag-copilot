"""
Northwind Copilot - cloud mode on Streamlit Community Cloud.

The same RAG engine as the Docker/FastAPI deployment (`enterprise_copilot`),
with Streamlit as the web layer instead of FastAPI + React:

    Groq (chat)  +  fastembed ONNX (embeddings, in-process)
    embedded Qdrant + BM25, built from the committed corpus on first start

Deploy: share.streamlit.io -> New app -> this repository, main file
`cloud/streamlit/streamlit_app.py`, Python 3.12, and in Advanced settings ->
Secrets:

    GROQ_API_KEY = "gsk_..."

The key is read server-side from `st.secrets` and never sent to the browser.

Run locally:
    local-enterprise-copilot/.venv/Scripts/python -m streamlit run cloud/streamlit/streamlit_app.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "local-enterprise-copilot"
if str(PACKAGE_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT / "src"))

# ---------------------------------------------------------------------------
# Configuration. Must be in the environment before enterprise_copilot reads
# its settings. Mirrors the cloud defaults in the root Dockerfile.
#
# FORCED values override anything in local-enterprise-copilot/.env, so a
# developer's local Ollama settings can never leak into this mode (the same
# rule run_local.py applies the other way round).
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("COPILOT_STATE_DIR") or Path(tempfile.gettempdir()) / "northwind-copilot")

FORCED = {
    "LLM_PROVIDER": "groq",
    "EMBEDDING_PROVIDER": "fastembed",
    "EMBEDDING_MODEL": "nomic-ai/nomic-embed-text-v1.5-Q",
    # Embedded, read-only analytics database built on first start from
    # sql/postgres + the synthetic generator: Text-to-SQL with no DB account.
    "DATABASE_BACKEND": "duckdb",
    "DUCKDB_PATH": str(STATE_DIR / "northwind.duckdb"),
    "QDRANT_MODE": "embedded",
    "QDRANT_PATH": str(STATE_DIR / "qdrant"),
    "QDRANT_INDEX_VERSION": "streamlit-nomic-v1",
}
DEFAULTS = {
    "GROQ_MODEL": "qwen/qwen3.8-27b",
    "COPILOT_PROFILE": "lite",
    "COPILOT_DEMO_MODE": "true",
    "TEXT_TO_SQL_PROVIDER": "vanna",
    "EMBEDDING_CACHE_DIR": str(STATE_DIR / "models"),
    "OBS_LOG_FORMAT": "console",
}


def _secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
    except Exception:  # noqa: BLE001 - no secrets.toml at all raises a Streamlit-internal error
        value = None
    return str(value) if value else None


def configure_environment() -> None:
    os.environ.update(FORCED)
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, _secret(key) or value)
    # Secrets override the defaults above; the key is required.
    for key in ("GROQ_API_KEY", "GROQ_MODEL", "GROQ_FALLBACK_MODELS", "TEXT_TO_SQL_PROVIDER"):
        value = _secret(key)
        if value:
            os.environ[key] = value


configure_environment()

# Questions per visitor session and for the whole app, per minute. The global
# cap protects the shared Groq free-tier quota from one busy visitor or bot.
# Sized from measurement: a grounded answer uses ~5k Groq tokens (routing,
# safety screen, SQL, answer) and the free tier gives each model 8k tokens a
# minute; with the two fallback models that is roughly 4-5 answers a minute.
SESSION_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "6"))
GLOBAL_LIMIT_PER_MINUTE = int(os.environ.get("GLOBAL_RATE_LIMIT_PER_MINUTE", "8"))
MAX_TABLE_ROWS = 50
MAX_QUESTION_CHARS = 2000

# Same personas as the API (cloud/api/main.py PERSONAS): switching persona
# changes what retrieval may return, not just what is displayed.
PERSONAS: dict[str, dict] = {
    "admin": {
        "label": "Administrator",
        "tenant": "all",
        "tenant_id": 1,
        "access_groups": ["public", "internal", "finance", "support", "security", "exec"],
        "description": "Sees everything. A baseline to compare the others against.",
        "is_admin": True,
    },
    "analyst_na": {
        "label": "Analyst — North America",
        "tenant": "NWC-NA",
        "tenant_id": 1,
        "access_groups": ["public", "internal", "finance"],
        "description": "Finance access, tenant NA only. No support or security material.",
        "is_admin": False,
    },
    "analyst_eu": {
        "label": "Analyst — Europe",
        "tenant": "NWC-EU",
        "tenant_id": 2,
        "access_groups": ["public", "internal", "finance"],
        "description": "The same role in another tenant. Ask the same question to see isolation.",
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
    "What is the refund policy for annual plans?",
    "Which five customers have the highest ARR?",
    "Show customers with more than three SLA breaches and summarise the SLA policy.",
    "What happened in INC-2025-0042 and what was the root cause?",
    "How many active customers do we have per region?",
    "What is the maximum discount a sales rep can approve?",
    "Delete all customers",
]

STAGE_MESSAGES = {
    "retrieval": "Document search was unavailable for this question.",
    "generation": "The language model was unavailable.",
}

st.set_page_config(page_title="Northwind Copilot", page_icon="💬", layout="wide")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner="Preparing the knowledge base (first start takes 1-3 minutes)...")
def load_copilot():
    """Build the index if needed, then the copilot. Once per server process.

    Streamlit Community Cloud has no build step, so the index the Docker image
    bakes in is built here on first start and reused until the app restarts.
    """
    from enterprise_copilot.config import get_settings
    from enterprise_copilot.ingestion.pipeline import IngestionPipeline
    from enterprise_copilot.observability.tracing import configure_logging
    from enterprise_copilot.routing.orchestrator import Copilot

    settings = get_settings()
    configure_logging(settings)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    pipeline = IngestionPipeline(settings)
    try:
        ok, _ = pipeline.validate_index()
        if not ok:
            pipeline.build_index(rebuild=True)
    finally:
        pipeline.store.close()

    if settings.database_backend == "duckdb":
        from enterprise_copilot.database.duckdb_store import ensure_database

        ensure_database(settings)

    copilot = Copilot(settings)
    # Train Vanna's store now rather than on the first visitor's question
    # (it embeds the schema, glossary and golden SQL once: ~30-60 s).
    provider = copilot.sql_provider
    if provider is not None and hasattr(provider, "ensure_trained"):
        try:
            provider.ensure_trained()
        except Exception:
            import logging

            logging.getLogger("copilot.streamlit").exception("Vanna warm-up failed")
    return copilot


@st.cache_resource
def engine_lock() -> threading.Lock:
    # One copilot per process with process-global tracing: serialise questions,
    # exactly like the API does.
    return threading.Lock()


@st.cache_resource
def global_window() -> tuple[threading.Lock, deque]:
    return threading.Lock(), deque()


def allow_request() -> str | None:
    """None if the question may run, otherwise a message for the visitor."""
    now = time.monotonic()
    session = st.session_state.setdefault("asked_at", deque())
    while session and now - session[0] > 60:
        session.popleft()
    if SESSION_LIMIT_PER_MINUTE and len(session) >= SESSION_LIMIT_PER_MINUTE:
        return "You are asking faster than the demo allows. Wait a minute and try again."

    lock, window = global_window()
    with lock:
        while window and now - window[0] > 60:
            window.popleft()
        if GLOBAL_LIMIT_PER_MINUTE and len(window) >= GLOBAL_LIMIT_PER_MINUTE:
            return "The demo is busy right now. Try again in a minute."
        window.append(now)
    session.append(now)
    return None


def data_view(sql_result, trace) -> dict:
    """What the page shows under "Data & SQL". Rows are capped for the page."""
    if sql_result is not None:
        return {
            "sql": sql_result.executed_sql,
            "rows": sql_result.rows[:MAX_TABLE_ROWS],
            "row_count": sql_result.row_count,
            "repairs": trace.sql_repairs if trace else 0,
        }
    if trace is not None and trace.generated_sql:
        return {"sql": trace.generated_sql, "rows": [], "row_count": None, "repairs": 0}
    return {}


def public_errors(errors: list[str]) -> list[str]:
    """Stage-level messages only; raw exception text stays in the server log."""
    out: list[str] = []
    for error in errors:
        stage = error.split(":", 1)[0].strip().lower()
        message = STAGE_MESSAGES.get(stage, "A pipeline step failed.")
        if message not in out:
            out.append(message)
    return out


def source_rows(evidence: list) -> list[dict]:
    rows = []
    for item in evidence:
        text = (item.text or "").strip()
        rows.append(
            {
                "id": item.evidence_id,
                "title": item.source_title or item.source_id,
                "version": item.version,
                "section": item.section,
                "effective": item.effective_date,
                "authority": item.authority,
                "snippet": text[:420] + ("…" if len(text) > 420 else ""),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_sources(sources: list[dict], cited: set[str]) -> None:
    if not sources:
        return
    shown = [s for s in sources if s["id"] in cited] or sources
    label = f"Sources ({len(shown)} cited)" if cited else f"Sources ({len(shown)})"
    with st.expander(label, expanded=False):
        for s in shown:
            meta = " · ".join(
                str(part)
                for part in (
                    f"v{s['version']}" if s["version"] else None,
                    s["section"],
                    f"effective {s['effective']}" if s["effective"] else None,
                    s["authority"],
                )
                if part
            )
            st.markdown(f"**[{s['id']}] {md(s['title'])}**")
            if meta:
                st.caption(md(meta))
            st.caption(md(s["snippet"]))


def render_details(details: dict) -> None:
    if not details:
        return
    status = details.get("status", "")
    grounded = details.get("grounded")
    badge = "✅ grounded" if grounded else ("ℹ️ " + status.replace("_", " ") if status else "")
    parts = [badge, f"route `{details.get('route', '')}`", f"{details.get('seconds', 0):.1f}s"]
    st.caption(" · ".join(p for p in parts if p))
    for warning in details.get("warnings", []):
        st.caption(f"⚠️ {warning}")


def md(text: str) -> str:
    """Streamlit renders $...$ as LaTeX; prices would turn into maths."""
    return text.replace("$", "\\$")


def render_data(data: dict) -> None:
    """The executed SQL and its rows: what the numbers in the answer came from."""
    if not data or not data.get("sql"):
        return
    label = f"Data & SQL ({data['row_count']} rows)" if data.get("row_count") is not None else "SQL"
    with st.expander(label, expanded=False):
        st.code(data["sql"], language="sql")
        if data.get("rows"):
            st.dataframe(data["rows"], use_container_width=True, hide_index=True)
            if data["row_count"] > len(data["rows"]):
                st.caption(f"Showing the first {len(data['rows'])} rows.")
        if data.get("repairs"):
            st.caption(
                f"The database rejected the first draft; the query was corrected "
                f"{data['repairs']} time(s) and re-validated by the guard."
            )


def render_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        st.markdown(md(message["content"]))
        if message["role"] == "assistant":
            render_details(message.get("details", {}))
            render_data(message.get("data", {}))
            render_sources(message.get("sources", []), set(message.get("cited", [])))


def answer(question: str, persona_key: str) -> dict:
    from enterprise_copilot.retrieval.hybrid import UserContext

    persona = PERSONAS[persona_key]
    user = UserContext(
        user_name=persona["label"],
        tenant=persona["tenant"],
        access_groups=list(persona["access_groups"]),
        is_admin=persona["is_admin"],
    )
    copilot = load_copilot()

    result: dict = {
        "role": "assistant",
        "content": "",
        "sources": [],
        "cited": [],
        "details": {},
        "data": {},
    }
    started = time.perf_counter()
    placeholder = st.empty()
    parts: list[str] = []
    final = None
    trace = None

    status = st.status("Searching the documents…", expanded=False)
    lock = engine_lock()
    if not lock.acquire(timeout=90):
        status.update(label="Busy", state="error")
        result["content"] = "The copilot is busy answering someone else. Please try again shortly."
        placeholder.markdown(result["content"])
        return result
    try:
        for kind, payload in copilot.ask_stream(
            question, user=user, tenant_id=persona["tenant_id"], strategy="hybrid"
        ):
            if kind == "prepared":
                trace = payload.trace
                evidence = payload.package.all_evidence if payload.package else []
                result["sources"] = source_rows(evidence)
                result["data"] = data_view(payload.sql_result, trace)
                status.update(label=f"Found {len(evidence)} sources · writing the answer…")
            elif kind == "token":
                parts.append(payload)
                placeholder.markdown(md("".join(parts).replace("【", "[").replace("】", "]")) + "▌")
            elif kind == "answer":
                final = payload
    except Exception:
        # Details go to the server log (Streamlit Cloud "Manage app" -> logs),
        # never to the page.
        import logging

        logging.getLogger("copilot.streamlit").exception("ask failed")
        status.update(label="Failed", state="error")
        result["content"] = "The request failed. Please try again."
        placeholder.markdown(result["content"])
        return result
    finally:
        lock.release()

    if final is not None:
        result["content"] = final.text or "".join(parts)
        result["cited"] = sorted({c.evidence_id for c in final.citations if c.is_valid})
        warnings = public_errors(trace.errors if trace else [])
        if trace and trace.sql_blocked_reason:
            warnings.insert(0, f"The database query was blocked by the safety guard: {trace.sql_blocked_reason}")
        result["details"] = {
            "status": final.status.value,
            "grounded": final.is_grounded,
            "route": trace.routing.route.value if trace and trace.routing else "document_rag",
            "seconds": time.perf_counter() - started,
            "warnings": warnings,
        }
    else:
        result["content"] = "".join(parts) or "No answer was produced. Please try again."

    status.update(label="Done", state="complete")
    placeholder.markdown(md(result["content"]))
    render_details(result["details"])
    render_data(result.get("data", {}))
    render_sources(result["sources"], set(result["cited"]))
    return result


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def main() -> None:
    if not os.environ.get("GROQ_API_KEY"):
        st.error(
            "GROQ_API_KEY is not configured. In Streamlit Community Cloud open "
            "**Manage app → Settings → Secrets** and add `GROQ_API_KEY = \"gsk_...\"`."
        )
        st.stop()

    with st.sidebar:
        st.title("Northwind Copilot")
        st.caption("☁️ CLOUD — GROQ · documents + database, every answer cited")

        persona_key = st.selectbox(
            "Acting as",
            list(PERSONAS),
            format_func=lambda k: PERSONAS[k]["label"],
            key="persona",
            help="Each persona has its own tenant and access groups. Retrieval is "
            "filtered before ranking, so a guest cannot see finance documents.",
        )
        persona = PERSONAS[persona_key]
        st.caption(persona["description"])
        st.caption("Access groups: " + ", ".join(persona["access_groups"]))

        st.divider()
        st.subheader("Try asking")
        for i, q in enumerate(EXAMPLE_QUESTIONS):
            if st.button(q, key=f"example_{i}", use_container_width=True):
                st.session_state["pending"] = q

        st.divider()
        if st.button("New conversation", use_container_width=True):
            st.session_state["messages"] = []
        st.caption(
            f"Model `{os.environ.get('GROQ_MODEL')}` on Groq · embeddings "
            "nomic-embed-text v1.5 (in-process) · hybrid dense + BM25 retrieval · "
            "Text-to-SQL by Vanna over a read-only DuckDB database. "
            "Synthetic demo data (Northwind Cloud)."
        )

    load_copilot()  # show the first-start spinner before the chat appears

    messages: list[dict] = st.session_state.setdefault("messages", [])
    if not messages:
        st.markdown("### Ask about Northwind's policies, incidents, customers and revenue")
        st.caption(
            "Policy questions are answered from documents [D1]; figures come from a "
            "read-only query of the company database [S1], shown under *Data & SQL*. "
            "Questions the sources cannot answer are declined rather than guessed."
        )
    for message in messages:
        render_message(message)

    typed = st.chat_input("Ask a question…", max_chars=MAX_QUESTION_CHARS)
    question = typed or st.session_state.pop("pending", None)
    if not question:
        return
    question = question.strip()[:MAX_QUESTION_CHARS]
    if not question:
        return

    messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(md(question))

    blocked = allow_request()
    with st.chat_message("assistant"):
        if blocked:
            st.warning(blocked)
            return
        messages.append(answer(question, persona_key))


main()
