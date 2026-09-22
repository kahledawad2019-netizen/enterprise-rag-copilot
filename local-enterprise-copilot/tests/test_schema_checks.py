"""
Schema-aware SQL checks.

Every case here comes from a real failure observed in the running system, not
from imagination. The headline one:

    SELECT SUM(invoice_number) ... FROM billing.invoices

`invoice_number` is VARCHAR. The security guard allowed it (correctly — it is a
safe read), SQL Server rejected it, and the user got an improvised answer about
reports that do not exist.

The false-positive cases matter just as much. A check that wrongly flags good
SQL sends a correct query off to be "repaired", which is worse than not
checking at all.
"""

from __future__ import annotations

from enterprise_copilot.text_to_sql.schema_checks import (
    NUMERIC_TYPES,
    check_aggregate_types,
    check_columns_exist,
    validate_against_schema,
)
from enterprise_copilot.text_to_sql.schema_retriever import SQLContext, TableInfo


def table(schema: str, name: str, columns: dict[str, str]) -> TableInfo:
    return TableInfo(
        schema=schema,
        name=name,
        object_type="BASE TABLE",
        columns=[{"name": n, "type": t, "nullable": True} for n, t in columns.items()],
    )


INVOICES = table(
    "billing",
    "invoices",
    {
        "invoice_id": "int",
        "invoice_number": "varchar(30)",  # the column that caused the failure
        "customer_id": "int",
        "tenant_id": "int",
        "issue_date": "date",
        "subtotal_amount": "decimal(19,4)",
        "total_amount": "decimal(19,4)",
        "amount_paid": "decimal(19,4)",
        "status": "varchar(20)",
    },
)

PAYMENTS = table(
    "billing",
    "payments",
    {
        "payment_id": "int",
        "amount": "decimal(19,4)",  # exists HERE, not on invoices
        "tenant_id": "int",
    },
)

CUSTOMER_360 = table(
    "analytics",
    "vw_customer_360",
    {
        "customer_id": "int",
        "customer_name": "nvarchar(200)",
        "current_arr": "decimal(19,4)",
        "tenant_id": "int",
    },
)

CATALOG = [INVOICES, PAYMENTS, CUSTOMER_360]


def context(*tables: TableInfo) -> SQLContext:
    return SQLContext(question="q", tables=list(tables), tenant_id=1)


class TestAggregateTypes:
    def test_summing_a_varchar_is_rejected(self) -> None:
        """The exact query that failed in production."""
        problems = check_aggregate_types(
            "SELECT SUM(invoice_number) FROM billing.invoices WHERE tenant_id = 1",
            context(INVOICES),
        )
        assert problems
        assert "invoice_number" in problems[0]
        assert "varchar" in problems[0]

    def test_summing_a_decimal_is_allowed(self) -> None:
        assert not check_aggregate_types(
            "SELECT SUM(total_amount) FROM billing.invoices WHERE tenant_id = 1",
            context(INVOICES),
        )

    def test_counting_a_varchar_is_allowed(self) -> None:
        """COUNT on a text column is perfectly valid and must not be flagged."""
        assert not check_aggregate_types(
            "SELECT COUNT(invoice_number) FROM billing.invoices WHERE tenant_id = 1",
            context(INVOICES),
        )

    def test_avg_of_a_varchar_is_rejected(self) -> None:
        assert check_aggregate_types("SELECT AVG(status) FROM billing.invoices", context(INVOICES))

    def test_types_resolve_from_the_catalog_when_the_table_was_not_offered(self) -> None:
        """The bug that let the failure survive the first fix.

        The retriever offered only analytics views, so the check had no type
        information for billing.invoices and stayed silent.
        """
        sql = "SELECT SUM(invoice_number) FROM billing.invoices WHERE tenant_id = 1"
        offered_elsewhere = context(CUSTOMER_360)

        assert not check_aggregate_types(sql, offered_elsewhere), (
            "expected silence without a catalog"
        )
        assert check_aggregate_types(sql, offered_elsewhere, CATALOG), "catalog lookup failed"

    def test_suggestions_name_columns_of_the_queried_table(self) -> None:
        """An earlier version suggested `active`, a column of an unrelated table."""
        problems = check_aggregate_types(
            "SELECT SUM(invoice_number) FROM billing.invoices",
            context(CUSTOMER_360),
            CATALOG,
        )
        assert problems
        message = problems[0]
        assert "total_amount" in message
        assert "current_arr" not in message, "suggested a column from another table"

    def test_unknown_column_produces_no_complaint(self) -> None:
        """A false accusation is worse than silence."""
        assert not check_aggregate_types(
            "SELECT SUM(mystery_column) FROM billing.invoices", context(INVOICES)
        )


class TestColumnExistence:
    def test_hallucinated_column_is_caught(self) -> None:
        """The repair invented SUM(amount); `amount` lives on payments."""
        problems = check_columns_exist(
            "SELECT SUM(amount) FROM billing.invoices WHERE tenant_id = 1",
            context(INVOICES),
            CATALOG,
        )
        assert problems
        assert "amount" in problems[0]

    def test_real_columns_pass(self) -> None:
        assert not check_columns_exist(
            "SELECT total_amount, status FROM billing.invoices WHERE tenant_id = 1",
            context(INVOICES),
            CATALOG,
        )

    def test_aliases_are_not_treated_as_unknown_columns(self) -> None:
        assert not check_columns_exist(
            "SELECT SUM(total_amount) AS total_sales FROM billing.invoices",
            context(INVOICES),
            CATALOG,
        )

    def test_cte_names_are_not_treated_as_tables(self) -> None:
        sql = (
            "WITH recent AS (SELECT total_amount, tenant_id FROM billing.invoices) "
            "SELECT SUM(total_amount) FROM recent"
        )
        assert not check_columns_exist(sql, context(INVOICES), CATALOG)

    def test_unknown_table_produces_silence(self) -> None:
        """Without knowing the table, any column verdict would be a guess."""
        assert not check_columns_exist(
            "SELECT whatever FROM some.unknown_table", context(INVOICES), CATALOG
        )

    def test_joined_tables_are_both_considered(self) -> None:
        sql = (
            "SELECT i.total_amount, p.amount FROM billing.invoices i "
            "JOIN billing.payments p ON p.tenant_id = i.tenant_id"
        )
        assert not check_columns_exist(sql, context(INVOICES, PAYMENTS), CATALOG)


class TestCombined:
    def test_valid_query_passes_everything(self) -> None:
        assert not validate_against_schema(
            "SELECT SUM(total_amount) AS total_sales, COUNT(invoice_number) AS n "
            "FROM billing.invoices WHERE tenant_id = 1",
            context(INVOICES),
            CATALOG,
        )

    def test_the_original_failure_is_caught(self) -> None:
        assert validate_against_schema(
            "SELECT SUM(invoice_number) AS total_sales, SUM(subtotal_amount) "
            "FROM billing.invoices WHERE tenant_id = 1",
            context(CUSTOMER_360),
            CATALOG,
        )

    def test_unparseable_sql_is_left_to_the_guard(self) -> None:
        assert not validate_against_schema("not sql at all (((", context(INVOICES), CATALOG)

    def test_empty_sql(self) -> None:
        assert not validate_against_schema("", context(INVOICES), CATALOG)

    def test_numeric_type_set_covers_the_money_types(self) -> None:
        assert {"decimal", "numeric", "money", "int", "bigint", "float"} <= NUMERIC_TYPES
        assert "varchar" not in NUMERIC_TYPES
        assert "date" not in NUMERIC_TYPES
