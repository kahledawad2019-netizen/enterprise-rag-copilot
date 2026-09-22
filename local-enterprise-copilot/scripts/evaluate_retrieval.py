r"""
Evaluate retrieval across all four strategies.

    .venv\Scripts\python scripts\evaluate_retrieval.py
    .venv\Scripts\python scripts\evaluate_retrieval.py --k 5 --save-baseline
    .venv\Scripts\python scripts\evaluate_retrieval.py --compare-baseline
    .venv\Scripts\python scripts\evaluate_retrieval.py --category exact_code

This is what makes claims about retrieval quality checkable. Every statement in
the README about hybrid or reranking beating the baseline comes from this
output, not from expectation.

Permission cases are run as the user they describe, so "a guest cannot reach
the pricing policy" is tested rather than assumed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "evals" / "document_rag.jsonl"
RESULTS_DIR = ROOT / "evals" / "results"
BASELINE_FILE = ROOT / "evals" / "baseline_retrieval.json"


def load_cases(path: Path, category: str | None) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Evaluation set not found: {path}")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if category:
        cases = [c for c in cases if c.get("category") == category]
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate document retrieval")
    parser.add_argument("--k", type=int, default=8, help="cutoff for @k metrics")
    parser.add_argument("--category", help="run only one category")
    parser.add_argument("--strategies", default="dense,sparse,hybrid,reranked")
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--compare-baseline", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--eval-file", type=Path, default=EVAL_FILE,
        help="evaluation set to run; use evals/document_rag_holdout.jsonl for the held-out set",
    )
    parser.add_argument(
        "--holdout", action="store_true",
        help="shorthand for --eval-file evals/document_rag_holdout.jsonl",
    )
    args = parser.parse_args()

    if args.holdout:
        args.eval_file = ROOT / "evals" / "document_rag_holdout.jsonl"

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)-7s | %(name)s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.evaluation.retrieval_metrics import (
        aggregate,
        compare_strategies,
        evaluate_query,
    )
    from enterprise_copilot.retrieval.hybrid import HybridRetriever, UserContext
    from enterprise_copilot.retrieval.vector_store import RetrievalFilter

    settings = get_settings()
    cases = load_cases(args.eval_file, args.category)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    is_holdout = "holdout" in args.eval_file.name
    print("=" * 92)
    print("  Retrieval evaluation")
    print(f"  set        : {args.eval_file.name}"
          + ("   [HELD OUT - not tuned against]" if is_holdout
             else "   [DEV SET - used during development]"))
    print(f"  cases      : {len(cases)}   k={args.k}")
    print(f"  embedding  : {settings.embedding_model}")
    print(f"  index      : {settings.vector_store.collection} "
          f"({settings.vector_store.index_version})")
    print("=" * 92)

    retriever = HybridRetriever(settings)
    print(f"  reranker   : {retriever.reranker.describe()}")
    print("=" * 92)

    all_metrics = {}
    per_strategy_evals: dict[str, list] = {}

    try:
        for strategy in strategies:
            evaluations = []
            abstain_ok = abstain_total = 0

            for case in cases:
                user = UserContext(
                    user_name="eval",
                    tenant="all",
                    access_groups=case.get("access_groups", ["public"]),
                )
                # Date- and version-sensitive cases need superseded documents
                # visible; everything else uses the production filter.
                filters = RetrievalFilter(
                    access_groups=case.get("access_groups"),
                    current_only=case.get("current_only", True),
                    exclude_doc_types=["security_test"],
                )

                started = time.perf_counter()
                results, _ = retriever.retrieve(
                    case["query"], strategy=strategy, user=user,
                    limit=args.k, filters=filters, expand_parents=False,
                )
                latency_ms = (time.perf_counter() - started) * 1000

                evaluation = evaluate_query(
                    query_id=case["id"], query=case["query"],
                    relevant_docs=case.get("relevant_docs", []),
                    results=results, k=args.k,
                    category=case.get("category", ""),
                    forbidden_docs=case.get("must_not_retrieve", []),
                    latency_ms=latency_ms,
                )
                evaluation.overlap_band = case.get("overlap_band", "")
                evaluations.append(evaluation)

                # An abstention case passes when nothing relevant was found,
                # i.e. the system has no evidence to over-claim from.
                if case.get("should_abstain"):
                    abstain_total += 1
                    if not results or not evaluation.hit:
                        abstain_ok += 1

                if args.verbose:
                    print(f"    {strategy:<9} {evaluation.summary()}")

            metrics = aggregate(
                evaluations, strategy=strategy, k=args.k,
                abstention_correct=abstain_ok, abstention_total=abstain_total,
            )
            all_metrics[strategy] = metrics
            per_strategy_evals[strategy] = evaluations
            print(f"  {strategy:<10} done: {metrics.queries} queries, "
                  f"NDCG@{args.k}={metrics.ndcg_at_k:.3f}")
    finally:
        retriever.close()

    print("\n" + "=" * 92)
    print(compare_strategies(all_metrics))
    print("=" * 92)

    # Per-category NDCG: where each strategy wins is more useful than the mean.
    categories = sorted({c.get("category", "") for c in cases if c.get("category")})
    print(f"\n{'category':<24}" + "".join(f"{s:>12}" for s in strategies))
    print("-" * (24 + 12 * len(strategies)))
    for category in categories:
        row = f"{category:<24}"
        for strategy in strategies:
            value = all_metrics[strategy].per_category.get(category)
            row += f"{value:>12.3f}" if value is not None else f"{'-':>12}"
        print(row)

    # Held-out cases record how much of the question's vocabulary appears in
    # the source passage. Splitting by that band separates genuine retrieval
    # skill from lexical leakage, which flatters BM25 in particular.
    bands = [b for b in ("low", "medium", "high")
             if any(c.get("overlap_band") == b for c in cases)]
    if bands:
        print("\nNDCG by lexical overlap between question and source passage")
        print("  low = least leakage (hardest)   high = most leakage (flatters BM25)")
        print(f"\n{'overlap':<12}{'n':>5}" + "".join(f"{s:>12}" for s in strategies))
        print("-" * (17 + 12 * len(strategies)))
        for band in bands:
            ids = {c["id"] for c in cases if c.get("overlap_band") == band}
            row = f"{band:<12}{len(ids):>5}"
            for strategy in strategies:
                subset = [e for e in per_strategy_evals[strategy]
                          if e.query_id in ids and e.relevant_docs]
                value = sum(e.ndcg_at_k for e in subset) / len(subset) if subset else 0.0
                row += f"{value:>12.3f}"
            print(row)

    print("\nFilter correctness (permission and version leaks):")
    for strategy in strategies:
        metrics = all_metrics[strategy]
        leaks = [e for e in per_strategy_evals[strategy] if not e.filter_correct]
        print(f"  {strategy:<10} {metrics.filter_accuracy:.3f}"
              + (f"  LEAKED: {[e.query_id for e in leaks]}" if leaks else "  (no leaks)"))

    print("\nAbstention (no evidence found for unanswerable questions):")
    for strategy in strategies:
        metrics = all_metrics[strategy]
        if metrics.abstention_total:
            print(f"  {strategy:<10} {metrics.abstention_correct}/{metrics.abstention_total}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at_utc": stamp,
        "k": args.k,
        "embedding_model": settings.embedding_model,
        "index_version": settings.vector_store.index_version,
        "reranker": retriever.reranker.describe(),
        "metrics": {name: metrics.as_row() for name, metrics in all_metrics.items()},
        "per_category": {name: metrics.per_category for name, metrics in all_metrics.items()},
    }
    result_path = RESULTS_DIR / f"retrieval_{stamp}.json"
    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nResults written to {result_path.relative_to(ROOT)}")

    if args.save_baseline:
        BASELINE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Baseline saved to {BASELINE_FILE.relative_to(ROOT)}")

    if args.compare_baseline:
        return _compare_to_baseline(payload, all_metrics)
    return 0


def _compare_to_baseline(payload: dict, all_metrics: dict) -> int:
    """Regression check against the saved baseline.

    This is what makes a change to chunk size, embedding model, reranker or
    prompt safe to make: the effect is measured rather than hoped for.
    """
    if not BASELINE_FILE.exists():
        print("\nNo baseline saved yet. Run with --save-baseline first.")
        return 0

    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    print("\n" + "=" * 92)
    print("  Regression against baseline")
    print(f"  baseline generated {baseline['generated_at_utc']} "
          f"with {baseline['embedding_model']}")
    print("=" * 92)

    regressed = False
    for strategy, metrics in all_metrics.items():
        before = baseline["metrics"].get(strategy)
        if not before:
            print(f"  {strategy:<10} (not in baseline)")
            continue
        delta = metrics.ndcg_at_k - before["ndcg@k"]
        marker = "REGRESSION" if delta < -0.02 else ("improved" if delta > 0.02 else "stable")
        if delta < -0.02:
            regressed = True
        print(f"  {strategy:<10} NDCG {before['ndcg@k']:.3f} -> "
              f"{metrics.ndcg_at_k:.3f}  ({delta:+.3f})  {marker}")

    if regressed:
        print("\nAt least one strategy regressed by more than 0.02 NDCG.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
