"""Traces - what actually happened, stage by stage."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
for _candidate in (ROOT / "src", ROOT / "app"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

st.set_page_config(page_title="Traces", page_icon="::", layout="wide")

from _shared import sidebar  # noqa: E402

import json  # noqa: E402


def load_traces(limit: int = 60) -> list[dict]:
    from enterprise_copilot.config import get_settings

    directory = get_settings().observability.trace_dir
    if not directory.exists():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("traces_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    return records[-limit:][::-1]


def main() -> None:
    sidebar("traces")
    st.title("Traces")
    st.caption("Every request writes a trace. Credentials are stripped and personal "
               "data masked before anything is written.")

    records = load_traces()
    if not records:
        st.info("No traces yet. Ask a question in Copilot Chat.")
        return

    import pandas as pd

    summary = pd.DataFrame([{
        "trace_id": r["trace_id"],
        "when": r.get("recorded_at_utc", "")[:19],
        "route": r.get("route"),
        "status": r.get("answer_status"),
        "grounded": r.get("grounded"),
        "citations": r.get("citation_count"),
        "rows": r.get("sql_row_count"),
        "total_ms": r.get("total_ms"),
    } for r in records])

    columns = st.columns(4)
    columns[0].metric("Traces", len(records))
    columns[1].metric("Median ms", f'{summary["total_ms"].median():.0f}')
    grounded = summary["grounded"].fillna(False)
    columns[2].metric("Grounded", f"{100 * grounded.mean():.0f}%")
    columns[3].metric("Routes", summary["route"].nunique())

    st.subheader("Recent requests")
    st.dataframe(summary, use_container_width=True, hide_index=True)

    try:
        import plotly.express as express

        figure = express.box(summary.dropna(subset=["route"]), x="route", y="total_ms",
                             title="Latency by route")
        st.plotly_chart(figure, use_container_width=True)
    except Exception:
        pass

    st.subheader("Inspect one trace")
    chosen = st.selectbox("Trace", summary["trace_id"].tolist())
    record = next(r for r in records if r["trace_id"] == chosen)

    st.json({k: v for k, v in record.items() if k != "spans"})

    st.subheader("Spans")
    spans = pd.DataFrame([{
        "name": s["name"], "ms": s["duration_ms"], "status": s["status"],
        "error": s.get("error"),
    } for s in record["spans"]])
    st.dataframe(spans, use_container_width=True, hide_index=True)

    try:
        import plotly.express as express

        figure = express.bar(spans, x="ms", y="name", orientation="h",
                             title="Where the time went")
        figure.update_layout(height=300)
        st.plotly_chart(figure, use_container_width=True)
    except Exception:
        pass

    for span in record["spans"]:
        with st.expander(f'{span["name"]} — {span["duration_ms"]:.0f} ms'):
            st.json(span["attributes"])


main()
