r"""
Ask a data question. Natural language in, validated SQL and an answer out.

    .venv\Scripts\python scripts\query.py "which five customers have the highest ARR?"
    .venv\Scripts\python scripts\query.py --provider native "calculate MRR"
    .venv\Scripts\python scripts\query.py --compare "how many SLA breaches per customer?"
    .venv\Scripts\python scripts\query.py --tenant 2 "top customers by ARR"

The generated SQL is always shown and always labelled as AI-generated, because
a number whose query you cannot see is a number you cannot check.
"""

from __future__ import annotations

import argparse
import logging
import sys
import textwrap
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def run_provider(provider, request, *, show_context: bool, approve: bool) -> int:
    from enterprise_copilot.database.read_only_runner import (
        QueryBlockedError,
        QueryExecutionError,
    )

    print(f"\n{'=' * 88}")
    print(f"  PROVIDER: {provider.name}")
    print("=" * 88)

    generated = provider.generate_query(request)

    if show_context and generated.context:
        print("\n  schema offered   :", ", ".join(generated.context.table_names()))
        print(
            "  glossary terms   :",
            ", ".join(g["term"] for g in generated.context.glossary) or "(none)",
        )
        print("  approved examples:", len(generated.context.examples))

    print(f"\n  generation: {generated.generation_ms:.0f} ms")
    print("\n  --- AI-GENERATED SQL (not written by a human) ---")
    for line in generated.sql.split("\n"):
        print(f"    {line}")

    if generated.is_empty:
        print("\n  [FAIL] no SQL was produced")
        for warning in generated.warnings:
            print(f"    - {warning}")
        return 1

    validation = provider.validate_query(generated.sql, tenant_id=request.tenant_id)
    print(f"\n  guard: {'ALLOWED' if validation.is_safe else 'BLOCKED'}")
    if validation.warnings:
        for warning in validation.warnings:
            print(f"    warning: {warning}")
    if not validation.is_safe:
        print(f"    reason: {validation.reason}")
        print("\n  The query was refused before touching the database.")
        return 1

    if validation.effective_sql != generated.sql:
        print(f"  rewritten for safety: {validation.effective_sql}")

    try:
        result = provider.execute_query(generated.sql, request=request, approved=approve)
    except QueryBlockedError as exc:
        print(f"\n  [BLOCKED] {exc.result.reason}")
        return 1
    except QueryExecutionError as exc:
        print(f"\n  [FAIL] {exc}")
        return 1

    print(f"\n  --- RESULT ({result.row_count} rows in {result.duration_ms:.0f} ms) ---")
    for line in result.preview(limit=10).split("\n"):
        print(f"    {line}")
    if result.warnings:
        for warning in result.warnings:
            print(f"    warning: {warning}")

    print("\n  --- ANSWER ---")
    explanation = provider.explain_result(request.question, result)
    for line in explanation.split("\n"):
        print(
            textwrap.fill(line, width=84, initial_indent="    ", subsequent_indent="    ")
            if line.strip()
            else ""
        )

    print(f"\n  trace: {provider.get_trace_metadata()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask a data question")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--provider", default="vanna", choices=["vanna", "native"])
    parser.add_argument("--compare", action="store_true", help="run both providers")
    parser.add_argument("--tenant", type=int, default=1, help="tenant_id to restrict to")
    parser.add_argument("--user", default="analyst_na")
    parser.add_argument("--show-context", action="store_true")
    parser.add_argument("--approve", action="store_true", help="approve execution if required")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s | %(name)s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.text_to_sql.provider import SQLRequest, build_provider

    settings = get_settings()
    question = " ".join(args.question)
    request = SQLRequest(
        question=question,
        tenant_id=args.tenant,
        app_user=args.user,
        trace_id=uuid.uuid4().hex[:12],
    )

    print("=" * 88)
    print(f"  Q      : {question}")
    print(f"  user   : {args.user} (tenant {args.tenant})")
    print(f"  model  : {settings.chat_model}   trace: {request.trace_id}")
    print("=" * 88)

    names = ["vanna", "native"] if args.compare else [args.provider]
    exit_code = 0
    for name in names:
        try:
            provider = build_provider(settings, name=name)
        except Exception as exc:
            print(f"\n  [SKIP] provider {name!r} unavailable: {exc}")
            continue
        exit_code |= run_provider(
            provider, request, show_context=args.show_context, approve=args.approve
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
