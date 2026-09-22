"""
Schema-aware checks on generated SQL.

The security guard answers *"is this SQL safe?"*. It deliberately does not
answer *"will this SQL actually run?"* — that needs column types, which is
correctness rather than security, and mixing the two would make the guard both
harder to reason about and harder to trust.

This module is that second question, and it exists because of a real failure.
Asked for "total sales for last week", the model produced:

    SELECT SUM(invoice_number) AS total_sales, SUM(subtotal_amount) ...
    FROM billing.invoices

`invoice_number` is `VARCHAR(30)` holding values like `INV-2023-000041`. The
guard allowed it (correctly — it is a safe, tenant-filtered read), SQL Server
rejected it with *"Operand data type varchar is invalid for sum operator"*, and
the user saw an improvised answer about reports that do not exist.

## Types are resolved from the whole catalog, not the offered subset

The first version of this module only looked at the tables the schema retriever
had selected for that question. `billing.invoices` was not among them — the
model knew the table from its own few-shot examples — so the check found no
type information and stayed silent, and the bug survived.

Types now come from the full catalog, scoped to the tables the query actually
references. The offered context is still used to detect hallucinated columns,
because that check is only meaningful within a known set.
"""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp

from .schema_retriever import SQLContext, TableInfo

log = logging.getLogger(__name__)

DIALECT = "tsql"

# T-SQL types that arithmetic aggregates accept.
NUMERIC_TYPES = frozenset({
    "int", "bigint", "smallint", "tinyint", "decimal", "numeric",
    "float", "real", "money", "smallmoney", "bit",
})

# Aggregates requiring a numeric argument. COUNT is excluded on purpose:
# counting a varchar column is perfectly valid.
NUMERIC_AGGREGATES: tuple[type[exp.Expression], ...] = (exp.Sum, exp.Avg)


def _referenced_tables(statement: exp.Expression) -> set[str]:
    """Qualified names of real tables in the query, excluding CTE aliases."""
    cte_names = {c.alias_or_name.lower() for c in statement.find_all(exp.CTE)}
    names: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        names.add(f"{table.db.lower()}.{name}" if table.db else name)
    return names


def _types_for(tables: list[TableInfo]) -> dict[str, str]:
    """column name -> base type. Ambiguous names are dropped, not guessed."""
    seen: dict[str, str] = {}
    ambiguous: set[str] = set()
    for table in tables:
        for column in table.columns:
            name = column["name"].lower()
            base = column["type"].split("(")[0].strip().lower()
            if name in seen and seen[name] != base:
                ambiguous.add(name)
            seen[name] = base
    for name in ambiguous:
        seen.pop(name, None)
    return seen


def check_aggregate_types(
    sql: str, context: SQLContext, catalog: list[TableInfo] | None = None
) -> list[str]:
    """Report SUM/AVG applied to a non-numeric column.

    Only complains when the column's type is *known*. An unknown column
    produces no complaint, because a false accusation sends the model off to
    fix something that was never wrong.
    """
    if not sql.strip():
        return []
    try:
        statement = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return []   # the guard reports parse failures; not this module's job

    referenced = _referenced_tables(statement)
    sources = list(context.tables)
    if catalog:
        sources += [t for t in catalog if t.qualified.lower() in referenced]

    # Suggestions must come from the tables the query actually reads. Pooling
    # the offered context as well produced advice to use `active`, a column of
    # an unrelated table the retriever happened to have selected.
    in_query = [t for t in sources if t.qualified.lower() in referenced] or sources

    types = _types_for(sources)
    problems: list[str] = []

    for aggregate_type in NUMERIC_AGGREGATES:
        for node in statement.find_all(aggregate_type):
            for column in node.find_all(exp.Column):
                name = (column.name or "").lower()
                declared = types.get(name)
                if declared is None or declared in NUMERIC_TYPES:
                    continue
                # Suggest columns that exist in THESE tables. A generic
                # suggestion list caused the repair to invent `amount`, which
                # lives in billing.payments, not billing.invoices.
                numeric = sorted({
                    c["name"] for t in in_query for c in t.columns
                    if c["type"].split("(")[0].strip().lower() in NUMERIC_TYPES
                    and not c["name"].lower().endswith("_id")
                })[:6]
                options = ", ".join(numeric) if numeric else "a numeric column"
                problems.append(
                    f"{aggregate_type.__name__.upper()}({column.name}) is invalid: "
                    f"{column.name} is {declared}, not numeric. "
                    f"Numeric columns available here: {options}. "
                    f"Or use COUNT({column.name}) if you meant to count rows."
                )

    if problems:
        log.info("Schema check rejected generated SQL: %s", problems)
    return problems


def check_columns_exist(
    sql: str, context: SQLContext, catalog: list[TableInfo] | None = None
) -> list[str]:
    """Report columns that exist in no offered table.

    **Only runs when every table in the query was actually offered.** The
    schema retriever sends a subset, and the model legitimately uses tables it
    learned from the approved examples. Without this guard the check reported
    `total_amount` as nonexistent purely because `billing.invoices` had not
    been selected for that question — a false positive that would have sent a
    perfectly good query off to be "repaired".
    """
    if not sql.strip():
        return []
    try:
        statement = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return []

    # Resolve every referenced table against the offered set first, then the
    # full catalog. Only checking the offered subset let a hallucinated column
    # through whenever the model used a table the retriever had not selected.
    referenced = _referenced_tables(statement)
    by_name = {t.qualified.lower(): t for t in (catalog or [])}
    by_name.update({t.qualified.lower(): t for t in context.tables})

    resolved = [by_name[name] for name in referenced if name in by_name]
    if len(resolved) != len(referenced):
        return []   # a table we know nothing about; say nothing rather than guess

    known: set[str] = set()
    for table in resolved:
        known.update(c["name"].lower() for c in table.columns)

    aliases = {(a.alias or "").lower() for a in statement.find_all(exp.Alias) if a.alias}
    aliases |= {(c.alias_or_name or "").lower() for c in statement.find_all(exp.CTE)}

    problems: list[str] = []
    reported: set[str] = set()
    for column in statement.find_all(exp.Column):
        name = (column.name or "").lower()
        if not name or name in known or name in aliases or name in reported:
            continue
        reported.add(name)
        problems.append(f"column '{column.name}' does not exist in the tables provided")

    if problems:
        log.info("Schema check found unknown columns: %s", problems)
    return problems


def validate_against_schema(
    sql: str, context: SQLContext, catalog: list[TableInfo] | None = None
) -> list[str]:
    """All schema-aware checks. An empty list means the SQL should execute."""
    return check_aggregate_types(sql, context, catalog) + check_columns_exist(sql, context, catalog)


__all__ = [
    "NUMERIC_AGGREGATES", "NUMERIC_TYPES", "check_aggregate_types",
    "check_columns_exist", "validate_against_schema",
]
