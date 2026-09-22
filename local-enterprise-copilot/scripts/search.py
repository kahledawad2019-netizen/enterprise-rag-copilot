r"""
Search the document index from the command line.

    .venv\Scripts\python scripts\search.py "what is the refund policy for enterprise annual plans?"
    .venv\Scripts\python scripts\search.py --compare "INC-2025-0042"
    .venv\Scripts\python scripts\search.py --strategy sparse "SLA-ENT-P1"
    .venv\Scripts\python scripts\search.py --user guest "pricing discount ceiling"

`--compare` runs dense, sparse, hybrid and reranked over the same query and
prints them side by side. That comparison is the point: it shows where each
method wins rather than asserting that hybrid is better.
"""

from __future__ import annotations

import argparse
import logging
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

USERS = {
    "admin": ("all", ["public", "internal", "finance", "support", "security", "exec"]),
    "analyst_na": ("NWC-NA", ["public", "internal", "finance"]),
    "analyst_eu": ("NWC-EU", ["public", "internal", "finance"]),
    "support_na": ("NWC-NA", ["public", "internal", "support"]),
    "guest": ("all", ["public"]),
}


def show(results, trace, *, verbose: bool) -> None:
    print(f"\n  {trace.summary()}")
    if trace.notes:
        for note in trace.notes:
            print(f"  note: {note}")
    if not results:
        print("  (no results)")
        return

    for rank, item in enumerate(results, start=1):
        chunk = item.chunk
        print(f"\n  {rank}. [{chunk.doc_id} v{chunk.version}] {chunk.title}")
        print(f"     section : {chunk.section_path or '(top level)'}")
        print(f"     scores  : {item.explain()}")
        print(
            f"     meta    : type={chunk.doc_type} authority={chunk.authority} "
            f"access={chunk.access_group} tenant={chunk.tenant}"
        )
        body = " ".join(chunk.text.split())
        width = 400 if verbose else 200
        print(
            textwrap.fill(
                body[:width] + ("..." if len(body) > width else ""),
                width=88,
                initial_indent="     > ",
                subsequent_indent="       ",
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Search the document index")
    parser.add_argument("query", nargs="+", help="the question")
    parser.add_argument(
        "--strategy", default="reranked", choices=["dense", "sparse", "hybrid", "reranked"]
    )
    parser.add_argument("--compare", action="store_true", help="run all four strategies")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--user", default="admin", choices=sorted(USERS))
    parser.add_argument("--no-parents", action="store_true", help="disable parent expansion")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s | %(name)s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.retrieval.hybrid import HybridRetriever, UserContext

    settings = get_settings()
    query = " ".join(args.query)
    tenant, groups = USERS[args.user]
    user = UserContext(
        user_name=args.user, tenant=tenant, access_groups=groups, is_admin=args.user == "admin"
    )

    retriever = HybridRetriever(settings)
    try:
        print("=" * 88)
        print(f"  Query : {query}")
        print(f"  User  : {args.user} (tenant={tenant}, groups={','.join(groups)})")
        print("=" * 88)

        if args.compare:
            for name, (results, trace) in retriever.compare_strategies(
                query, user=user, limit=args.limit
            ).items():
                print(f"\n{'-' * 88}\n  STRATEGY: {name.upper()}\n{'-' * 88}")
                show(results, trace, verbose=args.verbose)

            print(f"\n{'=' * 88}\n  Overlap between strategies\n{'=' * 88}")
            _print_overlap(retriever, query, user, args.limit)
        else:
            results, trace = retriever.retrieve(
                query,
                strategy=args.strategy,
                user=user,
                limit=args.limit,
                expand_parents=not args.no_parents,
            )
            show(results, trace, verbose=args.verbose)
            if args.verbose:
                print("\n  stage timings:")
                for stage, seconds in trace.stage_seconds.items():
                    print(f"    {stage:18} {seconds * 1000:7.1f} ms")
    finally:
        retriever.close()

    return 0


def _print_overlap(retriever, query, user, limit) -> None:
    outcomes = retriever.compare_strategies(query, user=user, limit=limit)
    sets = {
        name: {item.chunk.chunk_id for item in results} for name, (results, _) in outcomes.items()
    }
    names = list(sets)
    print(f"\n  {'':12}" + "".join(f"{n:>11}" for n in names))
    for left in names:
        row = f"  {left:12}"
        for right in names:
            shared = len(sets[left] & sets[right])
            row += f"{shared:>11}"
        print(row)
    print(f"\n  (cells are the number of shared chunks in the top {limit} of each pair)")


if __name__ == "__main__":
    raise SystemExit(main())
