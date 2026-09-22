"""
Live gates for the application's own query path.

`validate_postgres_live.py` proves the database enforces least privilege and
tenant isolation. That is necessary and not sufficient: it says nothing about
whether the application *uses* the database correctly. This script exercises
the real `SQLGuard` and `ReadOnlyRunner` against live Neon, so the claim being
tested is "the shipped code path is safe", not "the server could be safe if
asked nicely".

The difference matters. A runner that forgets to set tenant context, sets it
session-scoped, or fetches every row before truncating would pass every gate
in the other script and still leak or fall over in production.

## Running it

    $env:DATABASE_BACKEND = 'postgresql'
    $env:POSTGRES_DSN     = <pooled copilot_app DSN>
    python scripts/validate_runner_live.py

The DSN is read from the environment, never printed, and no customer row is
ever displayed - only counts and verdicts.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

from enterprise_copilot.config import get_settings, reset_settings_cache
from enterprise_copilot.database.read_only_runner import (
    QueryBlockedError,
    ReadOnlyRunner,
)


@dataclass
class Report:
    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append((name, passed, detail))
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


def gate_tenant_scoping(runner: ReadOnlyRunner, report: Report) -> None:
    """The runner must scope every query to the caller's tenant.

    Each tenant is asked the same question. The counts must differ, must sum
    to the whole customer base, and no tenant may see another's rows.
    """
    counts: dict[int, int] = {}
    for tenant in (1, 2, 3):
        result = runner.run(
            "SELECT customer_id, tenant_id FROM core.customers",
            tenant_id=tenant,
            app_user=f"gate-tenant-{tenant}",
        )
        foreign = [r for r in result.rows if r.get("tenant_id") != tenant]
        counts[tenant] = result.row_count
        report.record(
            f"runner scopes core.customers to tenant {tenant}",
            not foreign and result.row_count > 0,
            f"{result.row_count} rows, {len(foreign)} foreign",
        )

    total = sum(counts.values())
    report.record(
        "the three tenants partition the customer base exactly",
        total == 600,
        f"{' + '.join(str(c) for c in counts.values())} = {total}",
    )


def gate_row_cap(runner: ReadOnlyRunner, report: Report) -> None:
    """The row cap must be applied, and applied without fetching everything.

    core.usage_daily holds ~231k rows. If the cap were enforced in Python
    after `fetchall()`, this query would drag the whole table across the wire
    first - so the gate watches the clock as well as the count.
    """
    settings = get_settings()
    cap = settings.database.max_result_rows

    started = time.perf_counter()
    result = runner.run(
        "SELECT usage_date, active_users FROM core.usage_daily",
        tenant_id=1,
        app_user="gate-row-cap",
    )
    elapsed = time.perf_counter() - started

    report.record(
        f"result is capped at max_result_rows ({cap})",
        result.row_count <= cap,
        f"{result.row_count} rows returned",
    )
    report.record(
        "the cap is reported as a truncation, not silently applied",
        result.truncated,
        f"truncated={result.truncated}",
    )
    report.record(
        "capping did not stream the whole table first",
        elapsed < 20,
        f"{elapsed:.1f}s for a ~124k-row tenant slice",
    )


def gate_guard_blocks(runner: ReadOnlyRunner, report: Report) -> None:
    """The guard must refuse before the database is ever asked.

    A blocked query should raise QueryBlockedError, not reach PostgreSQL and
    come back as a privilege error - defence in depth means the first layer
    actually stops things.
    """
    attempts = [
        ("destructive DELETE", "DELETE FROM core.customers WHERE customer_id = 1"),
        ("batched statement", "SELECT 1; DROP TABLE core.customers"),
        ("forbidden schema", "SELECT * FROM security.app_users"),
        ("audit trail read", "SELECT * FROM ai.audit_events"),
        ("cross-tenant predicate", "SELECT * FROM core.customers WHERE tenant_id IN (1,2,3)"),
        ("tenant bypass via OR", "SELECT * FROM core.customers WHERE tenant_id = 1 OR 1 = 1"),
    ]
    for label, sql in attempts:
        try:
            runner.run(sql, tenant_id=1, app_user="gate-guard")
        except QueryBlockedError as exc:
            report.record(f"guard blocks {label}", True, exc.result.violations[0][0].value)
        except Exception as exc:  # reached the server - the guard did not stop it
            report.record(
                f"guard blocks {label}",
                False,
                f"reached the database: {type(exc).__name__}",
            )
        else:
            report.record(f"guard blocks {label}", False, "query EXECUTED")


def gate_missing_tenant(runner: ReadOnlyRunner, report: Report) -> None:
    """A query with no verified tenant must be refused, not run unscoped."""
    try:
        runner.run("SELECT * FROM core.customers", tenant_id=None, app_user="gate-no-tenant")
    except QueryBlockedError as exc:
        report.record(
            "query without tenant context is blocked",
            True,
            exc.result.violations[0][0].value,
        )
    except Exception as exc:
        report.record(
            "query without tenant context is blocked",
            False,
            f"reached the database: {type(exc).__name__}",
        )
    else:
        report.record("query without tenant context is blocked", False, "query EXECUTED")


def main() -> int:
    if os.environ.get("DATABASE_BACKEND") != "postgresql":
        print("DATABASE_BACKEND must be 'postgresql'.", file=sys.stderr)
        return 2
    if not os.environ.get("POSTGRES_DSN", "").strip():
        print("POSTGRES_DSN is not set.", file=sys.stderr)
        return 2

    reset_settings_cache()
    settings = get_settings()

    print("Live gates for the application query path")
    print("=" * 62)
    print(f"  backend={settings.database_backend} "
          f"dialect={settings.sql_dialect} cap={settings.database.max_result_rows}")
    print()

    report = Report()
    runner = ReadOnlyRunner(settings)

    gate_guard_blocks(runner, report)
    gate_missing_tenant(runner, report)
    gate_tenant_scoping(runner, report)
    gate_row_cap(runner, report)

    print("=" * 62)
    failures = report.failures
    if failures:
        print(f"{len(failures)} of {len(report.results)} gates FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}: {detail}")
        return 1

    print(f"All {len(report.results)} gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
