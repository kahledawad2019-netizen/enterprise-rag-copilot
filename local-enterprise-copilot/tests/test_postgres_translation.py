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
