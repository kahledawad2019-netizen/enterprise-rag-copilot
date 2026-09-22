"""
Tenant-predicate injection.

Motivated by measurement: the Text-to-SQL evaluation scored 75 % execution
success, and all three failures were a missing `tenant_id` predicate. The guard
was right to refuse them; the product still failed the user.

Injection can only **narrow** a result set, which is what makes it safe to do
automatically. The tests below split into two groups, and the second matters
more than the first:

* repairs that must happen, so the feature is useful
* repairs that must **not** happen, so the feature is safe
"""

from __future__ import annotations

import pytest

from enterprise_copilot.security.sql_guard import SQLGuard, Violation


@pytest.fixture(scope="module")
def guard() -> SQLGuard:
    return SQLGuard()


def violations_of(result) -> set[Violation]:
    return {v for v, _ in result.violations}


class TestRepairsThatMustHappen:
    """Each of these was a refusal the user experienced as a failure."""

    @pytest.mark.parametrize(("label", "sql"), [
        ("no WHERE at all", "SELECT COUNT(*) FROM support.tickets"),
        ("existing WHERE", "SELECT COUNT(*) FROM support.tickets WHERE status = 'open'"),
        ("GROUP BY",
         "SELECT region, COUNT(*) FROM analytics.vw_customer_360 GROUP BY region"),
        ("ORDER BY",
         "SELECT customer_name FROM analytics.vw_customer_360 ORDER BY current_arr DESC"),
        ("aggregate over a base table",
         "SELECT SUM(total_amount) FROM billing.invoices WHERE status = 'paid'"),
    ])
    def test_missing_predicate_is_injected(self, guard: SQLGuard, label: str, sql: str) -> None:
        result = guard.validate(sql, tenant_id=1)
        assert result.is_safe, f"{label} still blocked: {result.reason}"
        assert result.tenant_injected
        assert "tenant_id = 1" in result.effective_sql

    def test_join_predicate_is_alias_qualified(self, guard: SQLGuard) -> None:
        """A bare tenant_id across joined tables is ambiguous and SQL Server rejects it."""
        result = guard.validate(
            "SELECT c.customer_name FROM analytics.vw_customer_360 c "
            "JOIN analytics.vw_customer_risk r ON r.customer_id = c.customer_id",
            tenant_id=1,
        )
        assert result.is_safe, result.reason
        assert "c.tenant_id = 1" in result.effective_sql

    def test_cte_body_is_repaired_not_the_outer_select(self, guard: SQLGuard) -> None:
        """The CTE body reads the real table, so that is where the predicate belongs."""
        result = guard.validate(
            "WITH t AS (SELECT customer_id FROM core.customers) SELECT COUNT(*) FROM t",
            tenant_id=1,
        )
        assert result.is_safe, result.reason
        rewritten = result.effective_sql
        # The predicate must be inside the CTE, before the closing bracket.
        assert "tenant_id = 1" in rewritten.split(")")[0]

    def test_a_different_tenant_gets_its_own_predicate(self, guard: SQLGuard) -> None:
        result = guard.validate("SELECT COUNT(*) FROM support.tickets", tenant_id=3)
        assert result.is_safe
        assert "tenant_id = 3" in result.effective_sql

    def test_injection_is_reported_not_silent(self, guard: SQLGuard) -> None:
        """The user and the trace must be able to see that the query was changed."""
        result = guard.validate("SELECT COUNT(*) FROM support.tickets", tenant_id=1)
        assert any("added automatically" in w for w in result.warnings)


