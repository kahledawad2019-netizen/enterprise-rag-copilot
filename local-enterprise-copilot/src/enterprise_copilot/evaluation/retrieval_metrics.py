"""
Retrieval metrics.

All of these are deterministic: given a ranked list and a set of relevant
documents, the number is the number. No LLM judge is involved, which is
deliberate — retrieval quality must be measurable without a second model whose
own behaviour drifts.

Definitions used here, since these terms are used loosely in the wild:

* **Hit rate @k** — fraction of queries with at least one relevant result in
  the top k. The blunt "did it find anything useful at all" measure.
* **Recall @k** — of all the relevant documents that exist, what share appeared
  in the top k. Answers "did we miss something".
* **Precision @k** — of the k returned, what share were relevant. Answers "how
  much noise did we hand the model".
* **MRR** — mean of 1/rank of the *first* relevant result. Rewards putting the
  right answer first, which matters because the model reads the top results
  most attentively.
* **NDCG @k** — rank-weighted gain, discounted logarithmically. The most
  complete single number: it cares about how many relevant results there are
  *and* where they sit.

Relevance is judged at **document** level, not chunk level. A question about
the refund policy is answered correctly if any chunk of DOC-REF-001 is
retrieved; demanding one specific chunk would make the metric a measure of the
chunker rather than of retrieval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models.documents import ScoredChunk


@dataclass
class QueryEvaluation:
    """Metrics for a single query."""

    query_id: str
    query: str
    category: str = ""
    relevant_docs: list[str] = field(default_factory=list)
    retrieved_docs: list[str] = field(default_factory=list)

    hit: bool = False
    first_relevant_rank: int | None = None
    reciprocal_rank: float = 0.0
    recall_at_k: float = 0.0
    precision_at_k: float = 0.0
    ndcg_at_k: float = 0.0
    latency_ms: float = 0.0

    # Filter correctness: did a forbidden or superseded document leak through?
    forbidden_leaked: list[str] = field(default_factory=list)
    filter_correct: bool = True

    # Set for held-out cases: how much of the question's vocabulary appears in
    # the source passage. Used to separate retrieval skill from lexical leakage.
    overlap_band: str = ""

    def summary(self) -> str:
        rank = self.first_relevant_rank or "-"
        return (
            f"{self.query_id:<8} hit={'Y' if self.hit else 'N'} rank={rank} "
            f"rr={self.reciprocal_rank:.3f} recall={self.recall_at_k:.3f} "
            f"ndcg={self.ndcg_at_k:.3f} {self.latency_ms:6.0f}ms"
        )


@dataclass
class AggregateMetrics:
    """Mean metrics across a query set."""

    strategy: str
    k: int
    queries: int = 0
    hit_rate: float = 0.0
    mrr: float = 0.0
    recall_at_k: float = 0.0
    precision_at_k: float = 0.0
    ndcg_at_k: float = 0.0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    filter_accuracy: float = 1.0
    abstention_correct: int = 0
    abstention_total: int = 0
    per_category: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "k": self.k,
            "queries": self.queries,
            "hit_rate": round(self.hit_rate, 4),
            "mrr": round(self.mrr, 4),
            "recall@k": round(self.recall_at_k, 4),
            "precision@k": round(self.precision_at_k, 4),
            "ndcg@k": round(self.ndcg_at_k, 4),
            "filter_accuracy": round(self.filter_accuracy, 4),
            "mean_ms": round(self.mean_latency_ms, 1),
            "p95_ms": round(self.p95_latency_ms, 1),
        }


# ---------------------------------------------------------------------------
# Single-query metrics
# ---------------------------------------------------------------------------
def evaluate_query(
    *,
    query_id: str,
    query: str,
    relevant_docs: list[str],
    results: list[ScoredChunk],
    k: int,
    category: str = "",
    forbidden_docs: list[str] | None = None,
    latency_ms: float = 0.0,
) -> QueryEvaluation:
    """Score one ranked result list against its ground truth."""
    # Document-level, order preserved, duplicates removed.
    retrieved: list[str] = []
    for item in results[:k]:
        if item.chunk.doc_id not in retrieved:
            retrieved.append(item.chunk.doc_id)

    relevant = set(relevant_docs)
    evaluation = QueryEvaluation(
        query_id=query_id,
        query=query,
        category=category,
        relevant_docs=list(relevant_docs),
        retrieved_docs=retrieved,
        latency_ms=latency_ms,
    )

    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            evaluation.hit = True
            evaluation.first_relevant_rank = rank
            evaluation.reciprocal_rank = 1.0 / rank
            break

    found = len([d for d in retrieved if d in relevant])
    evaluation.recall_at_k = found / len(relevant) if relevant else 0.0
    evaluation.precision_at_k = found / len(retrieved) if retrieved else 0.0
    evaluation.ndcg_at_k = ndcg(retrieved, relevant, k)

    if forbidden_docs:
        leaked = [d for d in retrieved if d in set(forbidden_docs)]
        evaluation.forbidden_leaked = leaked
        evaluation.filter_correct = not leaked

    return evaluation


def ndcg(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Normalised discounted cumulative gain with binary relevance.

    Binary rather than graded: the evaluation set labels documents relevant or
    not, and inventing a 0-3 grading would add subjectivity without adding
    signal at this corpus size.
    """
    if not relevant:
        return 0.0

    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, doc_id in enumerate(retrieved[:k], start=1)
        if doc_id in relevant
    )
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate(
    evaluations: list[QueryEvaluation],
    *,
    strategy: str,
    k: int,
    abstention_correct: int = 0,
    abstention_total: int = 0,
) -> AggregateMetrics:
    if not evaluations:
        return AggregateMetrics(strategy=strategy, k=k)

    # Unanswerable queries have no relevant document, so including them in
    # recall or hit rate would report a failure for correctly finding nothing.
    answerable = [e for e in evaluations if e.relevant_docs]
    count = len(answerable) or 1

    latencies = sorted(e.latency_ms for e in evaluations)
    p95_index = max(0, int(len(latencies) * 0.95) - 1)

    metrics = AggregateMetrics(
        strategy=strategy,
        k=k,
        queries=len(evaluations),
        hit_rate=sum(1 for e in answerable if e.hit) / count,
        mrr=sum(e.reciprocal_rank for e in answerable) / count,
        recall_at_k=sum(e.recall_at_k for e in answerable) / count,
        precision_at_k=sum(e.precision_at_k for e in answerable) / count,
        ndcg_at_k=sum(e.ndcg_at_k for e in answerable) / count,
        mean_latency_ms=sum(latencies) / len(latencies),
        p95_latency_ms=latencies[p95_index],
        filter_accuracy=sum(1 for e in evaluations if e.filter_correct) / len(evaluations),
        abstention_correct=abstention_correct,
        abstention_total=abstention_total,
    )

    by_category: dict[str, list[float]] = {}
    for evaluation in answerable:
        if evaluation.category:
            by_category.setdefault(evaluation.category, []).append(evaluation.ndcg_at_k)
    metrics.per_category = {
        category: sum(values) / len(values) for category, values in sorted(by_category.items())
    }
    return metrics


