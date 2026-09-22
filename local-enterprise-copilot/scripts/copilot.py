r"""
The full copilot: routing, documents, SQL, multi-source, citations.

    .venv\Scripts\python scripts\copilot.py "what is the refund policy for annual plans?"
    .venv\Scripts\python scripts\copilot.py "which five customers have the highest ARR?"
    .venv\Scripts\python scripts\copilot.py "show customers with more than three SLA breaches and summarise the SLA policy"
    .venv\Scripts\python scripts\copilot.py --interactive
    .venv\Scripts\python scripts\copilot.py --trace "why did churn rise in Q2 2025?"
"""

from __future__ import annotations

import argparse
import logging
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

USERS = {
    "admin": ("all", 1, ["public", "internal", "finance", "support", "security", "exec"]),
    "analyst_na": ("NWC-NA", 1, ["public", "internal", "finance"]),
    "analyst_eu": ("NWC-EU", 2, ["public", "internal", "finance"]),
    "support_na": ("NWC-NA", 1, ["public", "internal", "support"]),
    "guest": ("all", 1, ["public"]),
}


def wrap(text: str, indent: str = "  ") -> str:
    out = []
    for line in text.split("\n"):
        out.append(
            textwrap.fill(line, width=86, initial_indent=indent, subsequent_indent=indent)
            if line.strip()
            else ""
        )
    return "\n".join(out)


def render(answer, trace, *, show_trace: bool) -> None:
    from enterprise_copilot.generation.citations import format_sources

    route = trace.routing
    print(
        f"\n  route     : {route.route.value}  (decided by {route.decided_by}, "
        f"confidence {route.confidence:.2f})"
    )
    print(f"  reason    : {route.reason}")
    if route.identifiers:
        print(f"  preserved : {', '.join(route.identifiers)}")
    if route.rewritten_query and route.rewritten_query != route.original_query:
        print(f"  rewritten : {route.rewritten_query}")

    if trace.generated_sql:
        print("\n  --- AI-GENERATED SQL (not written by a human) ---")
        for line in trace.generated_sql.split("\n"):
            print(f"    {line}")
        print(
            f"  guard: {trace.sql_validation}"
            + (f" - {trace.sql_blocked_reason}" if trace.sql_blocked_reason else "")
        )
        if trace.sql_row_count is not None:
            print(f"  rows : {trace.sql_row_count}")

    print("\n" + "-" * 90)
    print("  ANSWER")
    print("-" * 90 + "\n")
    print(wrap(answer.text))

    print("\n" + "-" * 90)
    print(f"  {format_sources(answer)}")
    print("-" * 90)
    print(f"  status    : {answer.status.value}")
    print(f"  grounded  : {answer.is_grounded}")
    print(
        f"  citations : {len(answer.citations)} "
        f"({sum(1 for c in answer.citations if c.is_valid)} valid)"
    )
    print(f"  latency   : {trace.total_ms:.0f} ms total")
    if answer.warnings:
        for warning in answer.warnings:
            print(f"  warning   : {warning}")
    if trace.errors:
        for error in trace.errors:
            print(f"  error     : {error}")

    if show_trace:
        print("\n  --- TRACE ---")
        for stage, ms in trace.stage_ms.items():
            print(f"    {stage:18} {ms:8.0f} ms")
        print(f"    {'trace_id':18} {trace.trace_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Enterprise Intelligence Copilot")
    parser.add_argument("question", nargs="*")
    parser.add_argument("--user", default="admin", choices=sorted(USERS))
    parser.add_argument(
        "--strategy", default="reranked", choices=["dense", "sparse", "hybrid", "reranked"]
    )
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--trace", action="store_true", help="show stage timings")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s | %(name)s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.retrieval.hybrid import UserContext
    from enterprise_copilot.routing.orchestrator import Copilot

    settings = get_settings()
    tenant_code, tenant_id, groups = USERS[args.user]
    user = UserContext(
        user_name=args.user,
        tenant=tenant_code,
        access_groups=groups,
        is_admin=args.user == "admin",
    )

    copilot = Copilot(settings)
    try:
        if args.interactive:
            print("=" * 90)
            print(f"  Local Enterprise Intelligence Copilot | {settings.chat_model}")
            print(f"  user: {args.user} (tenant {tenant_id})   type 'exit' to quit")
            print("=" * 90)
            while True:
                try:
                    question = input("\n> ").strip()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if question.lower() in {"exit", "quit", ""}:
                    return 0
                answer, trace = copilot.ask(
                    question, user=user, tenant_id=tenant_id, strategy=args.strategy
                )
                render(answer, trace, show_trace=args.trace)
            return 0

        if not args.question:
            parser.error("a question is required unless --interactive is used")

        question = " ".join(args.question)
        print("=" * 90)
        print(f"  Q    : {question}")
        print(f"  user : {args.user} (tenant {tenant_id}, groups {','.join(groups)})")
        print("=" * 90)

        answer, trace = copilot.ask(
            question, user=user, tenant_id=tenant_id, strategy=args.strategy
        )
        render(answer, trace, show_trace=args.trace)
    finally:
        copilot.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
