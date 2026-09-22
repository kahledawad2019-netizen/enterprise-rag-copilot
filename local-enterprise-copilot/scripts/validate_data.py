r"""
Run sql/008_validation_queries.sql and fail on any FAIL.

    .venv\Scripts\python scripts\validate_data.py

These are executable assertions about the dataset, not a report. The evaluation
sets assert specific answers ("more than three SLA breaches", "the June 2025
incident"), and those assertions are only meaningful if the underlying data is
known to hold the expected shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "008_validation_queries.sql"


def main() -> int:
    from enterprise_copilot.database.connection import raw_connection

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from setup_database import split_batches

    batches = split_batches(SQL_FILE.read_text(encoding="utf-8"))

    rows: list[tuple] = []
    with raw_connection() as conn:
        cursor = conn.cursor()
        for batch in batches:
            cursor.execute(batch)
            while True:
                if cursor.description is not None:
                    columns = [c[0] for c in cursor.description]
                    if columns[:1] == ["check_name"]:
                        rows.extend(cursor.fetchall())
                    else:
                        cursor.fetchall()
                if not cursor.nextset():
                    break

    if not rows:
        print("No validation rows returned - did 008_validation_queries.sql change shape?")
        return 1

    width = max(len(r[0]) for r in rows) + 2
    print("=" * 78)
    print("  Data validation")
    print("=" * 78)

    groups: dict[str, list[tuple]] = {}
    for row in rows:
        groups.setdefault(row[0].split(".", 1)[0], []).append(row)

    failures = 0
    for group, group_rows in groups.items():
        print(f"\n[{group}]")
        for name, expected, actual, status in group_rows:
            marker = "PASS" if status == "PASS" else "FAIL"
            if status != "PASS":
                failures += 1
            print(f"  {marker}  {name:<{width}} expected {expected:<22} actual {actual}")

    print("\n" + "=" * 78)
    print(f"  {len(rows)} checks | {failures} failed")
    print("=" * 78)

    if failures:
        print("\nRegenerate the dataset, or update the expectations if the change was intended:")
        print("  .venv\\Scripts\\python scripts\\generate_synthetic_data.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
