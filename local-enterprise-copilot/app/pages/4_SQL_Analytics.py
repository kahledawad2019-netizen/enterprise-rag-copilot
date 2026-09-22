"""SQL Analytics - the curated views, and the guard that protects them."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
for _candidate in (ROOT / "src", ROOT / "app"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

st.set_page_config(page_title="SQL Analytics", page_icon="::", layout="wide")

from _shared import (  # noqa: E402
    USERS,
    sidebar,
)

from enterprise_copilot.database.read_only_runner import (  # noqa: E402
    QueryBlockedError,
    QueryExecutionError,
    ReadOnlyRunner,
)
from enterprise_copilot.security.sql_guard import SQLGuard  # noqa: E402

ATTACKS = {
    "Delete every customer": "DELETE FROM core.customers",
    "Batched DROP": "SELECT 1; DROP TABLE core.customers",
    "Comment-hidden DROP": "SELECT 1 /* hide */ ; DROP TABLE core.customers",
    "SELECT INTO (creates a table)": "SELECT * INTO backup FROM core.customers",
    "Read the AI audit trail": "SELECT * FROM ai.audit_events",
    "Read the permission tables": "SELECT * FROM security.app_users",
    "Cross-tenant read": "SELECT customer_name, tenant_id FROM analytics.vw_customer_360 WHERE tenant_id IN (1,2,3)",
    "Blocked column": "SELECT password_hash FROM core.customers",
    "Remote data source": "SELECT * FROM OPENROWSET('SQLNCLI','x','SELECT 1')",
    "Legitimate query (allowed)": "SELECT TOP 5 customer_name, current_arr FROM analytics.vw_customer_360 WHERE tenant_id = 1 ORDER BY current_arr DESC",
}


def main() -> None:
    user_name, _ = sidebar("sql")
    _tenant_code, tenant_id, _groups = USERS[user_name]

    st.title("SQL Analytics")
    st.caption(
        "The guard is layer 1. The read-only database principal is layer 2. "
        "Neither is sufficient alone."
    )

    tab_guard, tab_run, tab_audit = st.tabs(["Safety guard", "Run a query", "Audit trail"])

    with tab_guard:
        st.subheader("Try to get something past the guard")
        st.caption(
            "Queries are parsed with sqlglot into a syntax tree. A comment "
            "cannot hide a node, and an alternative spelling parses the same."
        )

        choice = st.selectbox("Attack", list(ATTACKS))
        sql = st.text_area("SQL", value=ATTACKS[choice], height=110)

        if st.button("Validate", type="primary"):
            result = SQLGuard().validate(sql, tenant_id=tenant_id)
            if result.is_safe:
                st.success("ALLOWED")
                if result.effective_sql != result.sql:
                    st.caption("Rewritten with a row cap before execution:")
                    st.code(result.effective_sql, language="sql")
            else:
                st.error("BLOCKED")
                for violation, detail in result.violations:
                    st.markdown(f"- **{violation.value}** — {detail}")
            for warning in result.warnings:
                st.warning(warning)
            st.json(
                {
                    "tables": result.tables,
                    "schemas": result.schemas,
                    "joins": result.join_count,
                    "uses_analytics_view": result.uses_analytics_view,
                }
            )

    with tab_run:
        st.subheader("Run a read-only query")
        st.caption(
            f"Executed as tenant {tenant_id}. Writes are impossible; the "
            f"guard validates before anything reaches the server."
        )
        sql = st.text_area(
            "T-SQL",
            value="SELECT TOP 10 customer_name, segment, current_arr, sla_breaches\n"
            "FROM analytics.vw_customer_360\n"
            f"WHERE tenant_id = {tenant_id}\n"
            "ORDER BY current_arr DESC",
            height=140,
        )
        if st.button("Execute"):
            try:
                result = ReadOnlyRunner().run(
                    sql,
                    tenant_id=tenant_id,
                    app_user=user_name,
                    question="manual query from the UI",
                )
            except QueryBlockedError as exc:
                st.error(f"Blocked: {exc.result.reason}")
                return
            except QueryExecutionError as exc:
                st.error(str(exc))
                return

            st.success(f"{result.row_count} rows in {result.duration_ms:.0f} ms")
            st.dataframe(result.to_dataframe(), use_container_width=True, hide_index=True)
            for warning in result.warnings:
                st.warning(warning)

    with tab_audit:
        st.subheader("Every attempt is recorded")
        st.caption(
            "Written BEFORE execution, so a query that hangs or crashes still "
            "leaves a record. The AI cannot read this table."
        )
        try:
            from enterprise_copilot.database.connection import raw_connection

            with raw_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT TOP 40 occurred_at_utc, app_user, route, sql_allowed,
                           block_reason, row_count, duration_ms
                    FROM ai.audit_events ORDER BY audit_id DESC
                """)
                rows = [
                    {
                        "when": str(r[0])[:19],
                        "user": r[1],
                        "route": r[2],
                        "allowed": bool(r[3]) if r[3] is not None else None,
                        "blocked_because": (r[4] or "")[:70],
                        "rows": r[5],
                        "ms": r[6],
                    }
                    for r in cursor.fetchall()
                ]
            import pandas as pd

            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f"Audit trail unavailable: {exc}")


main()
