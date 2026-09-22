"""
Shared helpers for the Streamlit pages.

Deliberately separate from `streamlit_app.py`. Streamlit executes a page script
top to bottom, so a page module that both renders itself *and* exports helpers
runs its own body every time another page imports it — producing a second
sidebar and `StreamlitDuplicateElementId`. Keeping the helpers here means
importing them has no side effects.

Every widget declared here passes an explicit `key`. Streamlit derives a
widget's identity from its type and parameters when no key is given, so two
similar widgets on one page collide.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from enterprise_copilot.config import get_settings  # noqa: E402

# Users the demo can act as. Each has a real tenant and access groups, so the
# permission model is exercised by switching user rather than described.
USERS: dict[str, tuple[str, int, list[str]]] = {
    "admin": ("all", 1, ["public", "internal", "finance", "support", "security", "exec"]),
    "analyst_na": ("NWC-NA", 1, ["public", "internal", "finance"]),
    "analyst_eu": ("NWC-EU", 2, ["public", "internal", "finance"]),
    "support_na": ("NWC-NA", 1, ["public", "internal", "support"]),
    "guest": ("all", 1, ["public"]),
}


@st.cache_resource(show_spinner="Starting the copilot (loading models and index)...")
def load_copilot():
    """Expensive clients, built once per process.

    Embedded Qdrant is single-process, so this holds the index open for as long
    as the app runs. Ingestion jobs must finish before the app starts.
    """
    from enterprise_copilot.observability.tracing import configure_logging
    from enterprise_copilot.routing.orchestrator import Copilot

    settings = get_settings()
    configure_logging(settings)
    return Copilot(settings)


@st.cache_resource
def load_health() -> tuple[dict[str, tuple[bool, str]], dict[str, tuple[bool, str]]]:
    """Probe the system once. Returns (connectivity, security_posture).

    These are deliberately kept apart. An earlier version listed the
    "read-only principal" warning among the connection indicators, so a red
    marker appeared under a heading called "Health" and was reasonably read as
    "the database is down" — when the database was connected and the marker
    meant "connected, but with more privilege than it should have".

    Connectivity answers *can we reach it*. Posture answers *should we be
    comfortable*. A failure in the first stops the app working; a failure in
    the second does not, and must not look like it does.
    """
    settings = get_settings()
    connectivity: dict[str, tuple[bool, str]] = {}
    posture: dict[str, tuple[bool, str]] = {}

    try:
        import ollama

        models = {m["model"] for m in ollama.Client(settings.ollama.host).list().get("models", [])}
        ok = settings.chat_model in models
        connectivity["Ollama"] = (
            ok,
            settings.chat_model if ok else f"{settings.chat_model} not pulled",
        )
    except Exception as exc:
        connectivity["Ollama"] = (False, str(exc)[:60])

    try:
        from enterprise_copilot.database.connection import server_info

        info = server_info(settings)
        connectivity["SQL Server"] = (True, f"connected to {info['database']}")
        posture["Read-only principal"] = (
            not info["is_sysadmin"],
            "connected as sysadmin - can write (see ADR-003)"
            if info["is_sysadmin"]
            else "least privilege",
        )
    except Exception as exc:
        connectivity["SQL Server"] = (False, str(exc)[:60])

    try:
        from enterprise_copilot.retrieval.vector_store import QdrantVectorStore

        store = QdrantVectorStore(settings)
        count = store.count()
        store.close()
        connectivity["Document index"] = (count > 0, f"{count} chunks")
    except Exception as exc:
        connectivity["Document index"] = (False, str(exc)[:60])

    posture["TLS certificate"] = (
        not settings.database.trust_server_certificate,
        "TrustServerCertificate=yes (fine locally)"
        if settings.database.trust_server_certificate
        else "verified",
    )

    return connectivity, posture


def sidebar(page_key: str) -> tuple[str, str]:
    """Shared sidebar. Returns the selected user and retrieval strategy.

    `page_key` makes the widget ids unique per page. Without it, two pages
    rendering the same sidebar collide on Streamlit's auto-generated ids.
    """
    settings = get_settings()

    with st.sidebar:
        st.title("Copilot")
        st.caption("Northwind Cloud · everything runs locally")

        st.subheader("Acting as")
        user = st.selectbox(
            "User",
            sorted(USERS),
            index=sorted(USERS).index("admin"),
            key=f"{page_key}_user",
            help="Changes tenant and access groups. Try 'guest' to see permission "
            "filtering refuse the finance-only pricing policy.",
        )
        tenant_code, tenant_id, groups = USERS[user]
        st.caption(f"tenant `{tenant_code}` (id {tenant_id})")
        st.caption("groups: " + ", ".join(groups))

        st.divider()
        st.subheader("Retrieval")
        strategy = st.selectbox(
            "Strategy",
            ["reranked", "hybrid", "dense", "sparse"],
            index=0,
            key=f"{page_key}_strategy",
            help="reranked is most accurate; hybrid is ~45x faster with slightly "
            "lower NDCG (0.925 vs 0.948 on the held-out set).",
        )

        st.divider()
        st.subheader("Profile")
        profile = settings.profile
        st.caption(f"**{settings.profile_name.value}** — {profile.description}")
        st.caption(f"chat: `{settings.chat_model}`")
        st.caption(f"embed: `{settings.embedding_model}`")
        st.caption(f"rerank: `{profile.reranker_model or 'disabled'}`")
        st.caption("Switch profiles with COPILOT_PROFILE in .env, then restart.")

        st.divider()
        connectivity, posture = load_health()

        st.subheader("Connections")
        for name, (ok, detail) in connectivity.items():
            st.markdown(f"{'🟢' if ok else '🔴'} **{name}** — {detail}")

        # Amber, not red: these are "working, but not as locked down as it
        # should be". Red here would be indistinguishable from an outage.
        st.subheader("Security posture")
        for name, (ok, detail) in posture.items():
            st.markdown(f"{'🟢' if ok else '🟡'} **{name}** — {detail}")
        if not all(ok for ok, _ in posture.values()):
            st.caption(
                "Amber means the system is working but has more privilege than it "
                "should. See the System Configuration page."
            )

    return user, strategy


__all__ = ["USERS", "load_copilot", "load_health", "sidebar"]
