"""
Local Enterprise Intelligence Copilot — Streamlit entry point.

    .venv\\Scripts\\streamlit run app\\streamlit_app.py

Design rules this app follows:

* **Domain logic lives in `src/`, never here.** These pages call `Copilot.ask`
  and render the result. Nothing in `app/` decides what is safe, what is
  relevant, or what to retrieve.
* **Clients are cached as resources, results are not.** Loading the reranker
  takes ~6 s, so it is cached for the process. Answers are never cached,
  because caching one tenant's results and serving them to another is a data
  leak wearing a performance costume.
* **Shared helpers live in `_shared.py`.** A page module that both renders
  itself and exports helpers re-renders on every import, which produced
  duplicate widget ids and a broken sidebar.
* **No secrets in session state.** Session state is client-visible in
  Streamlit. Configuration is read from `Settings` on each render and shown
  through the masking helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT / "src", ROOT / "app"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

st.set_page_config(
    page_title="Enterprise Intelligence Copilot",
    page_icon="::",
    layout="wide",
    initial_sidebar_state="expanded",
)

from _shared import sidebar  # noqa: E402

from enterprise_copilot.config import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    sidebar("home")

    st.title("Local Enterprise Intelligence Copilot")
    st.caption(
        "Document RAG + Text-to-SQL over SQL Server, answered by a local model. "
        "No data leaves this machine."
    )

    left, right = st.columns([3, 2])

    with left:
        st.subheader("What this is")
        st.markdown(
            """
Ask questions about **Northwind Cloud**, a fictional B2B SaaS company, and get
answers drawn from two very different sources:

- **19 policy documents** — refund policy, SLA, pricing, incident postmortems
- **~287,000 database rows** — customers, subscriptions, invoices, tickets, incidents

A router decides which source a question needs — or both — before anything is
retrieved. Every factual claim is cited, and every citation is checked against
the evidence actually supplied.
            """
        )

        st.subheader("Try these")
        examples = {
            "Policy (documents)": "What is the refund policy for enterprise annual plans?",
            "Data (SQL)": "Which five customers have the highest ARR?",
            "Both (multi-source)": (
                "Show customers with more than three SLA breaches and summarise the SLA policy"
            ),
            "Refused (destructive)": "Delete all customers from the database",
            "Clarify (ambiguous)": "What is the response time?",
            "Exact identifier": "What happened in INC-2025-0042?",
        }
        for label, question in examples.items():
            st.markdown(f"**{label}** — `{question}`")

        st.info("Open **Copilot Chat** in the sidebar to ask a question.", icon="💬")

    with right:
        st.subheader("Architecture")
        st.code(
            """question
  |
  +- router          rules first, then the model
  |                  destructive -> refused before any SQL
  |
  +- documents       dense + BM25 -> RRF -> rerank -> MMR
  |                  tenant / access / version filters
  |
  +- database        schema subset + glossary + examples
  |                  -> T-SQL -> sqlglot guard -> read-only run
  |
  +- evidence        [D1..] documents  ·  [S1..] data
  |
  +- answer          local llama3.1, citations validated""",
            language="text",
        )

        st.subheader("Measured quality")
        st.caption("96 held-out questions the author did not write")
        st.markdown(
            """
| strategy | NDCG@8 | vs dense |
|---|---|---|
| dense | 0.856 | — |
| sparse | 0.877 | +2.5% |
| hybrid | 0.925 | +8.0% |
| **reranked** | **0.948** | **+10.7%** |
            """
        )
        st.caption("See `docs/evaluation.md` for the bias analysis.")

    st.divider()
    columns = st.columns(4)
    metrics = [
        ("Documents", "19", "7,160 words"),
        ("Chunks indexed", "130", f"dim 1024, {settings.vector_store.index_version}"),
        ("Database rows", "~287k", "25 tables, 6 views"),
        ("Tests", "261", "all passing"),
    ]
    for column, (label, value, caption) in zip(columns, metrics, strict=True):
        with column:
            st.metric(label, value)
            st.caption(caption)


main()
