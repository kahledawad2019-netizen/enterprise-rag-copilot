"""System Configuration - what is running, with secrets redacted."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
for _candidate in (ROOT / "src", ROOT / "app"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

st.set_page_config(page_title="Configuration", page_icon="::", layout="wide")

from _shared import sidebar  # noqa: E402

from enterprise_copilot.config import PROFILES, get_settings  # noqa: E402


def main() -> None:
    sidebar("config")
    settings = get_settings()

    st.title("System Configuration")
    st.caption(
        "Secrets are never rendered here. The password is a SecretStr and the "
        "connection string is shown masked."
    )

    st.subheader("Active configuration")
    st.json(settings.describe())

    st.subheader("Connection string (masked)")
    st.code(settings.database.safe_odbc_connection_string(), language="text")
    st.caption("The real string is built only inside the connection helper and is never logged.")

    st.subheader("Model profiles")
    import pandas as pd

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "profile": name.value,
                    "chat": profile.chat_model,
                    "embedding": profile.embedding_model,
                    "reranker": profile.reranker_model or "(none)",
                    "context": profile.chat_context_tokens,
                    "approx VRAM": f"{profile.approx_vram_gb} GB",
                    "CPU viable": profile.cpu_only_viable,
                }
                for name, profile in PROFILES.items()
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption("Switch with COPILOT_PROFILE in .env, then restart the app.")

    st.subheader("Security posture")
    try:
        from enterprise_copilot.database.connection import server_info

        info = server_info(settings)
        columns = st.columns(2)
        columns[0].metric("SQL Server", info["edition"].split("(")[0].strip())
        columns[1].metric("Connected as", info["login"])

        if info["is_sysadmin"]:
            st.error(
                "**The connected login is sysadmin.** Defence in depth is incomplete: "
                "only the application SQL guard is protecting the data. A sysadmin "
                "bypasses every database-level DENY. Run `sql/006_create_security.sql` "
                "and switch to the copilot_reader login. See ADR-003.",
                icon="🔴",
            )
        else:
            st.success("Connected as a least-privilege principal.", icon="🟢")

        if info["windows_auth_only"]:
            st.warning(
                "The instance is in Windows-Authentication-only mode, so a read-only "
                "SQL login cannot be created until Mixed Mode is enabled. See ADR-003.",
                icon="⚠️",
            )
    except Exception as exc:
        st.error(f"Could not read the server posture: {exc}")

    st.subheader("Guard policy")
    st.json(
        {
            "allowed_schemas": list(settings.security.allowed_schemas),
            "blocked_columns": list(settings.security.blocked_columns),
            "tenant_isolation": settings.security.enforce_tenant_isolation,
            "tenant_exempt_objects": list(settings.security.tenant_exempt_objects),
            "max_joins": settings.security.max_sql_joins,
            "require_sql_approval": settings.security.require_sql_approval,
            "max_result_rows": settings.database.max_result_rows,
            "query_timeout_seconds": settings.database.query_timeout_seconds,
        }
    )
    st.caption(
        "`ai` and `security` are absent from allowed_schemas on purpose: the "
        "model must not read its own audit trail or the permission tables."
    )

    st.subheader("Versions")
    import importlib.metadata as metadata

    rows = []
    for package in (
        "vanna",
        "qdrant-client",
        "sqlglot",
        "streamlit",
        "pydantic",
        "sentence-transformers",
        "ollama",
        "pyodbc",
    ):
        try:
            rows.append({"package": package, "version": metadata.version(package)})
        except metadata.PackageNotFoundError:
            rows.append({"package": package, "version": "(not installed)"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption(
        "vanna reports __version__ = 0.1.0 while the distribution is 2.0.2. "
        "Always read the distribution version. See ADR-002."
    )


main()