class TestRepairsThatMustNotHappen:
    """Injection must never widen access or rescue a genuine violation."""

    def test_wrong_tenant_stays_blocked(self, guard: SQLGuard) -> None:
        """Asking for another tenant is a violation, not an omission."""
        result = guard.validate(
            "SELECT customer_name FROM analytics.vw_customer_360 WHERE tenant_id = 2",
            tenant_id=1,
        )
        assert not result.is_safe
        assert Violation.CROSS_TENANT in violations_of(result)
        assert not result.tenant_injected

    def test_multi_tenant_in_clause_stays_blocked(self, guard: SQLGuard) -> None:
        result = guard.validate(
            "SELECT customer_name, tenant_id FROM analytics.vw_customer_360 "
            "WHERE tenant_id IN (1, 2, 3)",
            tenant_id=1,
        )
        assert not result.is_safe
        assert not result.tenant_injected

    def test_forbidden_schema_is_not_rescued(self, guard: SQLGuard) -> None:
        """Repair applies only when the tenant predicate is the ONLY problem."""
        result = guard.validate("SELECT * FROM ai.audit_events", tenant_id=1)
        assert not result.is_safe
        assert Violation.FORBIDDEN_SCHEMA in violations_of(result)
        assert not result.tenant_injected

    def test_write_operation_is_not_rescued(self, guard: SQLGuard) -> None:
        result = guard.validate("DELETE FROM core.customers", tenant_id=1)
        assert not result.is_safe
        assert not result.tenant_injected

    def test_blocked_column_is_not_rescued(self, guard: SQLGuard) -> None:
        result = guard.validate("SELECT password_hash FROM core.customers", tenant_id=1)
        assert not result.is_safe
        assert not result.tenant_injected

    def test_exempt_reference_table_is_untouched(self, guard: SQLGuard) -> None:
        """core.plans has no tenant_id; injecting one would break the query."""
        result = guard.validate("SELECT plan_name FROM core.plans", tenant_id=1)
        assert result.is_safe
        assert not result.tenant_injected
        assert "tenant_id" not in result.effective_sql

    def test_correct_query_is_not_rewritten(self, guard: SQLGuard) -> None:
        result = guard.validate(
            "SELECT customer_name FROM analytics.vw_customer_360 WHERE tenant_id = 1",
            tenant_id=1,
        )
        assert result.is_safe
        assert not result.tenant_injected

    def test_injection_can_be_disabled(self, guard: SQLGuard) -> None:
        """The original strict behaviour must remain available."""
        result = guard.validate(
            "SELECT COUNT(*) FROM support.tickets", tenant_id=1, auto_repair=False
        )
        assert not result.is_safe
        assert Violation.MISSING_TENANT_FILTER in violations_of(result)


class TestInjectedSQLIsValid:
    """A rewrite that produces invalid SQL is worse than no rewrite."""

    @pytest.mark.parametrize("sql", [
        "SELECT COUNT(*) FROM support.tickets",
        "SELECT region, COUNT(*) FROM analytics.vw_customer_360 GROUP BY region",
        "SELECT c.customer_name FROM analytics.vw_customer_360 c "
        "JOIN analytics.vw_customer_risk r ON r.customer_id = c.customer_id",
        "WITH t AS (SELECT customer_id FROM core.customers) SELECT COUNT(*) FROM t",
    ])
    def test_result_reparses_as_tsql(self, guard: SQLGuard, sql: str) -> None:
        import sqlglot

        result = guard.validate(sql, tenant_id=1)
        assert result.is_safe
        sqlglot.parse_one(result.effective_sql, read="tsql")   # raises on invalid SQL

    def test_repaired_query_revalidates_clean(self, guard: SQLGuard) -> None:
        """Feeding the rewrite back through the guard must produce no violations."""
        first = guard.validate("SELECT COUNT(*) FROM support.tickets", tenant_id=1)
        second = guard.validate(first.effective_sql, tenant_id=1)
        assert second.is_safe, second.reason
        assert not second.tenant_injected, "second pass should need no repair"


@pytest.mark.integration
@pytest.mark.requires_sqlserver
class TestInjectedSQLExecutes:
    """The repaired query must actually run against SQL Server."""

    @pytest.fixture(scope="class")
    def runner(self):
        from enterprise_copilot.database.read_only_runner import ReadOnlyRunner

        try:
            instance = ReadOnlyRunner()
            instance.run("SELECT TOP 1 tenant_id FROM core.customers WHERE tenant_id = 1",
                         tenant_id=1)
        except Exception as exc:
            pytest.skip(f"database unavailable: {exc}")
        return instance

    @pytest.mark.parametrize("sql", [
        "SELECT COUNT(*) AS open_tickets FROM support.tickets WHERE status = 'open'",
        "SELECT region, COUNT(*) AS n FROM analytics.vw_customer_360 GROUP BY region",
        "SELECT c.customer_name FROM analytics.vw_customer_360 c "
        "JOIN analytics.vw_customer_risk r ON r.customer_id = c.customer_id",
    ])
    def test_executes_after_repair(self, runner, sql: str) -> None:
        result = runner.run(sql, tenant_id=1, app_user="pytest")
        assert result.row_count >= 0
        assert result.validation is not None and result.validation.tenant_injected

    def test_repair_restricts_to_the_callers_tenant(self, runner) -> None:
        """The whole point: the rows returned belong to tenant 1 only."""
        result = runner.run(
            "SELECT DISTINCT tenant_id FROM analytics.vw_customer_360", tenant_id=1
        )
        tenants = {row["tenant_id"] for row in result.rows}
        assert tenants == {1}, f"repair leaked other tenants: {tenants}"
