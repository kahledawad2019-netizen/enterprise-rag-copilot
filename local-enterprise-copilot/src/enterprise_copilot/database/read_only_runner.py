"""
Read-only query execution.

Every generated query reaches the database through this one function, so the
safety policy cannot be bypassed by a caller that forgot to apply it. The
ordering matters:

    validate -> audit(attempt) -> execute with timeout -> cap rows
             -> redact columns -> audit(outcome)

The audit event is written **before** execution, not after. A query that hangs,
crashes the process, or is killed still leaves a record that it was attempted;
an audit written only on success is an audit that misses exactly the events
worth investigating.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import Settings, get_settings
from ..security.sql_guard import SQLGuard, ValidationResult, Violation
from .connection import raw_connection

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

log = logging.getLogger(__name__)


class QueryBlockedError(RuntimeError):
    """The guard refused the query. Carries the validation result for the UI."""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__(f"Query blocked: {result.reason}")
        self.result = result


class QueryExecutionError(RuntimeError):
    """The query was safe but failed at the server."""


@dataclass
class QueryResult:
    """The outcome of a validated, executed query."""

    sql: str
    executed_sql: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    redacted_columns: list[str] = field(default_factory=list)

    duration_ms: float = 0.0
    trace_id: str = ""
    validation: ValidationResult | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dataframe(self) -> pd.DataFrame:
        import pandas as pd

        return pd.DataFrame(self.rows, columns=self.columns or None)

    def preview(self, limit: int = 10) -> str:
        """Compact text rendering for a prompt or a CLI."""
        if not self.rows:
            return "(no rows)"
        shown = self.rows[:limit]
        header = " | ".join(self.columns)
        body = "\n".join(
            " | ".join("" if r.get(c) is None else str(r.get(c)) for c in self.columns)
            for r in shown
        )
        footer = ""
        if self.row_count > limit:
            footer = f"\n... {self.row_count - limit} more rows"
        return f"{header}\n{'-' * len(header)}\n{body}{footer}"


class ReadOnlyRunner:
    """The only permitted path from generated SQL to the database."""

    def __init__(self, settings: Settings | None = None, *, guard: SQLGuard | None = None) -> None:
        self.settings = settings or get_settings()
        self.guard = guard or SQLGuard(self.settings)

    def run(
        self,
        sql: str,
        *,
        tenant_id: int | None = None,
        app_user: str | None = None,
        trace_id: str | None = None,
        question: str | None = None,
        require_approval: bool | None = None,
        approved: bool = False,
    ) -> QueryResult:
        """Validate and execute. Raises rather than returning partial results."""
        trace_id = trace_id or uuid.uuid4().hex[:12]

        validation = self.guard.validate(sql, tenant_id=tenant_id)

        if not validation.is_safe:
            self._audit(
                trace_id=trace_id,
                app_user=app_user,
                tenant_id=tenant_id,
                question=question,
                sql=sql,
                allowed=False,
                block_reason=validation.reason,
            )
            raise QueryBlockedError(validation)

        needs_approval = (
            self.settings.security.require_sql_approval
            if require_approval is None
            else require_approval
        )
        if needs_approval and not approved:
            self._audit(
                trace_id=trace_id,
                app_user=app_user,
                tenant_id=tenant_id,
                question=question,
                sql=sql,
                allowed=False,
                block_reason="awaiting human approval",
            )
            raise QueryBlockedError(
                ValidationResult(
                    is_safe=False,
                    sql=sql,
                    violations=[
                        (
                            Violation.SUSPICIOUS_CONSTRUCT,
                            "approval required before execution",
                        )
                    ],
                    warnings=["the query passed validation; it awaits human approval"],
                )
            )

        executed_sql = validation.effective_sql

        # Written before execution: a query that hangs must still be recorded.
        self._audit(
            trace_id=trace_id,
            app_user=app_user,
            tenant_id=tenant_id,
            question=question,
            sql=executed_sql,
            allowed=True,
        )

        started = time.perf_counter()
        try:
            rows, columns = self._execute(executed_sql, tenant_id=tenant_id)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            self._audit(
                trace_id=trace_id,
                app_user=app_user,
                tenant_id=tenant_id,
                question=question,
                sql=executed_sql,
                allowed=True,
                error_category=type(exc).__name__,
                duration_ms=duration_ms,
            )
            raise QueryExecutionError(
                f"The query was safe but failed at the server: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc

        duration_ms = (time.perf_counter() - started) * 1000
        result = QueryResult(
            sql=sql,
            executed_sql=executed_sql,
            rows=rows,
            columns=columns,
            row_count=len(rows),
            duration_ms=duration_ms,
            trace_id=trace_id,
            validation=validation,
            warnings=list(validation.warnings),
        )

        self._cap_rows(result)
        self._redact(result)

        self._audit(
            trace_id=trace_id,
            app_user=app_user,
            tenant_id=tenant_id,
            question=question,
            sql=executed_sql,
            allowed=True,
            row_count=result.row_count,
            duration_ms=duration_ms,
        )
        log.info("Query ok: %d rows in %.0f ms (trace %s)", result.row_count, duration_ms, trace_id)
        return result

    # -- internals ---------------------------------------------------------
    def _execute(
        self, sql: str, *, tenant_id: int | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        with raw_connection(self.settings) as conn:
            cursor = conn.cursor()
            if self.settings.database_backend == "postgresql":
                # SET LOCAL is transaction-scoped, so a pooled connection can
                # never leak one caller's tenant into the next request.
                timeout_ms = self.settings.database.query_timeout_seconds * 1000
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(timeout_ms),),
                )
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    ("" if tenant_id is None else str(tenant_id),),
                )
            else:
                # In pyodbc timeout is a CONNECTION property, not a cursor
                # property. SQL Server session context is explicitly cleared
                # because pooling can otherwise retain the previous caller.
                conn.timeout = self.settings.database.query_timeout_seconds
                cursor.execute(
                    "EXEC sys.sp_set_session_context @key=N'tenant_id', @value=?",
                    tenant_id,
                )
            cursor.execute(sql)

            if cursor.description is None:
                return [], []

            columns = [c[0] for c in cursor.description]
            maximum = self.settings.database.max_result_rows
            fetched = cursor.fetchall() if maximum <= 0 else cursor.fetchmany(maximum + 1)
            rows = [
                {column: _coerce(value) for column, value in zip(columns, row, strict=True)}
                for row in fetched
            ]
            return rows, columns

    def _cap_rows(self, result: QueryResult) -> None:
        """Backstop for the guard's TOP rewrite.

        The rewrite is skipped for CTEs and for queries that already carry a
        limit, so the cap is also enforced here on the materialised rows.
        """
        maximum = self.settings.database.max_result_rows
        if maximum > 0 and result.row_count > maximum:
            result.rows = result.rows[:maximum]
            result.row_count = maximum
            result.truncated = True
            result.warnings.append(f"result truncated to {maximum} rows")

    def _redact(self, result: QueryResult) -> None:
        """Remove blocked columns that arrived anyway.

        The guard refuses a query that *names* a blocked column, but
        `SELECT *` over a view can return one without naming it. This is the
        second half of that check, applied to the data rather than the query.
        """
        blocked = {c.lower() for c in self.settings.security.blocked_columns}
        present = [c for c in result.columns if c.lower() in blocked]
        if not present:
            return

        for row in result.rows:
            for column in present:
                row[column] = "[REDACTED]"
        result.redacted_columns = present
        result.warnings.append(f"redacted sensitive column(s): {', '.join(present)}")
        log.warning("Redacted columns in result: %s", present)

    def _audit(
        self,
        *,
        trace_id: str,
        app_user: str | None,
        tenant_id: int | None,
        question: str | None,
        sql: str,
        allowed: bool,
        block_reason: str | None = None,
        row_count: int | None = None,
        duration_ms: float | None = None,
        error_category: str | None = None,
    ) -> None:
        """Record the attempt in ai.audit_events.

        Auditing must never break the request: if the audit insert fails, the
        failure is logged and the query proceeds. An unavailable audit table is
        an operational problem, not a reason to deny a legitimate question.
        """
        try:
            with raw_connection(self.settings, autocommit=True) as conn:
                statement = """
                    INSERT INTO ai.audit_events
                        (trace_id, app_user, tenant_id, route, question, generated_sql,
                         sql_allowed, block_reason, row_count, duration_ms, model_name,
                         error_category)
                    VALUES ({placeholders})
                    """
                values = (
                    trace_id,
                    app_user,
                    tenant_id,
                    "text_to_sql",
                    question,
                    sql,
                    # `ai.audit_events.sql_allowed` is BIT on SQL Server and
                    # BOOLEAN on PostgreSQL. Sending 1/0 worked on the former
                    # and psycopg refuses to cast an integer parameter to
                    # BOOLEAN, so every audit write on PostgreSQL failed with
                    # "you will need to rewrite or cast the expression" - and
                    # because auditing deliberately never breaks a request,
                    # the failure was a log line nobody read while the audit
                    # trail silently recorded nothing.
                    #
                    # A Python bool maps correctly in both drivers: psycopg to
                    # BOOLEAN, pyodbc to BIT. This is the same BIT-versus-
                    # BOOLEAN mismatch already fixed in synthetic_loader.py;
                    # this site was missed because nothing checked that the
                    # audit row actually arrived.
                    bool(allowed),
                    block_reason,
                    row_count,
                    int(duration_ms) if duration_ms is not None else None,
                    self.settings.chat_model,
                    error_category,
                )
                cursor = conn.cursor()
                if self.settings.database_backend == "postgresql":
                    cursor.execute(statement.format(placeholders=", ".join(["%s"] * 12)), values)
                else:
                    cursor.execute(statement.format(placeholders=", ".join(["?"] * 12)), *values)
        except Exception as exc:
            log.warning("Audit write failed (query still proceeds): %s", exc)


def _coerce(value: Any) -> Any:
    """Make driver types JSON-safe for the UI and the prompt."""
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


__all__ = ["QueryBlockedError", "QueryExecutionError", "QueryResult", "ReadOnlyRunner"]
