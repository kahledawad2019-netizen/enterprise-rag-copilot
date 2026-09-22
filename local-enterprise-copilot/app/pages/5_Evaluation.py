"""Evaluation - measured quality, and the bias in the measurement."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
for _candidate in (ROOT / "src", ROOT / "app"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

st.set_page_config(page_title="Evaluation", page_icon="::", layout="wide")

from _shared import sidebar  # noqa: E402

import json  # noqa: E402


@st.cache_data
def load_results() -> list[dict]:
    directory = ROOT / "evals" / "results"
    if not directory.exists():
        return []
    payloads = []
    for path in sorted(directory.glob("retrieval_*.json")):
        try:
            payloads.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return payloads


def main() -> None:
    sidebar("eval")
    st.title("Evaluation")
    st.caption("Every quality claim in this project comes from here.")

    st.subheader("Two sets, used for different things")
    st.markdown("""
| Set | Cases | Written by | Purpose |
|---|---|---|---|
| `document_rag.jsonl` | 42 | the author | **dev set** - regression guard, adversarial cases |
| `document_rag_holdout.jsonl` | 96 | llama3.1:8b | **test set** - generalisation, never tuned against |

The dev set was written by the same person who wrote the documents and the
chunker. That makes it useful for catching regressions and worthless as
evidence of generalisation, which is why the held-out set exists.
    """)

    st.subheader("Held-out results (96 cases, k=8)")
    st.markdown("""
| strategy | hit@k | MRR | recall | NDCG | mean ms |
|---|---|---|---|---|---|
| dense | 0.990 | 0.811 | 0.990 | 0.856 | 121 |
| sparse | 0.990 | 0.839 | 0.990 | 0.877 | **7** |
| hybrid | 0.979 | 0.906 | 0.979 | 0.925 | 65 |
| **reranked** | **1.000** | **0.930** | **1.000** | **0.948** | 2904 |
    """)

    st.subheader("The most informative result")
    st.caption("NDCG split by how much of the question's vocabulary appears in the "
               "source passage. Low overlap = least leakage = hardest.")
    st.markdown("""
| overlap | n | dense | sparse | hybrid | reranked |
|---|---|---|---|---|---|
| **low** (hardest) | 33 | 0.721 | 0.778 | 0.845 | **0.874** |
| medium | 45 | 0.927 | 0.918 | 0.981 | 0.981 |
| high (leakiest) | 18 | 0.927 | 0.959 | 0.931 | **1.000** |
    """)
    st.info(
        "Hybrid beats dense by **+17.2%** on the hardest cases and only **+0.4%** on "
        "the easiest. An evaluation artifact would show the opposite pattern. "
        "Sparse swings +23% from low to high overlap, confirming that lexical "
        "leakage flatters BM25 - which is why the breakdown is reported, not the mean.",
        icon="📊",
    )

    st.subheader("Honest limitations")
    st.markdown("""
- The author still wrote the **documents**; only the questions are independent.
- Ground truth is the source document, so a question legitimately answerable
  elsewhere counts as a miss. These numbers are a **lower bound**.
- The held-out set contains **only answerable questions** - it says nothing
  about abstention, permissions or injection resistance. The dev set covers those.
- 96 cases is small. Differences under ~2 points are not meaningful.
    """)

    results = load_results()
    if results:
        st.subheader("Recorded runs")
        import pandas as pd

        rows = []
        for payload in results:
            for name, metrics in payload["metrics"].items():
                rows.append({
                    "run": payload["generated_at_utc"],
                    "strategy": name,
                    "ndcg": metrics["ndcg@k"],
                    "mrr": metrics["mrr"],
                    "recall": metrics["recall@k"],
                    "filter_acc": metrics["filter_accuracy"],
                    "mean_ms": metrics["mean_ms"],
                })
        frame = pd.DataFrame(rows)
        st.dataframe(frame, use_container_width=True, hide_index=True)

        try:
            import plotly.express as express

            figure = express.line(
                frame, x="run", y="ndcg", color="strategy", markers=True,
                title="NDCG@8 across runs",
            )
            st.plotly_chart(figure, use_container_width=True)
        except Exception:
            pass

    st.subheader("Reproduce it")
    st.code(
        "# regenerate the held-out set (deterministic given COPILOT_SEED)\n"
        ".venv\\Scripts\\python scripts\\generate_eval_set.py\n\n"
        "# held-out - the number to quote\n"
        ".venv\\Scripts\\python scripts\\evaluate_retrieval.py --holdout --k 8\n\n"
        "# dev set - regression guard, non-zero exit on regression\n"
        ".venv\\Scripts\\python scripts\\evaluate_retrieval.py --k 8 --compare-baseline",
        language="powershell",
    )


main()
