"""
Native Text-to-SQL provider.

The reference implementation and the fallback. It uses the same schema
retriever, business glossary, approved examples, SQL guard and read-only runner
as the Vanna provider, so an A/B comparison between the two measures *only the
generation step*, which is the whole point of having both.

It is also the thing that keeps this system working if Vanna breaks, which
ADR-002 argues is a realistic possibility rather than a hypothetical one.
"""

from __future__ import annotations

import logging
import re
import time

from ..config import Settings, get_settings
from ..database.read_only_runner import QueryResult
from .provider import GeneratedSQL, SQLRequest, TextToSQLProvider
from .schema_retriever import SQLContext

log = logging.getLogger(__name__)

SQL_PROMPT_VERSION = "1.0.0"


SYSTEM_PROMPT = """You write Microsoft SQL Server (T-SQL) queries. You output SQL and nothing else.

RULES
- Output exactly ONE SELECT statement. Never INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, \
MERGE, TRUNCATE or EXEC.
- Always schema-qualify: [analytics].[vw_customer_360], not customers.
- Use TOP (n). LIMIT does not exist in T-SQL.
- Prefer the analytics views. They already encode the official business definitions; \
recomputing a metric from base tables is how the wrong number gets produced.
- Follow the BUSINESS DEFINITIONS exactly. They override any assumption you would \
otherwise make about what a metric means.
- Use only the tables and columns given in the SCHEMA section. Never invent a column.
- Only SUM() or AVG() a NUMERIC column. Identifier columns such as invoice_number, customer_code or ticket_number are text: use COUNT() on those, never SUM().
- For money questions use total_amount, subtotal_amount, amount, mrr_amount or current_arr - never an id or a code column.
- Guard division with NULLIF to avoid divide-by-zero.
- Output raw SQL only: no explanation, no markdown fences, no commentary."""


USER_PROMPT = """SCHEMA
{schema}

RELATIONSHIPS
{relationships}

BUSINESS DEFINITIONS (authoritative - follow these, not your own assumptions)
{glossary}

APPROVED EXAMPLES
{examples}
{tenant_rule}
QUESTION
{question}

Write the T-SQL SELECT that answers it. Output only SQL."""


TENANT_RULE = """
TENANT RESTRICTION (mandatory)
This user may only see data for tenant_id = {tenant_id}. Every query reading a \
tenant-scoped table or view MUST include `tenant_id = {tenant_id}` in its WHERE clause.

EXCEPTION - these objects have NO tenant_id column and must NOT be filtered on it. \
Adding `tenant_id` to them produces SQL that fails with "Invalid column name":
{exempt_objects}
"""


EXPLAIN_PROMPT = """A user asked: {question}

This SQL was run against the company database:
{sql}

It returned {row_count} row(s):
{rows}

Summarise the answer in two or three sentences of plain English. State the numbers from \
the result. Do not invent figures that are not shown, do not describe the SQL, and do not \
speculate about causes unless the data shows them."""


REPAIR_PROMPT = """The SQL you produced cannot run. Fix it.

YOUR SQL:
{sql}

WHAT IS WRONG:
{problems}

Rewrite the query so it is correct. Output only the corrected T-SQL SELECT, nothing else."""


