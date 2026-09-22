"""
Command-line entry point.

    python ask.py "How many orders were placed last month?"
    python ask.py            # interactive loop
"""

from __future__ import annotations

import sys

from src.security import UnsafeSQLError
from src.settings import get_settings
from src.vanna_agent import build_vanna


def answer(vn, question: str) -> None:
    print(f"\nQ: {question}")
    sql = vn.generate_sql(question, allow_llm_to_see_data=vn.allow_llm_to_see_data)
    print(f"\nSQL:\n{sql}\n")
    try:
        df = vn.run_sql(sql)
    except UnsafeSQLError as exc:
        print(f"BLOCKED by guardrail: {exc}")
        return
    print(df.to_string(max_rows=50))
    print(f"\n({len(df)} rows)")


def main() -> int:
    settings = get_settings()
    print(f"Local RAG over SQL Server | {settings.describe()}")
    vn = build_vanna(settings)

    if len(sys.argv) > 1:
        answer(vn, " ".join(sys.argv[1:]))
        return 0

    print("Type a question, or 'exit' to quit.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if question.lower() in {"exit", "quit", ""}:
            return 0
        try:
            answer(vn, question)
        except Exception as exc:
            print(f"Error: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
