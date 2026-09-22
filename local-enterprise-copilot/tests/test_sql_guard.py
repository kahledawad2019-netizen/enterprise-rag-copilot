"""
SQL guard tests.

This is the security boundary implemented in application code, so it gets the
most adversarial test coverage in the project. Every case here is a real attack
shape, not a synthetic one.

These tests do not touch a database — the guard must refuse a query before it
ever reaches one.
"""

from __future__ import annotations

import pytest

from enterprise_copilot.security.sql_guard import SQLGuard, Violation


@pytest.fixture(scope="module")
def guard() -> SQLGuard:
    return SQLGuard()


def violations_of(result) -> set[Violation]:
    return {v for v, _ in result.violations}


class TestAllowsLegitimateReads:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT TOP 5 customer_name FROM analytics.vw_customer_360",
            "SELECT customer_name, current_arr FROM analytics.vw_customer_360 ORDER BY current_arr DESC",
            "WITH t AS (SELECT customer_id FROM core.customers) SELECT COUNT(*) FROM t",
            "SELECT region, COUNT(*) FROM analytics.vw_customer_360 GROUP BY region",
            "SELECT a.customer_name FROM analytics.vw_customer_360 a "
            "JOIN analytics.vw_customer_risk b ON b.customer_id = a.customer_id",
            "SELECT COUNT(*) FROM support.tickets WHERE priority = 'P1'",
        ],
    )
    def test_allowed(self, guard: SQLGuard, sql: str) -> None:
        result = guard.validate(sql, tenant_id=1)
        assert result.is_safe, f"wrongly blocked: {result.reason}"

    def test_cte_is_not_mistaken_for_a_table(self, guard: SQLGuard) -> None:
        """A CTE name must not be schema-checked as though it were a table."""
        result = guard.validate(
            "WITH recent AS (SELECT customer_id, tenant_id FROM core.customers) "
            "SELECT COUNT(*) FROM recent",
            tenant_id=1,
        )
        assert result.is_safe, result.reason
        assert "recent" not in " ".join(result.tables)


class TestBlocksWrites:
    @pytest.mark.parametrize(
        ("sql", "label"),
        [
            ("DELETE FROM core.customers", "delete"),
            ("UPDATE core.customers SET status = 'x'", "update"),
            ("INSERT INTO core.customers (customer_code) VALUES ('x')", "insert"),
            ("DROP TABLE core.customers", "drop"),
            ("TRUNCATE TABLE support.tickets", "truncate"),
            ("ALTER TABLE core.customers ADD c INT", "alter"),
            ("CREATE TABLE evil (id INT)", "create"),
        ],
    )
    def test_write_is_blocked(self, guard: SQLGuard, sql: str, label: str) -> None:
        result = guard.validate(sql)
        assert not result.is_safe, f"{label} was allowed"

    def test_select_into_is_blocked(self, guard: SQLGuard) -> None:
        """SELECT ... INTO parses as a Select but creates a table."""
        result = guard.validate("SELECT * INTO backup FROM core.customers")
        assert not result.is_safe
        assert Violation.WRITE_OPERATION in violations_of(result)


