r"""
Run the version-controlled SQL scripts in order.

    .venv\Scripts\python scripts\setup_database.py            # run all
    .venv\Scripts\python scripts\setup_database.py --only 005 # run one
    .venv\Scripts\python scripts\setup_database.py --validate # 008 only, report

The scripts are idempotent, so re-running is safe.

Why this exists rather than "paste it into SSMS": the scripts must be runnable
from CI and from a fresh clone without a human clicking through SSMS. SSMS
remains fully supported -- see docs/database.md for the manual order.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# A GO batch separator: the word GO alone on a line, optionally with a repeat
# count. It is a client directive, not T-SQL, so pyodbc never sees it.
GO_PATTERN = re.compile(r"^\s*GO\s*(?:\d+)?\s*$", re.IGNORECASE | re.MULTILINE)


class ScriptError(RuntimeError):
    """A SQL batch failed. Carries enough context to find the offending batch."""

    def __init__(self, script: str, batch_index: int, snippet: str, original: Exception):
        super().__init__(f"{script} batch {batch_index}: {original}")
        self.script = script
        self.batch_index = batch_index
        self.snippet = snippet
        self.original = original


def split_batches(sql_text: str) -> list[str]:
    """Split a script on GO separators, dropping empty batches."""
    return [b.strip() for b in GO_PATTERN.split(sql_text) if b.strip()]


def run_script(cursor, path: Path, *, echo_prints: bool = True) -> int:
    """Execute one .sql file batch by batch. Returns the number of batches run."""
    sql_text = path.read_text(encoding="utf-8")
    batches = split_batches(sql_text)

    for index, batch in enumerate(batches, start=1):
        try:
            cursor.execute(batch)
            # Drain result sets so PRINT output and rowcounts do not leak into
            # the next batch.
            while True:
                if cursor.description is not None:
                    cursor.fetchall()
                if not cursor.nextset():
                    break
        except Exception as exc:
            snippet = "\n".join(batch.splitlines()[:6])
            raise ScriptError(path.name, index, snippet, exc) from exc

    if echo_prints:
        for message in getattr(cursor.connection, "messages", []) or []:
            print(f"      {message}")
    return len(batches)


def connect(settings, *, database: str):
    import pyodbc

    conn = pyodbc.connect(
        settings.database.odbc_connection_string(database=database),
        timeout=settings.database.connect_timeout,
        autocommit=True,  # DDL such as CREATE DATABASE cannot run in a transaction
    )
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply SQL Server migration scripts")
    parser.add_argument(
        "--only", help="run just the script whose name starts with this prefix, e.g. 005"
    )
    parser.add_argument(
        "--validate", action="store_true", help="run 008_validation_queries.sql only"
    )
    parser.add_argument("--from", dest="start_from", help="start at this prefix and continue")
    args = parser.parse_args()

    from enterprise_copilot.config import get_settings

    settings = get_settings()

    scripts = sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if not scripts:
        print(f"No SQL scripts found in {SQL_DIR}")
        return 1

    if args.validate:
        scripts = [s for s in scripts if s.name.startswith("008")]
    elif args.only:
        scripts = [s for s in scripts if s.name.startswith(args.only)]
        if not scripts:
            print(f"No script matches prefix {args.only!r}")
            return 1
    elif args.start_from:
        scripts = [s for s in scripts if s.name >= args.start_from]

    print("=" * 78)
    print(f"  Applying {len(scripts)} script(s) to {settings.database.server}")
    print(f"  Target database: {settings.database.database}")
    print("=" * 78)

    failures = 0
    for script in scripts:
        # 001 creates the database, so it must run against master.
        target = "master" if script.name.startswith("001") else settings.database.database
        started = time.perf_counter()
        try:
            with connect(settings, database=target) as conn:
                cursor = conn.cursor()
                batches = run_script(cursor, script)
            elapsed = time.perf_counter() - started
            print(f"[ OK ] {script.name:32} {batches:3} batches  {elapsed:6.2f}s")
        except ScriptError as exc:
            failures += 1
            print(f"[FAIL] {script.name:32} batch {exc.batch_index}")
            print(f"       {exc.original}")
            print("       ---- batch started with ----")
            for line in exc.snippet.splitlines():
                print(f"       {line}")
            break  # later scripts depend on earlier ones
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {script.name:32} {type(exc).__name__}: {exc}")
            break

    print("=" * 78)
    if failures:
        print("  Setup FAILED. Fix the error above and re-run; scripts are idempotent.")
        return 1
    print("  Setup complete.")
    print("  Next: .venv\\Scripts\\python scripts\\generate_synthetic_data.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