def compare_strategies(results: dict[str, AggregateMetrics]) -> str:
    """Render a comparison table, with the improvement over the dense baseline.

    The baseline matters: "NDCG 0.82" means nothing on its own, while
    "NDCG 0.82, up 14 percent on dense-only" is a claim that can be checked.
    """
    if not results:
        return "(no results)"

    header = (
        f"{'strategy':<12}{'hit@k':>8}{'MRR':>8}{'recall':>9}"
        f"{'prec':>8}{'NDCG':>8}{'filter':>8}{'mean ms':>10}"
    )
    lines = [header, "-" * len(header)]

    baseline = results.get("dense")
    for name, metrics in results.items():
        lines.append(
            f"{name:<12}{metrics.hit_rate:>8.3f}{metrics.mrr:>8.3f}"
            f"{metrics.recall_at_k:>9.3f}{metrics.precision_at_k:>8.3f}"
            f"{metrics.ndcg_at_k:>8.3f}{metrics.filter_accuracy:>8.3f}"
            f"{metrics.mean_latency_ms:>10.0f}"
        )

    if baseline and baseline.ndcg_at_k > 0:
        lines.append("")
        lines.append("Improvement in NDCG over the dense baseline:")
        for name, metrics in results.items():
            if name == "dense":
                continue
            delta = (metrics.ndcg_at_k - baseline.ndcg_at_k) / baseline.ndcg_at_k * 100
            lines.append(f"  {name:<12}{delta:+7.1f} percent")

    return "\n".join(lines)


__all__ = [
    "AggregateMetrics",
    "QueryEvaluation",
    "aggregate",
    "compare_strategies",
    "evaluate_query",
    "ndcg",
]
