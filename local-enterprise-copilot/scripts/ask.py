r"""
Ask a document question and get a cited answer.

    .venv\Scripts\python scripts\ask.py "what is the refund policy for enterprise annual plans?"
    .venv\Scripts\python scripts\ask.py --user guest "what is the maximum discount?"
    .venv\Scripts\python scripts\ask.py --show-evidence "why did churn rise in Q2 2025?"

Document RAG only. Text-to-SQL and routing arrive in later phases.
"""

from __future__ import annotations

import argparse
import logging
import sys
import textwrap
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

USERS = {
    "admin":      ("all",    ["public", "internal", "finance", "support", "security", "exec"]),
    "analyst_na": ("NWC-NA", ["public", "internal", "finance"]),
    "analyst_eu": ("NWC-EU", ["public", "internal", "finance"]),
    "support_na": ("NWC-NA", ["public", "internal", "support"]),
    "guest":      ("all",    ["public"]),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask a question over company documents")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--user", default="admin", choices=sorted(USERS))
    parser.add_argument("--strategy", default="reranked",
                        choices=["dense", "sparse", "hybrid", "reranked"])
    parser.add_argument("--limit", type=int, default=None, help="evidence chunks (default: profile)")
    parser.add_argument("--show-evidence", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s | %(name)s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.generation.answerer import Answerer, GenerationError
    from enterprise_copilot.generation.citations import format_sources
    from enterprise_copilot.retrieval.hybrid import HybridRetriever, UserContext

    settings = get_settings()
    question = " ".join(args.question)
    tenant, groups = USERS[args.user]
    user = UserContext(user_name=args.user, tenant=tenant, access_groups=groups,
                       is_admin=args.user == "admin")
    trace_id = uuid.uuid4().hex[:12]

    print("=" * 88)
    print(f"  Q     : {question}")
    print(f"  user  : {args.user} (tenant={tenant}, groups={','.join(groups)})")
    print(f"  model : {settings.chat_model}   trace: {trace_id}")
    print("=" * 88)

    retriever = HybridRetriever(settings)
    try:
        started = time.perf_counter()
        results, trace = retriever.retrieve(
            question, strategy=args.strategy, user=user, limit=args.limit
        )
        retrieval_ms = (time.perf_counter() - started) * 1000
        print(f"\n  retrieval: {trace.summary()}")

        answerer = Answerer(settings)
        package = answerer.build_package(question, results)

        if package.conflicts:
            print("\n  conflicts detected:")
            for conflict in package.conflicts:
                print(f"    - {conflict}")

        if args.show_evidence:
            print("\n" + "-" * 88)
            print("  EVIDENCE GIVEN TO THE MODEL")
            print("-" * 88)
            for item in package.all_evidence:
                print(f"\n  {item.short_reference()}")
                body = " ".join(item.text.split())
                print(textwrap.fill(body[:320] + ("..." if len(body) > 320 else ""),
                                    width=86, initial_indent="    ", subsequent_indent="    "))

        print("\n" + "-" * 88)
        print("  ANSWER")
        print("-" * 88)
        try:
            answer = answerer.answer(package, trace_id=trace_id)
        except GenerationError as exc:
            print(f"\n  [FAIL] {exc}")
            return 1

        print()
        for line in answer.text.split("\n"):
            print(textwrap.fill(line, width=86, initial_indent="  ", subsequent_indent="  ")
                  if line.strip() else "")

        print("\n" + "-" * 88)
        print(f"  {format_sources(answer)}")
        print("-" * 88)
        print(f"  status     : {answer.status.value}")
        print(f"  grounded   : {answer.is_grounded}")
        print(f"  citations  : {len(answer.citations)} "
              f"({sum(1 for c in answer.citations if c.is_valid)} valid)")
        print(f"  latency    : retrieval {retrieval_ms:.0f} ms + "
              f"generation {answer.latency_ms:.0f} ms")
        if answer.tokens_in:
            print(f"  tokens     : {answer.tokens_in} in / {answer.tokens_out} out")
        if answer.warnings:
            print("  warnings   :")
            for warning in answer.warnings:
                print(f"    - {warning}")
    finally:
        retriever.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
