"""Retrieval Debugger - see exactly why each chunk was selected."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
for _candidate in (ROOT / "src", ROOT / "app"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

st.set_page_config(page_title="Retrieval Debugger", page_icon="::", layout="wide")

from _shared import sidebar  # noqa: E402

from _shared import USERS, load_copilot  # noqa: E402

from enterprise_copilot.retrieval.hybrid import UserContext  # noqa: E402


def results_frame(results):
    import pandas as pd

    return pd.DataFrame([{
        "rank": rank,
        "doc_id": item.chunk.doc_id,
        "version": item.chunk.version,
        "section": item.chunk.section_path[:60],
        "score": round(item.score, 4),
        "dense": round(item.dense_score, 4) if item.dense_score is not None else None,
        "d_rank": item.dense_rank,
        "sparse": round(item.sparse_score, 4) if item.sparse_score is not None else None,
        "s_rank": item.sparse_rank,
        "rerank": round(item.rerank_score, 4) if item.rerank_score is not None else None,
        "access": item.chunk.access_group,
        "tenant": item.chunk.tenant,
    } for rank, item in enumerate(results, start=1)])


def main() -> None:
    user_name, strategy = sidebar("debug")
    copilot = load_copilot()

    st.title("Retrieval Debugger")
    st.caption(
        "Every stage of retrieval, with the scores that produced the ranking. "
        "This is how retrieval gets tuned rather than guessed at."
    )

    query = st.text_input(
        "Query", value="What is the refund policy for enterprise annual plans?"
    )
    compare = st.checkbox(
        "Compare all four strategies", value=True,
        help="Try 'INC-2025-0042': dense returns the WRONG incident, sparse gets it right.",
    )
    limit = st.slider("Results", 3, 20, 8)

    if not st.button("Run retrieval", type="primary"):
        st.info("Suggested queries: `INC-2025-0042` (exact identifier), "
                "`what stops a salesperson cutting the price too far?` (paraphrase)")
        return

    tenant_code, tenant_id, groups = USERS[user_name]
    context = UserContext(
        user_name=user_name, tenant=tenant_code,
        access_groups=groups, is_admin=user_name == "admin",
    )

    if compare:
        outcomes = copilot.retriever.compare_strategies(query, user=context, limit=limit)
        tabs = st.tabs([name.upper() for name in outcomes])
        for tab, (name, (results, trace)) in zip(tabs, outcomes.items(), strict=True):
            with tab:
                st.caption(trace.summary())
                if results:
                    st.dataframe(results_frame(results), use_container_width=True, hide_index=True)
                else:
                    st.warning("No results.")

        st.subheader("Do the strategies agree?")
        sets = {n: {i.chunk.chunk_id for i in r} for n, (r, _) in outcomes.items()}
        import pandas as pd

        overlap = pd.DataFrame(
            [[len(sets[a] & sets[b]) for b in sets] for a in sets],
            index=list(sets), columns=list(sets),
        )
        st.dataframe(overlap, use_container_width=True)
        st.caption("Shared chunks in the top-k of each pair. Low overlap between "
                   "dense and sparse is exactly why fusion helps.")
        return

    results, trace = copilot.retriever.retrieve(
        query, strategy=strategy, user=context, limit=limit
    )

    st.subheader("Pipeline")
    columns = st.columns(5)
    columns[0].metric("Dense", len(trace.dense_results))
    columns[1].metric("Sparse", len(trace.sparse_results))
    columns[2].metric("Fused", len(trace.fused_results))
    columns[3].metric("Final", len(trace.final_results))
    columns[4].metric("Total", f"{trace.total_seconds * 1000:.0f} ms")

    st.subheader("Filters applied")
    st.json(trace.filters)
    st.caption("Filters run DURING search, not after: filtering afterwards lets "
               "forbidden chunks occupy top-k slots and silently shrinks the result set.")

    st.subheader("Final evidence")
    st.dataframe(results_frame(results), use_container_width=True, hide_index=True)

    with st.expander("Stage timings"):
        for stage, seconds in trace.stage_seconds.items():
            st.text(f"{stage:20} {seconds * 1000:8.1f} ms")
        st.json(trace.reranker)

    with st.expander("Chunk text"):
        for rank, item in enumerate(results, start=1):
            st.markdown(f"**{rank}. [{item.chunk.doc_id} v{item.chunk.version}]** "
                        f"{item.chunk.section_path}")
            st.caption(item.explain())
            st.text(item.chunk.text[:700])
            st.divider()


main()
