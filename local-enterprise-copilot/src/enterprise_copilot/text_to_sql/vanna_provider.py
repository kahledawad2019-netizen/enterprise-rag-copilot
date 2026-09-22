"""
Vanna Text-to-SQL provider — the specified default.

Read ADR-002 before changing anything here. The short version:

* The installed distribution is **vanna 2.0.2**, not the 0.7.x that every
  tutorial targets. Its `__version__` reports `"0.1.0"`, which is wrong; use
  `importlib.metadata.version("vanna")`.
* The classic API survives at `vanna.legacy.*` and is what this uses. The 2.x
  agent framework would take ownership of routing and tool execution, which
  this project implements itself and needs to keep testable.
* `vanna.legacy.ollama.Ollama.extract_sql` cuts the model response at the first
  `[`, which silently truncates any query using bracket-quoted identifiers —
  ordinary T-SQL. It is overridden below and covered by a regression test.

Vanna is used for **generation only**. Validation and execution go through the
application's guard and runner, exactly as they do for the native provider, so
no safety property depends on Vanna behaving correctly.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings, get_settings
from ..database.read_only_runner import QueryResult
from .native import EXPLAIN_PROMPT, extract_sql
from .provider import GeneratedSQL, SQLRequest, TextToSQLProvider

log = logging.getLogger(__name__)


def installed_vanna_version() -> str:
    """The real distribution version. `vanna.__version__` is unreliable."""
    import importlib.metadata as metadata

    try:
        return metadata.version("vanna")
    except metadata.PackageNotFoundError:
        return "not installed"


def _build_vanna_class():
    """Compose Vanna's Chroma store and Ollama client, with our fixes applied.

    Built lazily inside a function so importing this module does not require
    Vanna to be installed; `build_provider` catches the ImportError and falls
    back to the native provider.
    """
    from vanna.legacy.chromadb import ChromaDB_VectorStore
    from vanna.legacy.ollama import Ollama

    class _LocalVanna(ChromaDB_VectorStore, Ollama):
        """Vanna wired to the local Chroma store and the local Ollama model."""

        def __init__(self, settings: Settings) -> None:
            self.settings = settings
            store_path = settings.project_root / "data" / "vanna_chroma"
            store_path.mkdir(parents=True, exist_ok=True)

            ChromaDB_VectorStore.__init__(
                self, config={"path": str(store_path), "client": "persistent", "n_results": 6}
            )
            Ollama.__init__(
                self,
                config={
                    "model": settings.chat_model,
                    "ollama_host": settings.ollama.host,
                    "ollama_timeout": settings.ollama.timeout_seconds,
                    "options": {
                        "num_ctx": settings.profile.chat_context_tokens,
                        "temperature": 0.0,
                    },
                },
            )

        def log(self, message: str, title: str = "Info") -> None:
            """Vanna prints whole prompts to stdout. Route that to the logger."""
            log.debug("%s: %s", title, message)

        def extract_sql(self, llm_response: str) -> str:
            """Replaces Vanna's extractor, which truncates at the first '['.

            Vanna's version applies `(?=;|\\[|```)` as a lookahead, a workaround
            aimed at Mistral. On SQL Server it destroys any query with
            bracket-quoted identifiers: `SELECT [State], COUNT(*) ...` becomes
            `SELECT `, which then fails at the server with an error pointing
            nowhere near the cause. See ADR-002 and
            tests/test_text_to_sql.py::test_vanna_extract_sql_keeps_brackets.
            """
            return extract_sql(llm_response)

    return _LocalVanna


class VannaTextToSQLProvider(TextToSQLProvider):
    name = "vanna"

    def __init__(self, settings: Settings | None = None, **kwargs) -> None:
        super().__init__(settings or get_settings(), **kwargs)
        self.vanna_version = installed_vanna_version()
        if self.vanna_version == "not installed":
            raise ImportError('vanna is not installed; run: pip install -e ".[vanna]"')

        self._vanna = _build_vanna_class()(self.settings)
        self._trained = False
        log.info("Vanna provider ready (distribution version %s)", self.vanna_version)

    # -- training ----------------------------------------------------------
    def ensure_trained(self, *, force: bool = False) -> dict[str, int]:
        """Load schema, relationships, glossary and examples into Vanna's store.

        Trained from the same sources the native provider reads, so the
        comparison between the two isolates generation rather than measuring
        who was given better context.
        """
        if self._trained and not force:
            return {}

        import contextlib
        import io

        catalog = self.schema.load_catalog()
        counts = {"ddl": 0, "documentation": 0, "examples": 0}

        # Vanna prints progress with bare print(); swallow it.
        with contextlib.redirect_stdout(io.StringIO()):
            for table in catalog:
                self._vanna.train(ddl=table.to_ddl(dialect=self.settings.sql_dialect))
                counts["ddl"] += 1

            for relationship in self.schema._relationships or []:
                self._vanna.train(documentation=f"Join relationship: {relationship}")
                counts["documentation"] += 1

            for term in self._all_glossary_terms():
                self._vanna.train(
                    documentation=(
                        f"BUSINESS DEFINITION - {term['term']} (v{term['version']}): "
                        f"{term['definition']} HOW TO COMPUTE: {term['sql_guidance']} "
                        f"EXCLUDES: {term['known_exclusions'] or 'nothing stated'}"
                    )
                )
                counts["documentation"] += 1

            for example in self._all_examples():
                self._vanna.train(question=example["question"], sql=example["sql_text"])
                counts["examples"] += 1

        self._trained = True
        log.info("Vanna trained: %s", counts)
        return counts

    def _all_glossary_terms(self) -> list[dict[str, str]]:
        from ..database.connection import raw_connection

        with raw_connection(self.settings) as conn:
            cursor = conn.cursor()
            current = "TRUE" if self.settings.sql_dialect == "postgres" else "1"
            cursor.execute(
                "SELECT term, definition, sql_guidance, version, known_exclusions "
                f"FROM ai.business_glossary WHERE is_current = {current}"
            )
            return [
                {
                    "term": t,
                    "definition": d,
                    "sql_guidance": g,
                    "version": v,
                    "known_exclusions": e or "",
                }
                for t, d, g, v, e in cursor.fetchall()
            ]

    def _all_examples(self) -> list[dict[str, str]]:
        from ..database.connection import raw_connection

        with raw_connection(self.settings) as conn:
            cursor = conn.cursor()
            active = "TRUE" if self.settings.sql_dialect == "postgres" else "1"
            cursor.execute(
                "SELECT question, sql_text FROM ai.approved_sql_examples "
                f"WHERE is_active = {active}"
            )
            return [{"question": q, "sql_text": s} for q, s in cursor.fetchall()]

    # -- generation --------------------------------------------------------
    def generate_query(self, request: SQLRequest) -> GeneratedSQL:
        self.ensure_trained()
        started = time.perf_counter()

        # Built even though Vanna does its own retrieval, so the trace records
        # the same fields for both providers and they stay comparable.
        context = self.schema.build_context(request.question, tenant_id=request.tenant_id)
        question = self._with_tenant_instruction(request)

        try:
            raw = self._vanna.generate_sql(question, allow_llm_to_see_data=False)
        except Exception as exc:
            raise RuntimeError(f"Vanna SQL generation failed: {type(exc).__name__}: {exc}") from exc

        sql = extract_sql(raw) if raw else ""

        generated = GeneratedSQL(
            sql=sql,
            question=request.question,
            provider=self.name,
            context=context,
            raw_response=raw or "",
            generation_ms=(time.perf_counter() - started) * 1000,
        )
        if generated.is_empty:
            generated.warnings.append("Vanna returned no usable SQL")

        self._record_trace(
            vanna_version=self.vanna_version,
            model=self.settings.chat_model,
            tables_offered=context.table_names(),
            glossary_terms=[g["term"] for g in context.glossary],
            generation_ms=generated.generation_ms,
        )
        return generated

    def _with_tenant_instruction(self, request: SQLRequest) -> str:
        """Append the tenant restriction to the question.

        Vanna owns its own prompt, so the constraint cannot be injected as a
        system rule. This is a real limitation of wrapping it: the instruction
        is advisory, and only the guard actually enforces it.
        """
        if request.tenant_id is None or not self.settings.security.enforce_tenant_isolation:
            return request.question
        return (
            f"{request.question}\n\n"
            f"(Restrict results to tenant_id = {request.tenant_id}. "
            f"Every tenant-scoped table must be filtered on tenant_id.)"
        )

    # -- explanation -------------------------------------------------------
    def explain_result(self, question: str, result: QueryResult) -> str:
        from ..llm import build_chat_client

        client = build_chat_client(self.settings)
        try:
            response = client.chat(
                model=self.settings.chat_model,
                messages=[
                    {
                        "role": "user",
                        "content": EXPLAIN_PROMPT.format(
                            question=question,
                            sql=result.executed_sql,
                            row_count=result.row_count,
                            rows=result.preview(limit=15),
                        ),
                    }
                ],
                options={"temperature": 0.1, "num_predict": 300},
            )
            return response["message"]["content"].strip()
        except Exception as exc:
            log.warning("Result explanation failed: %s", exc)
            return f"The query returned {result.row_count} row(s)."

    def get_trace_metadata(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "vanna_version": self.vanna_version,
            "trained": self._trained,
            **self._last_trace,
        }


__all__ = ["VannaTextToSQLProvider", "installed_vanna_version"]
