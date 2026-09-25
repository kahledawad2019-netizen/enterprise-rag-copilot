r"""
Build the embedded DuckDB analytics database.

    .venv\Scripts\python scripts\build_duckdb.py            # build if missing
    .venv\Scripts\python scripts\build_duckdb.py --rebuild  # always rebuild
    .venv\Scripts\python scripts\build_duckdb.py --demo     # small dataset

Uses the PostgreSQL scripts in sql/postgres and the deterministic synthetic
generator, so the data matches a Neon/PostgreSQL deployment built with the
same seed. See enterprise_copilot/database/duckdb_store.py for what is
translated and why.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the DuckDB analytics database")
    parser.add_argument("--rebuild", action="store_true", help="rebuild even if present")
    parser.add_argument("--demo", action="store_true", help="small dataset (90 customers)")
    parser.add_argument("--full", action="store_true", help="full dataset (600 customers)")
    parser.add_argument("--path", type=Path, help="override DUCKDB_PATH")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.database.duckdb_store import build_database, database_ready

    settings = get_settings()
    if args.path:
        settings.duckdb.path = args.path.resolve()

    if database_ready(settings) and not args.rebuild:
        print(f"[ OK ] {settings.duckdb.path} already built. Use --rebuild to rebuild.")
        return 0

    demo = True if args.demo else (False if args.full else None)
    started = time.perf_counter()
    counts = build_database(settings, demo=demo)
    print(f"[ OK ] Built {settings.duckdb.path} in {time.perf_counter() - started:.1f}s")
    for name, count in counts.items():
        print(f"       {name:22} {count:>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
