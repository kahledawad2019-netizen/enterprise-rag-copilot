"""
SQL guardrails.

An LLM writes the SQL here, so the SQL is untrusted input. Two layers protect
the database:

  Layer 1 (this module) - refuse anything that is not a read, refuse batched
                          statements, and cap the number of rows returned.
  Layer 2 (the server)  - connect with a least-privilege login that only has
                          db_datareader. See README "Least-privilege login".

Layer 1 alone is NOT sufficient. Always do layer 2 as well.
"""

from __future__ import annotations

import re

# Statements that must never be executed by the agent.
FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE",
    "ALTER", "GRANT", "REVOKE", "DENY", "EXEC", "EXECUTE", "SP_", "XP_",
    "BACKUP", "RESTORE", "SHUTDOWN", "RECONFIGURE", "KILL", "BULK",
    "OPENROWSET", "OPENDATASOURCE", "OPENQUERY", "WAITFOR",
}

# The only tokens an outermost read query may begin with.
ALLOWED_STARTS = ("SELECT", "WITH")


class UnsafeSQLError(Exception):
    """Raised when generated SQL violates the read-only policy."""


def strip_sql_comments(sql: str) -> str:
    """Remove -- line comments and /* block */ comments.

    Done before keyword checks so that `SELECT 1 /*x*/; DROP TABLE t` cannot
    hide a second statement behind a comment.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _strip_string_literals(sql: str) -> str:
    """Blank out 'quoted text' so a literal like 'please DROP it' is ignored."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def assert_read_only(sql: str) -> None:
    """Raise UnsafeSQLError unless `sql` is a single, read-only statement."""
    if not sql or not sql.strip():
        raise UnsafeSQLError("Empty SQL statement.")

    cleaned = _strip_string_literals(strip_sql_comments(sql)).strip()

    # Reject statement batching: anything after the first `;` must be blank.
    head, _, tail = cleaned.partition(";")
    if tail.strip():
        raise UnsafeSQLError(
            "Multiple SQL statements are not allowed (found ';' followed by "
            "more SQL)."
        )
    cleaned = head.strip()

    upper = cleaned.upper()

    if not upper.startswith(ALLOWED_STARTS):
        raise UnsafeSQLError(
            f"Only SELECT / WITH queries are allowed, got: {cleaned[:60]!r}"
        )

    # `SELECT ... INTO newtable` writes data - block it.
    if re.search(r"\bINTO\s+[\w\[\]#.]+", upper):
        raise UnsafeSQLError("SELECT ... INTO creates a table and is not allowed.")

    for kw in FORBIDDEN_KEYWORDS:
        pattern = rf"\b{re.escape(kw)}" if kw.endswith("_") else rf"\b{re.escape(kw)}\b"
        if re.search(pattern, upper):
            raise UnsafeSQLError(f"Forbidden keyword in generated SQL: {kw}")


def is_read_only(sql: str) -> bool:
    try:
        assert_read_only(sql)
        return True
    except UnsafeSQLError:
        return False


def apply_row_limit(sql: str, max_rows: int) -> str:
    """Inject `TOP (max_rows)` into a plain outer SELECT when absent.

    Queries starting with a CTE (`WITH`) are returned untouched - rewriting
    those safely is not worth the risk, and `run_sql` truncates the resulting
    DataFrame anyway as a backstop.
    """
    if max_rows <= 0:
        return sql

    stripped = strip_sql_comments(sql).strip().rstrip(";")
    upper = stripped.upper()

    if upper.startswith("WITH"):
        return sql
    # Already limited by the model.
    if re.search(r"^\s*SELECT\s+(DISTINCT\s+)?TOP\b", upper) or " OFFSET " in upper:
        return sql

    return re.sub(
        r"^(\s*SELECT\s+)(DISTINCT\s+)?",
        lambda m: f"{m.group(1)}{m.group(2) or ''}TOP ({max_rows}) ",
        stripped,
        count=1,
        flags=re.IGNORECASE,
    )
