"""
Copilot Chat — the main interface.

Shows the route taken, the SQL generated (always labelled AI-generated), the
rows returned, the answer, and every citation with its document version. The
point is that nothing the model produced is presented without the evidence
behind it being inspectable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
for _candidate in (ROOT / "src", ROOT / "app"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

st.set_page_config(page_title="Copilot Chat", page_icon="::", layout="wide")

from _shared import USERS, load_copilot, sidebar  # noqa: E402

from enterprise_copilot.models.evidence import AnswerStatus, EvidenceType  # noqa: E402
from enterprise_copilot.retrieval.hybrid import UserContext  # noqa: E402

STATUS_STYLE = {
    AnswerStatus.ANSWERED: ("✅", "Answered from evidence"),
    AnswerStatus.PARTIAL: ("⚠️", "Answered, but nothing was cited"),
    AnswerStatus.INSUFFICIENT_EVIDENCE: ("ℹ️", "No relevant evidence found"),
    AnswerStatus.CONFLICTING_SOURCES: ("⚠️", "Sources disagree — see the note"),
    AnswerStatus.REFUSED: ("🚫", "Refused"),
    AnswerStatus.CLARIFICATION_NEEDED: ("❓", "Needs clarification"),
}

ROUTE_HELP = {
    "document_rag": "Answered from policy documents.",
    "text_to_sql": "Answered by querying the database.",
    "multi_source": "Needed both documents and the database.",
    "refuse": "Refused by a rule, before any SQL was generated.",
    "clarify": "Too ambiguous to answer without guessing.",
}


def render_answer(answer, trace) -> None:
    route = trace.routing.route.value if trace.routing else "unknown"

    columns = st.columns([1, 1, 1, 1])
    columns[0].metric("Route", route)
    columns[1].metric("Citations", f"{sum(1 for c in answer.citations if c.is_valid)}"
                                   f"/{len(answer.citations)}")
    columns[2].metric("Grounded", "yes" if answer.is_grounded else "no")
    columns[3].metric("Latency", f"{trace.total_ms / 1000:.1f}s")

    st.caption(ROUTE_HELP.get(route, ""))
    if trace.routing and trace.routing.reason:
        st.caption(f"Router: {trace.routing.reason} "
                   f"(decided by {trace.routing.decided_by})")

    icon, label = STATUS_STYLE.get(answer.status, ("", answer.status.value))
    if answer.status in (AnswerStatus.ANSWERED,):
        st.success(f"{icon} {label}")
    elif answer.status is AnswerStatus.REFUSED:
        st.error(f"{icon} {label}")
    else:
        st.warning(f"{icon} {label}")

    st.markdown(answer.text)

    # --- SQL, always shown and always labelled ---
    if trace.generated_sql:
        with st.expander("AI-generated SQL (not written by a human)", expanded=True):
            st.code(trace.generated_sql, language="sql")
            if trace.sql_validation == "allowed":
                st.success(f"Passed the safety guard · {trace.sql_row_count} rows")
            elif trace.sql_validation == "blocked":
                st.error(f"Blocked by the safety guard: {trace.sql_blocked_reason}")
            else:
                st.warning(f"Guard status: {trace.sql_validation}")

    # --- result table and chart ---
    if answer.evidence:
        sql_evidence = [e for e in answer.evidence.all_evidence
                        if e.evidence_type is EvidenceType.SQL_RESULT and e.structured]
        for evidence in sql_evidence:
            rows = evidence.structured.get("rows") or []
            if not rows:
                continue
            import pandas as pd

            frame = pd.DataFrame(rows)
            st.dataframe(frame, use_container_width=True, hide_index=True)
            _maybe_chart(frame)

    # --- citations ---
    cited = answer.cited_evidence()
    if cited:
        st.subheader("Sources")
        for evidence in cited:
            if evidence.evidence_type is EvidenceType.SQL_RESULT:
                st.markdown(f"**[{evidence.evidence_id}]** Company database")
            else:
                detail = f"**[{evidence.evidence_id}]** {evidence.source_title}"
                if evidence.version:
                    detail += f" · v{evidence.version}"
                if evidence.effective_date:
                    detail += f" · effective {evidence.effective_date}"
                st.markdown(detail)
                if evidence.section:
                    st.caption(evidence.section)

    if answer.evidence and answer.evidence.conflicts:
        with st.expander("Conflicting sources", expanded=True):
            for conflict in answer.evidence.conflicts:
                st.warning(conflict)

    for warning in answer.warnings:
        st.caption(f"⚠️ {warning}")
    for error in trace.errors:
        st.caption(f"🔴 {error}")

    # --- debug ---
    with st.expander("Debug: evidence given to the model"):
        if answer.evidence:
            for evidence in answer.evidence.all_evidence:
                st.markdown(f"**[{evidence.evidence_id}]** {evidence.citation_label()}")
                st.text(evidence.text[:900] + ("..." if len(evidence.text) > 900 else ""))
                st.divider()

    with st.expander("Debug: stage timings"):
        for stage, ms in trace.stage_ms.items():
            st.text(f"{stage:20} {ms:8.0f} ms")
        st.caption(f"trace id: {trace.trace_id}")


def _maybe_chart(frame) -> None:
    """Chart the result when its shape makes one meaningful."""
    numeric = frame.select_dtypes("number").columns.tolist()
    labels = [c for c in frame.columns if c not in numeric]
    if not numeric or not labels or len(frame) < 2 or len(frame) > 60:
        return
    try:
        import plotly.express as express

        figure = express.bar(
            frame.head(25), x=labels[0], y=numeric[0],
            title=f"{numeric[0]} by {labels[0]}",
        )
        figure.update_layout(height=380, margin=dict(l=10, r=10, t=44, b=10))
        st.plotly_chart(figure, use_container_width=True)
    except Exception:
        pass  # a chart is a bonus, never a failure


def main() -> None:
    user_name, strategy = sidebar("chat")
    copilot = load_copilot()

    st.title("Copilot Chat")
    st.caption(f"Acting as **{user_name}** · retrieval **{strategy}**")

    st.session_state.setdefault("messages", [])

    # Clearing on user switch: one tenant's answers must never be shown to
    # another. Cheap, and removes a whole class of leak.
    if st.session_state.get("_last_user") not in (None, user_name):
        st.session_state["messages"] = []
    st.session_state["_last_user"] = user_name

    for message in st.session_state["messages"]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    question = st.chat_input("Ask about a policy, the data, or both...")
    if not question:
        return

    st.session_state["messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    tenant_code, tenant_id, groups = USERS[user_name]
    context = UserContext(
        user_name=user_name, tenant=tenant_code,
        access_groups=groups, is_admin=user_name == "admin",
    )

    with st.chat_message("assistant"):
        with st.status("Working...", expanded=False) as status:
            status.update(label="Routing the question...")
            try:
                answer, trace = copilot.ask(
                    question, user=context, tenant_id=tenant_id, strategy=strategy
                )
            except Exception as exc:
                status.update(label="Failed", state="error")
                st.error(f"{type(exc).__name__}: {exc}")
                return
            status.update(label=f"Done via {trace.routing.route.value}", state="complete")

        render_answer(answer, trace)

    st.session_state["messages"].append({
        "role": "assistant",
        "content": answer.text + (
            f"\n\n*{sum(1 for c in answer.citations if c.is_valid)} sources cited*"
        ),
    })


main()
