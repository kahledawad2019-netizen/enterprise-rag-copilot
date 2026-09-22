"""Regression tests for the mechanical PostgreSQL migration translator."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_translator() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "translate_tsql_to_postgres.py"
    spec = importlib.util.spec_from_file_location("translate_tsql_to_postgres", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRANSLATOR = _load_translator()


def test_convert_apply_preserves_nested_subqueries_and_places_join_condition() -> None:
    source = """\
FROM support.tickets AS tk
OUTER APPLY (
    SELECT TOP (1) sp.*
    FROM support.sla_policies AS sp
    WHERE sp.plan_tier = (
        SELECT TOP (1) pl.tier
        FROM core.plans AS pl
        WHERE pl.plan_id = tk.product_id
    )
) AS pol;
"""

    result = TRANSLATOR.convert_apply(source)

    assert "OUTER APPLY" not in result
    assert "LEFT JOIN LATERAL (" in result
    assert ") AS pol ON TRUE;" in result
    assert "WHERE sp.plan_tier = (" in result


def test_convert_apply_converts_multiple_outer_and_cross_apply_clauses() -> None:
    source = """\
FROM core.customers AS c
OUTER APPLY (SELECT COUNT(*) AS total FROM billing.invoices AS i) AS inv
CROSS APPLY (SELECT c.customer_id AS customer_id) AS ids;
"""

    result = TRANSLATOR.convert_apply(source)

    assert result.count("LEFT JOIN LATERAL") == 1
    assert result.count(" ON TRUE") == 1
    assert "AS inv ON TRUE\nCROSS JOIN LATERAL" in result
    assert "CROSS APPLY" not in result


def test_convert_datediff_accepts_date_or_timestamp_operands() -> None:
    source = "DATEDIFF(DAY, c.signup_date, COALESCE(c.churn_date, CURRENT_DATE))"

    result = TRANSLATOR.convert_date_functions(source)

    assert "DATEDIFF" not in result
    assert "CAST((COALESCE(c.churn_date, CURRENT_DATE)) AS TIMESTAMP)" in result
    assert "CAST((c.signup_date) AS TIMESTAMP)" in result
    assert "EXTRACT(EPOCH FROM" in result
    assert "/ 86400" in result


def test_convert_values_alias_types_uses_schema_derived_types() -> None:
    source = """\
SELECT v.effective_date, v.is_current
FROM (VALUES ('2025-01-01', 1)) AS v(effective_date, is_current);
"""

    result = TRANSLATOR.convert_values_alias_types(
        source,
        dates={"effective_date"},
        booleans={"is_current"},
    )

    assert "SELECT CAST(v.effective_date AS DATE), (v.is_current <> 0)" in result
    assert "AS v(effective_date, is_current)" in result


def test_convert_boolean_casts_maps_sql_server_bit_literals() -> None:
    assert TRANSLATOR.convert_boolean_casts("CAST(1 AS BOOLEAN)") == "TRUE"
    assert TRANSLATOR.convert_boolean_casts("cast(0 as boolean)") == "FALSE"


def test_make_views_security_invoker_prevents_rls_bypass() -> None:
    source = "CREATE OR REPLACE VIEW analytics.vw_customer AS\nSELECT * FROM core.customers;"

    result = TRANSLATOR.make_views_security_invoker(source)

    assert result == (
        "CREATE OR REPLACE VIEW analytics.vw_customer\n"
        "WITH (security_invoker = true)\n"
        "AS\nSELECT * FROM core.customers;"
    )