class NativeTextToSQLProvider(TextToSQLProvider):
    name = "native"

    def __init__(self, settings: Settings | None = None, **kwargs) -> None:
        super().__init__(settings or get_settings(), **kwargs)
        from ..llm import build_chat_client

        self._client = build_chat_client(self.settings)

    # -- generation --------------------------------------------------------
    def generate_query(self, request: SQLRequest) -> GeneratedSQL:
        started = time.perf_counter()

        context = self.schema.build_context(request.question, tenant_id=request.tenant_id)
        prompt = self._build_prompt(context, request)

        try:
            response = self._client.chat(
                model=self.settings.chat_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                options={
                    "num_ctx": self.settings.profile.chat_context_tokens,
                    "temperature": 0.0,   # SQL generation wants determinism
                    "num_predict": 600,
                },
                keep_alive=self.settings.ollama.keep_alive,
            )
        except Exception as exc:
            raise RuntimeError(
                f"SQL generation failed with {self.settings.chat_model!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        raw = response["message"]["content"]
        sql = extract_sql(raw)

        generated = GeneratedSQL(
            sql=sql,
            question=request.question,
            provider=self.name,
            context=context,
            raw_response=raw,
            generation_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.get("prompt_eval_count"),
            tokens_out=response.get("eval_count"),
        )
        if generated.is_empty:
            generated.warnings.append("no SQL could be extracted from the model response")

        # Repair is handled by TextToSQLProvider.generate_and_repair, so every
        # provider benefits rather than only this one.

        self._record_trace(
            prompt_version=SQL_PROMPT_VERSION,
            model=self.settings.chat_model,
            tables_offered=context.table_names(),
            glossary_terms=[g["term"] for g in context.glossary],
            examples_used=len(context.examples),
            generation_ms=generated.generation_ms,
        )
        return generated

    def _build_prompt(self, context: SQLContext, request: SQLRequest) -> str:
        tenant_rule = ""
        if request.tenant_id is not None and self.settings.security.enforce_tenant_isolation:
            exempt = getattr(self.settings.security, "tenant_exempt_objects", ())
            exempt_list = "\n".join(f"  - {o}" for o in exempt) or "  (none)"
            tenant_rule = TENANT_RULE.format(
                tenant_id=request.tenant_id, exempt_objects=exempt_list
            )

        return USER_PROMPT.format(
            schema=context.render_schema(),
            relationships=context.render_relationships(),
            glossary=context.render_glossary(),
            examples=context.render_examples(),
            tenant_rule=tenant_rule,
            question=context.question,
        )

    def _repair(self, sql: str, problems: list[str]) -> str:
        """One corrective pass, with the specific problem fed back."""
        try:
            response = self._client.chat(
                model=self.settings.chat_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": REPAIR_PROMPT.format(
                        sql=sql, problems="\n".join(f"- {p}" for p in problems),
                    )},
                ],
                options={
                    "num_ctx": self.settings.profile.chat_context_tokens,
                    "temperature": 0.0, "num_predict": 600,
                },
                keep_alive=self.settings.ollama.keep_alive,
            )
            return extract_sql(response["message"]["content"])
        except Exception as exc:  # noqa: BLE001
            log.warning("SQL repair attempt failed: %s", exc)
            return ""

    # -- explanation -------------------------------------------------------
    def explain_result(self, question: str, result: QueryResult) -> str:
        prompt = EXPLAIN_PROMPT.format(
            question=question,
            sql=result.executed_sql,
            row_count=result.row_count,
            rows=result.preview(limit=15),
        )
        try:
            response = self._client.chat(
                model=self.settings.chat_model,
                messages=[{"role": "user", "content": prompt}],
                options={
                    "num_ctx": self.settings.profile.chat_context_tokens,
                    "temperature": 0.1,
                    "num_predict": 300,
                },
                keep_alive=self.settings.ollama.keep_alive,
            )
            return response["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("Result explanation failed: %s", exc)
            return f"The query returned {result.row_count} row(s)."


# ---------------------------------------------------------------------------
# SQL extraction
# ---------------------------------------------------------------------------
FENCED = re.compile(r"```(?:sql|tsql)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)
STATEMENT_START = re.compile(r"\b(WITH|SELECT)\b", re.IGNORECASE)


def extract_sql(response: str) -> str:
    """Pull the SQL out of a model response.

    Written from scratch rather than reused from Vanna, whose version cuts the
    response at the first `[` and therefore destroys any query using
    bracket-quoted identifiers - which is ordinary T-SQL. See ADR-002.
    """
    if not response:
        return ""

    text = response.strip()

    fenced = FENCED.search(text)
    candidate = fenced.group(1) if fenced else None

    if candidate is None:
        start = STATEMENT_START.search(text)
        if not start:
            return ""
        candidate = text[start.start():]

    # Keep the first statement only; drop a trailing semicolon.
    candidate = candidate.split(";")[0].strip()

    # Drop trailing prose the model appended after a blank line.
    candidate = re.split(r"\n\s*\n(?=[A-Z][a-z])", candidate)[0].strip()

    return candidate


__all__ = ["NativeTextToSQLProvider", "SQL_PROMPT_VERSION", "extract_sql"]
