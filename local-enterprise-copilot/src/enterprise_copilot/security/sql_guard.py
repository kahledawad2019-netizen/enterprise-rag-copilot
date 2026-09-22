"""
SQL safety validation.

The SQL executed by this system is written by a language model, which makes it
untrusted input in the strictest sense: an attacker who can influence the
prompt can influence the query.

## Why this parses instead of pattern-matching

Regular expressions are the usual approach and they are not sufficient. Every
one of these defeats a keyword blocklist:

    SELECT 1; /*x*/ DROP TABLE core.customers        -- comment between statements
    SELECT * FROM core/**/.customers                 -- comment inside an identifier
    SELECT * FROM [core].[customers] WHERE 1=1 --    -- trailing comment truncation
    EXECUTE ('DROP TABLE x')                         -- spelling variant
    SELECT * FROM core.customers FOR XML PATH        -- unexpected clause

So the query is **parsed with sqlglot into an abstract syntax tree** and the
tree is inspected. A comment cannot hide a node, and an alternative spelling
parses to the same node type. Regex is used only as a defence-in-depth second
pass, never as the primary check.

## What this is not

This is **layer 1**. It is application code, and application code can be
refactored, bypassed, or contain a bug. The real boundary is layer 2: a
database principal that physically cannot write (`sql/006_create_security.sql`).
Neither layer is sufficient alone, and this module never pretends otherwise.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

import sqlglot
from sqlglot import exp

from ..config import Settings, get_settings
from .tenant_injection import (
    TENANT_COLUMN,
    filtering_clauses,
    inject_tenant_predicate,
    qualifier_for,
    tenant_scoped_tables,
)

log = logging.getLogger(__name__)

DIALECT = "tsql"


class Violation(StrEnum):
    PARSE_ERROR = "parse_error"
    NOT_A_SELECT = "not_a_select"
    MULTIPLE_STATEMENTS = "multiple_statements"
    WRITE_OPERATION = "write_operation"
    FORBIDDEN_SCHEMA = "forbidden_schema"
    FORBIDDEN_COLUMN = "forbidden_column"
    MISSING_TENANT_FILTER = "missing_tenant_filter"
    CROSS_TENANT = "cross_tenant"
    TOO_MANY_JOINS = "too_many_joins"
    SUSPICIOUS_CONSTRUCT = "suspicious_construct"
    EMPTY_QUERY = "empty_query"


@dataclass
class ValidationResult:
    """The verdict, with everything needed to explain and audit it."""

    is_safe: bool
    sql: str
    rewritten_sql: str | None = None

    violations: list[tuple[Violation, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    schemas: list[str] = field(default_factory=list)
    join_count: int = 0
    uses_analytics_view: bool = False
    tenant_injected: bool = False

    @property
    def effective_sql(self) -> str:
        """What should actually be executed."""
        return self.rewritten_sql or self.sql

    @property
    def reason(self) -> str:
        if self.is_safe:
            return "safe"
        return "; ".join(f"{v.value}: {detail}" for v, detail in self.violations)

    def summary(self) -> str:
        verdict = "ALLOWED" if self.is_safe else "BLOCKED"
        return f"{verdict} | tables={self.tables} joins={self.join_count} | {self.reason}"


# Statement types that are never permitted. Checked on the parsed tree, so a
# comment or an unusual spelling cannot disguise them.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.TruncateTable,
    exp.Grant,
)

# Constructs that indicate an attempt to reach outside the query, or to
# obfuscate. `WaitFor` is the classic time-based blind-injection primitive.
SUSPICIOUS_FUNCTIONS = {
    "openrowset",
    "opendatasource",
    "openquery",
    "openxml",
    "xp_cmdshell",
    "sp_executesql",
    "sp_oacreate",
    "sp_configure",
    "bulk_insert",
    "waitfor",
    "dbcc",
    "sp_addlogin",
    "sp_password",
}

# Second-pass regex. Defence in depth only: the parser is authoritative.
RAW_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bxp_\w+", re.I), "extended stored procedure"),
    (re.compile(r"\bsp_\w+", re.I), "system stored procedure"),
    (re.compile(r";\s*\w", re.S), "statement separator followed by more SQL"),
    (re.compile(r"\bwaitfor\s+delay\b", re.I), "WAITFOR DELAY (time-based probe)"),
    (re.compile(r"\binto\s+(outfile|dumpfile)\b", re.I), "file write"),
    (re.compile(r"/\*.*?\*/", re.S), "block comment"),
)


class SQLGuard:
    """Validates a generated query before it is allowed near the database."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.security = self.settings.security

    # -- main entry point --------------------------------------------------
    def validate(
        self,
        sql: str,
        *,
        tenant_id: int | None = None,
        enforce_tenant: bool | None = None,
        auto_repair: bool = True,
    ) -> ValidationResult:
        """Parse and check a query. Never executes anything."""
        result = ValidationResult(is_safe=False, sql=sql)

        if not sql or not sql.strip():
            result.violations.append((Violation.EMPTY_QUERY, "the query is empty"))
            return result

        # ---- 1. parse ----
        try:
            statements = sqlglot.parse(sql, read=DIALECT)
        except Exception as exc:
            result.violations.append(
                (
                    Violation.PARSE_ERROR,
                    f"could not be parsed as T-SQL: {str(exc)[:200]}",
                )
            )
            return result

        statements = [s for s in statements if isinstance(s, exp.Expression)]

        if not statements:
            result.violations.append((Violation.EMPTY_QUERY, "no statement found"))
            return result

        # ---- 2. exactly one statement ----
        if len(statements) > 1:
            kinds = ", ".join(type(s).__name__ for s in statements)
            result.violations.append(
                (
                    Violation.MULTIPLE_STATEMENTS,
                    f"{len(statements)} statements found ({kinds}); only one SELECT is allowed",
                )
            )
            return result

        statement = cast(exp.Expression, statements[0])

        # ---- 3. must be a read ----
        self._check_is_select(statement, result)
        self._check_no_write_nodes(statement, result)

        # ---- 4. what does it touch ----
        self._collect_targets(statement, result)
        self._check_schemas(result)
        self._check_columns(statement, result)
        self._check_joins(statement, result)

        # ---- 5. obfuscation and out-of-band access ----
        self._check_suspicious(statement, result)
        self._check_raw_patterns(sql, result)

        # ---- 6. tenant isolation ----
        enforce = (
            self.security.enforce_tenant_isolation if enforce_tenant is None else enforce_tenant
        )
        if enforce:
            if tenant_id is None:
                exempt = {e.lower() for e in getattr(self.security, "tenant_exempt_objects", ())}
                touches_tenant_data = any(
                    tenant_scoped_tables(select, exempt)
                    for select in statement.find_all(exp.Select)
                )
                if touches_tenant_data:
                    result.violations.append(
                        (
                            Violation.MISSING_TENANT_FILTER,
                            "tenant-scoped data requires a verified caller tenant context",
                        )
                    )
            else:
                self._check_tenant(statement, result, tenant_id)

        # A MISSING tenant predicate is repairable: adding one can only narrow
        # the result to the caller's own tenant. A WRONG tenant predicate is
        # not - that is a genuine violation and stays blocked.
        if auto_repair and tenant_id is not None:
            statement = self._try_tenant_injection(statement, result, tenant_id)

        result.is_safe = not result.violations
        if result.is_safe:
            result.rewritten_sql = self._apply_row_limit(statement)

        log.info("SQL guard: %s", result.summary())
        return result

    def _try_tenant_injection(
        self, statement: exp.Expression, result: ValidationResult, tenant_id: int
    ) -> exp.Expression:
        """Repair a missing tenant predicate by adding one.

        Measured motivation: 3 of 12 Text-to-SQL evaluation questions were
        refused solely because the model omitted this predicate. The guard was
        right to refuse; the product still failed.
        """
        missing = [
            (violation, detail)
            for violation, detail in result.violations
            if violation is Violation.MISSING_TENANT_FILTER
        ]
        if not missing or len(result.violations) != len(missing):
            # Only repair when the tenant predicate is the ONLY problem. A
            # query that is also reading a forbidden schema must stay blocked.
            return statement

        rewritten, injections = inject_tenant_predicate(
            statement, tenant_id, set(getattr(self.security, "tenant_exempt_objects", ()))
        )
        if rewritten is None:
            return statement

        # Never trust the rewriter merely because it returned an AST.  Prove
        # the same invariant again on the rewritten tree before removing the
        # original violation.
        post_check = ValidationResult(is_safe=False, sql=rewritten.sql(dialect=DIALECT))
        self._check_tenant(rewritten, post_check, tenant_id)
        if post_check.violations:
            log.warning("Tenant injection did not revalidate: %s", post_check.reason)
            return statement

        result.violations = [
            v for v in result.violations if v[0] is not Violation.MISSING_TENANT_FILTER
        ]
        result.tenant_injected = True
        result.warnings.append(
            "tenant filter was missing and has been added automatically: " + "; ".join(injections)
        )
        log.info("Injected tenant predicate: %s", injections)
        return rewritten

    # -- individual checks -------------------------------------------------
    def _check_is_select(self, statement: exp.Expression, result: ValidationResult) -> None:
        # A CTE parses as Select with a `with` arg, so both are reads.
        if not isinstance(statement, (exp.Select, exp.Union, exp.Subquery)):
            result.violations.append(
                (
                    Violation.NOT_A_SELECT,
                    f"statement is {type(statement).__name__}, not a SELECT",
                )
            )

    def _check_no_write_nodes(self, statement: exp.Expression, result: ValidationResult) -> None:
        """Any write node anywhere in the tree, including inside a subquery."""
        for node_type in FORBIDDEN_NODES:
            found = list(statement.find_all(node_type))
            if found:
                result.violations.append(
                    (
                        Violation.WRITE_OPERATION,
                        f"contains a {node_type.__name__.upper()} operation",
                    )
                )

        # SELECT ... INTO creates a table. It parses as a Select, so the
        # statement-type check above does not catch it.
        if isinstance(statement, exp.Select) and statement.args.get("into"):
            result.violations.append(
                (
                    Violation.WRITE_OPERATION,
                    "SELECT ... INTO creates a table",
                )
            )

    def _collect_targets(self, statement: exp.Expression, result: ValidationResult) -> None:
        tables: set[str] = set()
        schemas: set[str] = set()

        # CTE names are not real tables and must not be treated as such.
        cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}

        for table in statement.find_all(exp.Table):
            name = table.name
            if not name or name.lower() in cte_names:
                continue
            schema = table.db or ""
            qualified = f"{schema}.{name}" if schema else name
            tables.add(qualified)
            if schema:
                schemas.add(schema.lower())

        result.tables = sorted(tables)
        result.schemas = sorted(schemas)
        result.uses_analytics_view = any(t.lower().startswith("analytics.") for t in tables)

        if not result.uses_analytics_view and tables:
            result.warnings.append(
                "query reads base tables directly; the curated analytics views "
                "encode the business definitions and are preferred"
            )

    def _check_schemas(self, result: ValidationResult) -> None:
        allowed = {s.lower() for s in self.security.allowed_schemas}
        for schema in result.schemas:
            if schema not in allowed:
                result.violations.append(
                    (
                        Violation.FORBIDDEN_SCHEMA,
                        f"schema '{schema}' is not readable (allowed: {', '.join(sorted(allowed))})",
                    )
                )

        # An unqualified table name cannot be checked against the schema
        # allow-list, so it is refused rather than assumed safe.
        for table in result.tables:
            if "." not in table:
                result.violations.append(
                    (
                        Violation.FORBIDDEN_SCHEMA,
                        f"table '{table}' is not schema-qualified; "
                        f"use analytics.<view> or <schema>.<table>",
                    )
                )

    def _check_columns(self, statement: exp.Expression, result: ValidationResult) -> None:
        blocked = {c.lower() for c in self.security.blocked_columns}
        seen: set[str] = set()

        for column in statement.find_all(exp.Column):
            name = (column.name or "").lower()
            if name:
                seen.add(name)
            if name in blocked:
                result.violations.append(
                    (
                        Violation.FORBIDDEN_COLUMN,
                        f"column '{column.name}' is not readable",
                    )
                )

        # `SELECT *` could pull a blocked column without ever naming it.
        if any(isinstance(e, exp.Star) for e in statement.find_all(exp.Star)):
            result.warnings.append(
                "SELECT * may expose columns that were never named; "
                "the result is filtered again after execution"
            )

        result.columns = sorted(seen)

    def _check_joins(self, statement: exp.Expression, result: ValidationResult) -> None:
        result.join_count = len(list(statement.find_all(exp.Join)))
        if result.join_count > self.security.max_sql_joins:
            result.violations.append(
                (
                    Violation.TOO_MANY_JOINS,
                    f"{result.join_count} joins exceeds the limit of "
                    f"{self.security.max_sql_joins}; this is usually a runaway query",
                )
            )

    def _check_suspicious(self, statement: exp.Expression, result: ValidationResult) -> None:
        for function in statement.find_all(exp.Anonymous):
            name = (function.this or "").lower() if isinstance(function.this, str) else ""
            if name in SUSPICIOUS_FUNCTIONS:
                result.violations.append(
                    (
                        Violation.SUSPICIOUS_CONSTRUCT,
                        f"function '{name}' is not permitted",
                    )
                )

        for command in statement.find_all(exp.Command):
            result.violations.append(
                (
                    Violation.SUSPICIOUS_CONSTRUCT,
                    f"raw command '{str(command)[:60]}' is not permitted",
                )
            )

    def _check_raw_patterns(self, sql: str, result: ValidationResult) -> None:
        """Second pass over the raw text.

        The parser is authoritative; this catches things that parse cleanly but
        should still be refused, and things a future sqlglot version might
        represent differently. A block comment is flagged rather than blocked,
        because comments are legitimate and their danger is hiding a second
        statement, which the statement-count check already prevents.
        """
        for pattern, description in RAW_PATTERNS:
            if not pattern.search(sql):
                continue
            if description == "block comment":
                result.warnings.append("query contains a block comment")
                continue
            if description == "statement separator followed by more SQL":
                # Already definitively handled by the parse-based count.
                continue
            result.violations.append(
                (Violation.SUSPICIOUS_CONSTRUCT, f"raw text contains {description}")
            )

    def _check_tenant(
        self, statement: exp.Expression, result: ValidationResult, tenant_id: int
    ) -> None:
        """Require a tenant predicate on any query touching tenant-scoped data.

        Checked on the tree rather than by searching the text, so a tenant_id
        mentioned inside a string literal or a comment does not satisfy it.
        """
        exempt = {o.lower() for o in getattr(self.security, "tenant_exempt_objects", ())}
        exempt = {e.lower() for e in getattr(self.security, "tenant_exempt_objects", ())}

        def is_conjunctive(equality: exp.EQ, owner: exp.Select) -> bool:
            """The equality must not be weakened by OR or inverted by NOT."""
            node = equality.parent
            while node is not None and node is not owner:
                if isinstance(node, (exp.Or, exp.Not)):
                    return False
                node = node.parent
            return node is owner

        for select in statement.find_all(exp.Select):
            tables = tenant_scoped_tables(select, exempt)
            if not tables:
                continue

            clauses = filtering_clauses(select)
            tenant_columns = [
                column
                for clause in clauses
                for column in clause.find_all(exp.Column)
                if column.find_ancestor(exp.Select) is select
                and (column.name or "").lower() == TENANT_COLUMN
            ]
            multiple_sources = len({table.alias_or_name for table in tables}) > 1

            for table in tables:
                qualifier = (qualifier_for(select, table) or table.alias_or_name or "").lower()
                restricted = False

                for clause in clauses:
                    for equality in clause.find_all(exp.EQ):
                        if equality.find_ancestor(exp.Select) is not select:
                            continue
                        if not is_conjunctive(equality, select):
                            continue
                        for side, other in (
                            (equality.left, equality.right),
                            (equality.right, equality.left),
                        ):
                            if not isinstance(side, exp.Column):
                                continue
                            if (side.name or "").lower() != TENANT_COLUMN:
                                continue
                            column_qualifier = (side.table or "").lower()
                            if multiple_sources and column_qualifier != qualifier:
                                continue
                            if not multiple_sources and column_qualifier not in (
                                "",
                                qualifier,
                                (table.name or "").lower(),
                            ):
                                continue
                            if isinstance(other, exp.Literal) and str(other.this) == str(tenant_id):
                                restricted = True
                                break
                        if restricted:
                            break
                    if restricted:
                        break

                if restricted:
                    continue

                target = f"{table.db}.{table.name}"
                if tenant_columns:
                    result.violations.append(
                        (
                            Violation.CROSS_TENANT,
                            f"{target} is not unconditionally restricted to tenant_id = {tenant_id}",
                        )
                    )
                else:
                    result.violations.append(
                        (
                            Violation.MISSING_TENANT_FILTER,
                            f"{target} has no tenant_id = {tenant_id} predicate",
                        )
                    )

    def _apply_row_limit(self, statement: exp.Expression) -> str | None:
        """Add TOP (n) when the query has no explicit limit.

        Rewriting the parsed tree rather than the text means this cannot be
        defeated by unusual formatting, and it produces valid T-SQL by
        construction.
        """
        max_rows = self.settings.database.max_result_rows
        if max_rows <= 0 or not isinstance(statement, exp.Select):
            return None
        if statement.args.get("limit") or statement.args.get("offset"):
            return None

        limited = statement.copy()
        limited.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        try:
            return limited.sql(dialect=DIALECT)
        except Exception as exc:  # never let a rewrite failure block a safe query
            log.warning("Could not apply the row limit: %s", exc)
            return None


def validate_sql(sql: str, *, tenant_id: int | None = None) -> ValidationResult:
    """Convenience wrapper using process settings."""
    return SQLGuard().validate(sql, tenant_id=tenant_id)


__all__ = ["SQLGuard", "ValidationResult", "Violation", "validate_sql"]
