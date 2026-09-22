"""
The Text-to-SQL provider interface.

Vanna is the specified default, but ADR-002 records why nothing safety-critical
may depend on it: it is a rewritten library with a misleading `__version__`, a
dialect-breaking defect in its SQL extractor, and an upstream repository
reported archived.

So the application owns this interface, and a provider is responsible for
**generation only**:

    generate_query   -> the provider writes SQL
    validate_query   -> OUR guard, always
    execute_query    -> OUR runner, always
    explain_result   -> the provider narrates the rows
    get_trace_metadata -> what the observability layer records

`validate_query` and `execute_query` are implemented once on the base class and
are deliberately not overridable in spirit: a provider that could validate its
own SQL could approve its own SQL.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..database.read_only_runner import QueryResult, ReadOnlyRunner
from ..security.sql_guard import SQLGuard, ValidationResult
from .schema_checks import validate_against_schema
from .schema_retriever import SchemaRetriever, SQLContext

log = logging.getLogger(__name__)


@dataclass
class SQLRequest:
    """One Text-to-SQL request."""

    question: str
    tenant_id: int | None = None
    app_user: str | None = None
    trace_id: str = ""
    allow_base_tables: bool = True


@dataclass
class GeneratedSQL:
    """SQL produced by a provider, before validation."""

    sql: str
    question: str
    provider: str
    context: SQLContext | None = None
    raw_response: str = ""
    generation_ms: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.sql.strip()


class TextToSQLProvider(ABC):
    """Base class. Generation varies; safety does not."""

    name: str = "base"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        schema_retriever: SchemaRetriever | None = None,
        runner: ReadOnlyRunner | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.schema = schema_retriever or SchemaRetriever(self.settings)
        self.guard = SQLGuard(self.settings)
        self.runner = runner or ReadOnlyRunner(self.settings, guard=self.guard)
        self._last_trace: dict[str, Any] = {}

    # -- provider-specific -------------------------------------------------
    @abstractmethod
    def generate_query(self, request: SQLRequest) -> GeneratedSQL: ...

    @abstractmethod
    def explain_result(self, question: str, result: QueryResult) -> str: ...

    # -- shared, and deliberately not provider-specific --------------------
    def generate_and_repair(self, request: SQLRequest) -> GeneratedSQL:
        """Generate, then fix anything the schema says cannot run.

        Lives on the base class, not in a provider, because it applies to every
        provider equally - and because putting it only in the native provider
        meant the default (Vanna) never got it, which is exactly the bug this
        was written to fix.
        """
        generated = self.generate_query(request)
        if generated.is_empty or generated.context is None:
            return generated

        # The full catalog is passed so column types can be resolved for tables
        # the query references but the retriever did not offer - which is
        # exactly how the SUM(invoice_number) failure slipped through.
        catalog = self.schema.load_catalog()
        problems = validate_against_schema(generated.sql, generated.context, catalog)
        if not problems:
            return generated

        log.info("Generated SQL will not run (%s); attempting one repair", problems)
        generated.warnings.append("first attempt was invalid: " + "; ".join(problems))

        repaired = self._repair_sql(generated.sql, problems)
        if repaired:
            remaining = validate_against_schema(repaired, generated.context, catalog)
            generated.sql = repaired
            generated.warnings.append(
                "SQL was repaired automatically" if not remaining
                else "repair attempted but problems remain: " + "; ".join(remaining)
            )
        return generated

    def _repair_sql(self, sql: str, problems: list[str]) -> str:
        """One corrective LLM pass with the specific problem fed back."""
        from .native import REPAIR_PROMPT, SYSTEM_PROMPT, extract_sql

        try:
            import ollama

            client = ollama.Client(
                self.settings.ollama.host, timeout=self.settings.ollama.timeout_seconds
            )
            response = client.chat(
                model=self.settings.chat_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": REPAIR_PROMPT.format(
                        sql=sql, problems=chr(10).join(f"- {p}" for p in problems))},
                ],
                options={"temperature": 0.0, "num_predict": 600,
                         "num_ctx": self.settings.profile.chat_context_tokens},
                keep_alive=self.settings.ollama.keep_alive,
            )
            return extract_sql(response["message"]["content"])
        except Exception as exc:  # noqa: BLE001
            log.warning("SQL repair attempt failed: %s", exc)
            return ""

    def validate_query(self, sql: str, *, tenant_id: int | None = None) -> ValidationResult:
        """Always our guard. A provider never validates its own output."""
        return self.guard.validate(sql, tenant_id=tenant_id)

    def execute_query(
        self, sql: str, *, request: SQLRequest, approved: bool = False
    ) -> QueryResult:
        """Always our runner: validation, audit, timeout, row cap, redaction."""
        return self.runner.run(
            sql,
            tenant_id=request.tenant_id,
            app_user=request.app_user,
            trace_id=request.trace_id,
            question=request.question,
            approved=approved,
        )

    def get_trace_metadata(self) -> dict[str, Any]:
        return {"provider": self.name, **self._last_trace}

    def _record_trace(self, **fields: Any) -> None:
        self._last_trace = fields


def build_provider(
    settings: Settings | None = None, *, name: str | None = None
) -> TextToSQLProvider:
    """Construct the configured provider, falling back rather than failing.

    Vanna is the default per the specification, and the choice now comes from
    `TEXT_TO_SQL_PROVIDER` rather than a literal in this function. That matters:
    while the default lived here, `scripts/evaluate_text_to_sql.py` defaulted to
    `native` and the two disagreed silently - the published Text-to-SQL numbers
    described a provider the application was not running.

    If the configured provider is not installed, the native provider is used and
    the substitution is logged: the system keeps working, and the reason is
    visible rather than mysterious.
    """
    settings = settings or get_settings()
    # The configured provider, not a literal. An explicit `name` still wins, so
    # the evaluation harness can compare implementations on demand.
    requested = (name or settings.text_to_sql_provider or "vanna").lower()

    if requested == "native":
        from .native import NativeTextToSQLProvider

        return NativeTextToSQLProvider(settings)

    try:
        from .vanna_provider import VannaTextToSQLProvider

        return VannaTextToSQLProvider(settings)
    except ImportError as exc:
        log.warning(
            "Vanna is unavailable (%s); using the native provider. "
            'Install it with: pip install -e ".[vanna]"', exc,
        )
        from .native import NativeTextToSQLProvider

        return NativeTextToSQLProvider(settings)


__all__ = [
    "GeneratedSQL", "SQLRequest", "TextToSQLProvider", "build_provider",
]
