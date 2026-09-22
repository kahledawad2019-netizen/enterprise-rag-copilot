"""
Automatic tenant-predicate injection.

## Why this exists

The Text-to-SQL evaluation measured **75 % execution success**, and every one
of the three failures was the same thing: the model omitted the mandatory
`tenant_id` predicate and the guard refused the query.

```
SQL-008  How many customers churned in each month of 2025?   missing_tenant_filter
SQL-009  Which customers were affected by INC-2025-0042?     missing_tenant_filter
SQL-012  How many tickets are currently open?                missing_tenant_filter
```

That is the guard working correctly and the product failing anyway: one
question in four returned a refusal instead of data. The rule is stated in the
prompt; an 8B model simply does not apply it reliably across every query shape.

## Why injection rather than another LLM repair pass

Adding `tenant_id = N` can only ever **narrow** a result set to the caller's
own tenant. It cannot widen access, cannot expose another tenant's rows, and
cannot turn a read into a write. That makes it safe to do deterministically,
which is both faster than a second model call and — more importantly — reliable
in a way a model is not.

The rewrite happens on the parsed tree, never on the text, so formatting,
comments and casing cannot defeat it.

## What it deliberately refuses to do

Injection is skipped, and the original violation stands, whenever the rewrite
cannot be proven correct:

* set operations (`UNION`, `INTERSECT`, `EXCEPT`) — each branch would need its
  own predicate and the alias scoping gets ambiguous
* a query that already references `tenant_id` — it may be restricting to the
  wrong tenant, which is a genuine violation and must stay blocked
* any select whose tenant-scoped table cannot be identified

Refusing to rewrite is always the safe outcome, because the guard then blocks
the query as before.
"""

from __future__ import annotations

import logging

from sqlglot import exp

log = logging.getLogger(__name__)

TENANT_COLUMN = "tenant_id"


def _tenant_scoped_tables(select: exp.Select, exempt: set[str]) -> list[exp.Table]:
    """Tables in THIS select that carry a tenant_id column.

    Only tables belonging to the business schemas are considered, and the
    exempt list (reference data such as products and SLA policies, which have
    no tenant_id) is removed.
    """
    tables: list[exp.Table] = []
    for table in select.find_all(exp.Table):
        # A table inside a nested subquery belongs to that subquery, not here.
        if table.find_ancestor(exp.Select) is not select:
            continue
        name = (table.name or "").lower()
        schema = (table.db or "").lower()
        if not schema or not name:
            continue
        qualified = f"{schema}.{name}"
        if qualified in exempt:
            continue
        if schema in ("core", "billing", "support", "analytics"):
            tables.append(table)
    return tables


def _has_tenant_reference(select: exp.Select) -> bool:
    """Does this select already mention tenant_id in its own WHERE or HAVING?"""
    for clause in (select.args.get("where"), select.args.get("having")):
        if clause is None:
            continue
        for column in clause.find_all(exp.Column):
            if (column.name or "").lower() == TENANT_COLUMN:
                return True
    return False


def _qualifier_for(select: exp.Select, table: exp.Table) -> str | None:
    """The alias to prefix `tenant_id` with, when the select has joins.

    A bare `tenant_id` in a multi-table select is ambiguous and SQL Server
    rejects it, so the predicate must be qualified.
    """
    distinct_sources = {
        t.alias_or_name for t in select.find_all(exp.Table)
        if t.find_ancestor(exp.Select) is select
    }
    if len(distinct_sources) <= 1:
        return None
    return table.alias_or_name or None


def inject_tenant_predicate(
    statement: exp.Expression, tenant_id: int, exempt: set[str] | None = None
) -> tuple[exp.Expression | None, list[str]]:
    """Add `tenant_id = N` wherever a tenant-scoped table is read without it.

    Returns the rewritten tree and a description of each injection, or
    ``(None, [])`` when the rewrite cannot be performed safely.
    """
    exempt = {e.lower() for e in (exempt or set())}

    # Set operations need a predicate per branch with per-branch alias scoping.
    # Not worth the risk of getting subtly wrong; the guard blocks instead.
    if isinstance(statement, exp.Union) or any(
        isinstance(node, exp.Union) for node in statement.find_all(exp.Union)
    ):
        return None, []

    rewritten = statement.copy()
    injections: list[str] = []

    # Every select in the tree, including CTE bodies and subqueries. A CTE body
    # is where the real table is read, so that is where the predicate belongs.
    for select in rewritten.find_all(exp.Select):
        if _has_tenant_reference(select):
            continue

        tables = _tenant_scoped_tables(select, exempt)
        if not tables:
            continue

        target = tables[0]
        qualifier = _qualifier_for(select, target)

        column = exp.column(TENANT_COLUMN, table=qualifier) if qualifier else exp.column(
            TENANT_COLUMN
        )
        predicate = exp.EQ(this=column, expression=exp.Literal.number(tenant_id))

        existing = select.args.get("where")
        if existing is not None:
            select.set("where", exp.Where(this=exp.and_(existing.this, predicate)))
        else:
            select.set("where", exp.Where(this=predicate))

        label = f"{qualifier}.{TENANT_COLUMN}" if qualifier else TENANT_COLUMN
        injections.append(f"{label} = {tenant_id} on {target.db}.{target.name}")

    if not injections:
        return None, []
    return rewritten, injections


def can_inject(statement: exp.Expression) -> bool:
    """Cheap pre-check used by the guard before attempting a rewrite."""
    if isinstance(statement, exp.Union):
        return False
    return isinstance(statement, exp.Select)


__all__ = ["TENANT_COLUMN", "can_inject", "inject_tenant_predicate"]
