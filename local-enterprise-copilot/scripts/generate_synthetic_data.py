r"""
Generate and load the synthetic Northwind Cloud dataset.

    .venv\Scripts\python scripts\generate_synthetic_data.py
    .venv\Scripts\python scripts\generate_synthetic_data.py --demo    # small, fast
    .venv\Scripts\python scripts\generate_synthetic_data.py --dry-run # no writes

Deterministic: the same COPILOT_SEED always produces the same data, so the
expected answers in evals/ stay valid across machines and re-runs.

Re-running replaces the transactional data but leaves the reference data from
007_seed_reference_data.sql untouched.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def load_reference_data(conn) -> dict:
    """Read the reference rows the generator needs (created by script 007)."""
    from enterprise_copilot.database.synthetic_loader import read_reference_data

    return read_reference_data(conn)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic Northwind Cloud data")
    parser.add_argument("--demo", action="store_true", help="small dataset for the notebook")
    parser.add_argument("--dry-run", action="store_true", help="generate but do not write")
    parser.add_argument("--keep", action="store_true", help="do not clear existing rows first")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.database.connection import raw_connection
    from enterprise_copilot.database.synthetic import EDGE_CASES, SyntheticGenerator, Volumes
    from enterprise_copilot.database.synthetic_loader import SyntheticLoader
    from enterprise_copilot.database.synthetic_operations import (
        generate_billing,
        generate_health,
        generate_incidents,
        generate_support,
        generate_usage,
    )

    settings = get_settings()
    volumes = Volumes.demo() if (args.demo or settings.demo_mode) else Volumes()

    print("=" * 78)
    print("  Synthetic data generation - Northwind Cloud")
    print(
        f"  seed={settings.random_seed}  profile={'demo' if args.demo else 'full'}  "
        f"target customers={volumes.customers}"
    )
    print("=" * 78)

    with raw_connection(settings) as conn:
        reference = load_reference_data(conn)
        print(
            f"Reference data: {len(reference['tenants'])} tenants, "
            f"{len(reference['products'])} products, {len(reference['plans'])} plans, "
            f"{len(reference['sla_policies'])} SLA policy rows"
        )

        started = time.perf_counter()
        generator = SyntheticGenerator(settings, volumes, reference)

        for label, step in [
            ("customers & contacts", generator.generate_customers),
            ("subscriptions & changes", generator.generate_subscriptions),
            ("usage", lambda: generate_usage(generator)),
            ("billing", lambda: generate_billing(generator)),
            ("incidents", lambda: generate_incidents(generator)),
            ("support tickets", lambda: generate_support(generator)),
            ("customer health", lambda: generate_health(generator)),
        ]:
            step_started = time.perf_counter()
            step()
            print(f"  generated {label:26} {time.perf_counter() - step_started:6.2f}s")

        counts = generator.data.counts()
        print(f"\nGenerated in {time.perf_counter() - started:.2f}s:")
        for name, count in counts.items():
            print(f"  {name:22} {count:>8,}")

        if args.dry_run:
            print("\n--dry-run: nothing written to the database.")
            return 0

        print(f"\nLoading into {settings.database_backend} ...")
        load_started = time.perf_counter()
        loader = SyntheticLoader(conn, dialect=settings.sql_dialect)
        if not args.keep:
            loader.clear_transactional_data()
        loader.load(generator.data)
        print(f"Loaded in {time.perf_counter() - load_started:.2f}s")

    print("\nDeliberate patterns present in this dataset:")
    for name, description in EDGE_CASES.items():
        print(f"  - {name:26} {description}")

    print("\nNext: .venv\\Scripts\\python scripts\\setup_database.py --validate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