class TestBlocksBatching:
    """Batched statements are the classic way past a keyword blocklist."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; DROP TABLE core.customers",
            "SELECT 1 /* hide */ ; DROP TABLE core.customers",
            "SELECT * FROM core.customers; DELETE FROM core.customers",
            "SELECT 1;\n\nTRUNCATE TABLE support.tickets",
        ],
    )
    def test_batched_statements_blocked(self, guard: SQLGuard, sql: str) -> None:
        result = guard.validate(sql)
        assert not result.is_safe
        assert Violation.MULTIPLE_STATEMENTS in violations_of(result)

    def test_comment_cannot_hide_a_second_statement(self, guard: SQLGuard) -> None:
        """Parsing, not regex, is what makes this reliable."""
        result = guard.validate("SELECT 1 /* a very long comment ; */ ; DROP TABLE core.customers")
        assert not result.is_safe


class TestBlocksProcedures:
    @pytest.mark.parametrize(
        "sql",
        [
            "EXEC sp_executesql N'SELECT 1'",
            "EXECUTE sp_who",
            "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')",
            "SELECT * FROM OPENQUERY(remote, 'SELECT 1')",
        ],
    )
    def test_procedure_and_remote_access_blocked(self, guard: SQLGuard, sql: str) -> None:
        assert not guard.validate(sql).is_safe


class TestSchemaRestrictions:
    def test_ai_schema_is_not_readable(self, guard: SQLGuard) -> None:
        """The AI must not be able to read its own audit trail."""
        result = guard.validate("SELECT * FROM ai.audit_events")
        assert not result.is_safe
        assert Violation.FORBIDDEN_SCHEMA in violations_of(result)

    def test_security_schema_is_not_readable(self, guard: SQLGuard) -> None:
        result = guard.validate("SELECT * FROM security.app_users")
        assert not result.is_safe
        assert Violation.FORBIDDEN_SCHEMA in violations_of(result)

    def test_unqualified_table_is_refused(self, guard: SQLGuard) -> None:
        """An unqualified name cannot be checked, so it is not assumed safe."""
        result = guard.validate("SELECT * FROM customers")
        assert not result.is_safe
        assert Violation.FORBIDDEN_SCHEMA in violations_of(result)

    def test_forbidden_schema_inside_a_subquery_is_caught(self, guard: SQLGuard) -> None:
        result = guard.validate(
            "SELECT * FROM analytics.vw_customer_360 WHERE customer_id IN "
            "(SELECT tenant_id FROM security.app_users)"
        )
        assert not result.is_safe


class TestColumnRestrictions:
    @pytest.mark.parametrize("column", ["password_hash", "api_key", "secret", "token"])
    def test_blocked_columns(self, guard: SQLGuard, column: str) -> None:
        result = guard.validate(f"SELECT {column} FROM core.customers")
        assert not result.is_safe
        assert Violation.FORBIDDEN_COLUMN in violations_of(result)

    def test_select_star_is_warned_about(self, guard: SQLGuard) -> None:
        result = guard.validate("SELECT * FROM analytics.vw_customer_360", tenant_id=1)
        assert result.is_safe
        assert any("SELECT *" in w for w in result.warnings)


class TestTenantIsolation:
    def test_missing_tenant_predicate_is_repaired_not_executed_unscoped(
        self, guard: SQLGuard
    ) -> None:
        """A missing predicate is now repaired rather than refused.

        The security property is unchanged — the query never runs unscoped —
        but the user gets an answer instead of a refusal. See
        tests/test_tenant_injection.py and `security/tenant_injection.py`.
        """
        result = guard.validate("SELECT customer_name FROM analytics.vw_customer_360", tenant_id=1)
        assert result.is_safe
        assert result.tenant_injected
        assert "tenant_id = 1" in result.effective_sql

    def test_missing_tenant_predicate_is_blocked_when_repair_is_disabled(
        self, guard: SQLGuard
    ) -> None:
        """The original strict behaviour is still available and still correct."""
        result = guard.validate(
            "SELECT customer_name FROM analytics.vw_customer_360",
            tenant_id=1,
            auto_repair=False,
        )
        assert not result.is_safe
        assert Violation.MISSING_TENANT_FILTER in violations_of(result)

    def test_correct_tenant_is_allowed(self, guard: SQLGuard) -> None:
        result = guard.validate(
            "SELECT customer_name FROM analytics.vw_customer_360 WHERE tenant_id = 1",
            tenant_id=1,
        )
        assert result.is_safe, result.reason

    def test_other_tenant_is_blocked(self, guard: SQLGuard) -> None:
        result = guard.validate(
            "SELECT customer_name FROM analytics.vw_customer_360 WHERE tenant_id = 2",
            tenant_id=1,
        )
        assert not result.is_safe
        assert Violation.CROSS_TENANT in violations_of(result)

    def test_multi_tenant_in_clause_is_blocked(self, guard: SQLGuard) -> None:
        """The exact SQL the model produced when asked to ignore tenant limits."""
        result = guard.validate(
            "SELECT customer_name, tenant_id FROM analytics.vw_customer_360 "
            "WHERE tenant_id IN (1, 2, 3)",
            tenant_id=1,
        )
        assert not result.is_safe
        assert Violation.CROSS_TENANT in violations_of(result)

    def test_tenant_id_inside_a_string_literal_does_not_satisfy_the_check(
        self, guard: SQLGuard
    ) -> None:
        """Checking the parse tree, not the text, is what makes this hold.

        A regex looking for "tenant_id = 1" would accept this query as already
        scoped. The AST does not, so a real predicate is added alongside the
        literal rather than the literal being mistaken for one.
        """
        sql = (
            "SELECT customer_name FROM analytics.vw_customer_360 "
            "WHERE customer_name = 'tenant_id = 1'"
        )

        strict = guard.validate(sql, tenant_id=1, auto_repair=False)
        assert not strict.is_safe
        assert Violation.MISSING_TENANT_FILTER in violations_of(strict)

        repaired = guard.validate(sql, tenant_id=1)
        assert repaired.is_safe and repaired.tenant_injected
        # The literal survives as data; a genuine predicate is now present too.
        assert "'tenant_id = 1'" in repaired.effective_sql
        assert "AND tenant_id = 1" in repaired.effective_sql


class TestResourceLimits:
    def test_excessive_joins_blocked(self, guard: SQLGuard) -> None:
        joins = " ".join(
            f"JOIN core.customers c{n} ON c{n}.customer_id = c0.customer_id" for n in range(1, 12)
        )
        result = guard.validate(f"SELECT c0.customer_id FROM core.customers c0 {joins}")
        assert not result.is_safe
        assert Violation.TOO_MANY_JOINS in violations_of(result)

    def test_row_limit_is_injected(self, guard: SQLGuard) -> None:
        result = guard.validate("SELECT customer_name FROM analytics.vw_customer_360", tenant_id=1)
        assert result.effective_sql != result.sql
        assert "TOP" in result.effective_sql.upper()

    def test_existing_limit_is_respected(self, guard: SQLGuard) -> None:
        sql = "SELECT TOP 5 customer_name FROM analytics.vw_customer_360"
        result = guard.validate(sql)
        assert "TOP 5" in result.effective_sql


class TestMalformedInput:
    def test_empty_query(self, guard: SQLGuard) -> None:
        assert not guard.validate("").is_safe
        assert not guard.validate("   ").is_safe

    def test_unparseable_sql_is_blocked_not_crashed(self, guard: SQLGuard) -> None:
        result = guard.validate("SELECT FROM WHERE ((( not sql at all")
        assert not result.is_safe

    def test_prose_is_blocked(self, guard: SQLGuard) -> None:
        result = guard.validate("I'm sorry, I cannot answer that question.")
        assert not result.is_safe


class TestViewPreference:
    def test_analytics_view_use_is_detected(self, guard: SQLGuard) -> None:
        result = guard.validate("SELECT customer_name FROM analytics.vw_customer_360")
        assert result.uses_analytics_view
        assert not any("base tables" in w for w in result.warnings)

    def test_base_table_use_is_warned_about(self, guard: SQLGuard) -> None:
        """Base-table queries recompute metrics the views already define."""
        result = guard.validate("SELECT display_name FROM core.customers", tenant_id=1)
        assert result.is_safe
        assert any("base tables" in w for w in result.warnings)
