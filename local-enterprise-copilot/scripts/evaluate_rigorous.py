r"""
Statistically rigorous retrieval evaluation.

    .venv\Scripts\python scripts\evaluate_rigorous.py --holdout
    .venv\Scripts\python scripts\evaluate_rigorous.py --eval-file evals\document_rag_holdout_qwen.jsonl
    .venv\Scripts\python scripts\evaluate_rigorous.py --holdout --ablations

The ordinary evaluator reports point estimates. This one reports what those
estimates are actually worth:

* **bootstrap 95 % confidence intervals** on every metric
* **paired bootstrap comparisons** between strategies, preserving the pairing
* **Holm-Bonferroni correction**, because six pairwise tests at alpha = 0.05
  carry a ~26 % chance of at least one false positive
* **win/loss/tie counts**, which say more than a mean when a strategy helps
  some queries and hurts others
* **a power analysis**, so "no significant difference" can be distinguished
  from "this set is too small to tell"

Nothing here changes retrieval. It changes how confidently the results may be
described.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "evals" / "results"

STRATEGIES = ["dense", "sparse", "hybrid", "reranked"]


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Evaluation set not found: {path}")
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def run_strategy(retriever, cases: list[dict], strategy: str, k: int) -> dict[str, list]:
    """Per-query metrics for one strategy. Order matches `cases` for pairing."""
    from enterprise_copilot.evaluation.retrieval_metrics import evaluate_query
    from enterprise_copilot.retrieval.hybrid import UserContext
    from enterprise_copilot.retrieval.vector_store import RetrievalFilter

    ndcg: list[float] = []
    reciprocal: list[float] = []
    recall: list[float] = []
    hits: list[float] = []
    latency: list[float] = []
    per_case: list[dict] = []

    for case in cases:
        user = UserContext(
            user_name="eval",
            tenant="all",
            access_groups=case.get("access_groups", ["public"]),
        )
        filters = RetrievalFilter(
            access_groups=case.get("access_groups"),
            current_only=case.get("current_only", True),
            exclude_doc_types=["security_test"],
        )

        started = time.perf_counter()
        results, _ = retriever.retrieve(
            case["query"],
            strategy=strategy,
            user=user,
            limit=k,
            filters=filters,
            expand_parents=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        evaluation = evaluate_query(
            query_id=case["id"],
            query=case["query"],
            relevant_docs=case.get("relevant_docs", []),
            results=results,
            k=k,
            category=case.get("category", ""),
            forbidden_docs=case.get("must_not_retrieve", []),
            latency_ms=elapsed_ms,
        )

        ndcg.append(evaluation.ndcg_at_k)
        reciprocal.append(evaluation.reciprocal_rank)
        recall.append(evaluation.recall_at_k)
        hits.append(1.0 if evaluation.hit else 0.0)
        latency.append(elapsed_ms)
        per_case.append(
            {
                "id": case["id"],
                "ndcg": evaluation.ndcg_at_k,
                "hit": evaluation.hit,
                "rank": evaluation.first_relevant_rank,
                "overlap_band": case.get("overlap_band", ""),
                "filter_ok": evaluation.filter_correct,
                # The passage this question was generated from. Questions sharing a
                # source are not independent, so the clustered interval resamples
                # by this key rather than by row.
                "source_chunk_id": case.get("source_chunk_id", case["id"]),
            }
        )

    return {
        "ndcg": ndcg,
        "mrr": reciprocal,
        "recall": recall,
        "hit": hits,
        "latency": latency,
        "per_case": per_case,
        "clusters": [c["source_chunk_id"] for c in per_case],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rigorous retrieval evaluation")
    parser.add_argument("--eval-file", type=Path)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--resamples", type=int, default=10000)
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    parser.add_argument(
        "--ablations",
        action="store_true",
        help="also measure the contribution of individual components",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)

    eval_file = args.eval_file
    if eval_file is None:
        eval_file = (
            ROOT
            / "evals"
            / ("document_rag_holdout.jsonl" if args.holdout else "document_rag.jsonl")
        )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.evaluation.statistics import (
        bootstrap_mean,
        clustered_bootstrap_mean,
        holm_bonferroni,
        paired_bootstrap,
        required_sample_size,
        standard_deviation,
    )
    from enterprise_copilot.retrieval.hybrid import HybridRetriever

    settings = get_settings()
    cases = load_cases(eval_file)
    # Only answerable cases can contribute to a ranking metric.
    cases = [c for c in cases if c.get("relevant_docs")]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    print("=" * 96)
    print("  RIGOROUS RETRIEVAL EVALUATION")
    print(f"  set        : {eval_file.name}  ({len(cases)} answerable cases, k={args.k})")
    print(f"  embedding  : {settings.embedding_model}")
    print(f"  bootstrap  : {args.resamples:,} resamples, 95 % percentile intervals")
    print("=" * 96)

    retriever = HybridRetriever(settings)
    print(f"  reranker   : {retriever.reranker.describe()}")

    outcomes: dict[str, dict] = {}
    try:
        for strategy in strategies:
            started = time.perf_counter()
            outcomes[strategy] = run_strategy(retriever, cases, strategy, args.k)
            print(f"  {strategy:10} done in {time.perf_counter() - started:6.1f}s")
    finally:
        retriever.close()

    # ---------------- point estimates with intervals ----------------
    print("\n" + "=" * 96)
    print("  METRICS WITH 95 % BOOTSTRAP CONFIDENCE INTERVALS")
    print("=" * 96)
    header = f"{'strategy':<11}{'NDCG@k':>24}{'MRR':>24}{'recall@k':>24}"
    print(header)
    print("-" * len(header))

    intervals: dict[str, dict] = {}
    for strategy in strategies:
        data = outcomes[strategy]
        row = {
            metric: bootstrap_mean(data[metric], resamples=args.resamples)
            for metric in ("ndcg", "mrr", "recall")
        }
        intervals[strategy] = row
        print(
            f"{strategy:<11}"
            f"{row['ndcg'].format():>24}"
            f"{row['mrr'].format():>24}"
            f"{row['recall'].format():>24}"
        )

    print(f"\n{'strategy':<11}{'mean ms':>10}{'median ms':>12}")
    print("-" * 33)
    for strategy in strategies:
        latencies = sorted(outcomes[strategy]["latency"])
        median = latencies[len(latencies) // 2]
        print(f"{strategy:<11}{sum(latencies) / len(latencies):>10.0f}{median:>12.0f}")

    # ---------------- the same numbers, corrected for correlation --------
    clusters = outcomes[strategies[0]]["clusters"]
    distinct = len(set(clusters))
    clustered: dict[str, object] = {}
    if distinct < len(clusters):
        print()
        print("=" * 96)
        print("  THE SAME NDCG, RESAMPLED BY SOURCE PASSAGE")
        print("=" * 96)
        print(f"  {len(clusters)} questions came from {distinct} distinct passages.")
        print("  Questions sharing a passage are not independent observations, so the")
        print("  interval above is narrower than the evidence supports. Resampling")
        print("  whole passages instead gives the honest width:")
        print()
        print(f"  {'strategy':<11}{'by question':>26}{'by passage':>26}")
        print("  " + "-" * 63)
        for strategy in strategies:
            honest = clustered_bootstrap_mean(
                outcomes[strategy]["ndcg"],
                outcomes[strategy]["clusters"],
                resamples=args.resamples,
            )
            clustered[strategy] = honest
            print(
                f"  {strategy:<11}{intervals[strategy]['ndcg'].format():>26}{honest.format():>26}"
            )
        print()
        print("  Widening is the correct behaviour here, not a regression. Narrowing")
        print("  it again needs more source documents, not more questions per passage.")

    # ---------------- paired comparisons ----------------
    print("\n" + "=" * 96)
    print("  PAIRED COMPARISONS (same queries, pairing preserved)")
    print("=" * 96)

    comparisons = []
    for i, left in enumerate(strategies):
        for right in strategies[i + 1 :]:
            comparisons.append(
                paired_bootstrap(
                    left,
                    outcomes[left]["ndcg"],
                    right,
                    outcomes[right]["ndcg"],
                    resamples=args.resamples,
                )
            )

    corrected = holm_bonferroni(comparisons)
    for comparison, significant in corrected:
        marker = "SIGNIFICANT" if significant else "not significant"
        print(f"  {comparison.format()}")
        print(f"      after Holm-Bonferroni correction: {marker}")

    # ---------------- power ----------------
    print("\n" + "=" * 96)
    print("  STATISTICAL POWER - can this set detect what it claims?")
    print("=" * 96)
    baseline = strategies[0]
    spread = standard_deviation(outcomes[baseline]["ndcg"])
    print(f"  per-query NDCG standard deviation ({baseline}): {spread:.3f}")
    print(f"  queries in this set: {len(cases)}")
    print()
    for effect in (0.02, 0.05, 0.07, 0.10):
        needed = required_sample_size(effect, spread)
        verdict = "detectable" if needed <= len(cases) else "NOT detectable"
        print(f"    to detect +{effect:.2f} NDCG at 80 % power: ~{needed:>5} queries -> {verdict}")

    # ---------------- by leakage band ----------------
    bands = [b for b in ("low", "medium", "high") if any(c.get("overlap_band") == b for c in cases)]
    if bands:
        print("\n" + "=" * 96)
        print("  NDCG BY LEXICAL OVERLAP BAND (with intervals)")
        print("  low overlap = least leakage from the source passage = hardest")
        print("=" * 96)
        print(f"{'band':<9}{'n':>5}" + "".join(f"{s:>24}" for s in strategies))
        print("-" * (14 + 24 * len(strategies)))
        for band in bands:
            indexes = [i for i, c in enumerate(cases) if c.get("overlap_band") == band]
            row = f"{band:<9}{len(indexes):>5}"
            for strategy in strategies:
                subset = [outcomes[strategy]["ndcg"][i] for i in indexes]
                row += f"{bootstrap_mean(subset, resamples=2000).format():>24}"
            print(row)

    # ---------------- ablations ----------------
    if args.ablations:
        run_ablations(settings, cases, args)

    # ---------------- persist ----------------
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at_utc": stamp,
        "eval_file": eval_file.name,
        "cases": len(cases),
        "k": args.k,
        "resamples": args.resamples,
        "embedding_model": settings.embedding_model,
        "intervals": {
            s: {
                m: {"mean": i.mean, "lower": i.lower, "upper": i.upper}
                for m, i in intervals[s].items()
            }
            for s in strategies
        },
        "comparisons": [
            {
                "a": c.name_a,
                "b": c.name_b,
                "difference": c.difference,
                "ci": [c.ci_lower, c.ci_upper],
                "p_value": c.p_value,
                "relative_percent": c.relative_percent,
                "wins": c.wins,
                "losses": c.losses,
                "ties": c.ties,
                "significant_raw": c.is_significant,
                "significant_holm": sig,
            }
            for c, sig in corrected
        ],
        # Persisted so a later run can be compared against this one without
        # re-reading console scrollback. Latency is the whole argument for the
        # adaptive reranking policy, so it belongs in the record, not just in
        # the printout.
        "clustered_intervals": {
            name: {"mean": i.mean, "lower": i.lower, "upper": i.upper, "clusters": i.n}
            for name, i in clustered.items()
        },
        "latency_ms": {
            s: {
                "mean": sum(outcomes[s]["latency"]) / len(outcomes[s]["latency"]),
                "median": sorted(outcomes[s]["latency"])[len(outcomes[s]["latency"]) // 2],
                "p90": sorted(outcomes[s]["latency"])[
                    min(int(len(outcomes[s]["latency"]) * 0.9), len(outcomes[s]["latency"]) - 1)
                ],
                "per_case": outcomes[s]["latency"],
            }
            for s in strategies
        },
        "per_query": {s: outcomes[s]["per_case"] for s in strategies},
    }
    path = RESULTS_DIR / f"rigorous_{eval_file.stem}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWritten to {path.relative_to(ROOT)}")
    return 0


def run_ablations(settings, cases: list[dict], args) -> None:
    """Measure what individual components actually contribute.

    Each ablation changes one thing and re-measures. Without this, a claim like
    "the breadcrumb prefix helps" is an assertion rather than a finding.
    """
    from enterprise_copilot.evaluation.statistics import paired_bootstrap
    from enterprise_copilot.retrieval.hybrid import HybridRetriever

    print("\n" + "=" * 96)
    print("  ABLATIONS - what each component contributes")
    print("=" * 96)

    variants = {
        "baseline (rrf k=60, mmr 0.7)": {},
        "no MMR diversity (lambda=1.0)": {"mmr_lambda": 1.0},
        "strong diversity (lambda=0.3)": {"mmr_lambda": 0.3},
        "rrf k=10 (top ranks dominate)": {"rrf_k": 10},
        "rrf k=200 (flatter fusion)": {"rrf_k": 200},
    }

    results: dict[str, list[float]] = {}
    for label, overrides in variants.items():
        altered = settings.model_copy(deep=True)
        for key, value in overrides.items():
            setattr(altered.retrieval, key, value)

        retriever = HybridRetriever(altered)
        try:
            data = run_strategy(retriever, cases, "hybrid", args.k)
        finally:
            retriever.close()
        results[label] = data["ndcg"]
        mean = sum(data["ndcg"]) / len(data["ndcg"])
        print(f"  {label:<32} NDCG {mean:.4f}")

    baseline_label = "baseline (rrf k=60, mmr 0.7)"
    print("\n  against baseline:")
    for label, values in results.items():
        if label == baseline_label:
            continue
        comparison = paired_bootstrap(
            baseline_label, results[baseline_label], label, values, resamples=4000
        )
        print(
            f"    {label:<32} {comparison.difference:+.4f} "
            f"[{comparison.ci_lower:+.4f}, {comparison.ci_upper:+.4f}] "
            f"p={comparison.p_value:.3f} -> "
            f"{'matters' if comparison.is_significant else 'no measurable effect'}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
