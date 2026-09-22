"""
The Vanna agent: ChromaDB (local vector store) + Ollama (local llama3.1)
+ SQL Server as the data source.

Nothing in this pipeline makes an outbound call to a hosted LLM. The schema,
the questions, and the query results all stay on this machine.
"""

from __future__ import annotations

import logging
import re

import pandas as pd
from vanna.legacy.chromadb import ChromaDB_VectorStore
from vanna.legacy.ollama import Ollama

from .security import UnsafeSQLError, apply_row_limit, assert_read_only
from .settings import Settings, get_settings

log = logging.getLogger(__name__)


class LocalVanna(ChromaDB_VectorStore, Ollama):
    """Vanna wired to a persistent local Chroma store and a local Ollama model."""

    def __init__(self, settings: Settings):
        self.settings = settings

        ChromaDB_VectorStore.__init__(
            self,
            config={
                "path": str(settings.chroma_path),
                "client": "persistent",
                "n_results": settings.n_results,
            },
        )
        Ollama.__init__(
            self,
            config={
                "model": settings.ollama_model,
                "ollama_host": settings.ollama_host,
                "ollama_timeout": settings.ollama_timeout,
                "options": {
                    "num_ctx": settings.ollama_num_ctx,
                    "temperature": settings.ollama_temperature,
                },
            },
        )

    # ------------------------------------------------------------------
    # Logging: Vanna prints full prompts to stdout by default. Route that
    # through the standard logger so the console stays readable, while the
    # detail stays available at LOG_LEVEL=DEBUG (or VERBOSE=true).
    # ------------------------------------------------------------------
    def log(self, message: str, title: str = "Info") -> None:
        if self.settings.verbose:
            print(f"{title}: {message}")
        else:
            log.debug("%s: %s", title, message)

    # ------------------------------------------------------------------
    # SQL extraction
    #
    # Vanna's stock Ollama.extract_sql cuts the response at the first "["
    # (a workaround aimed at Mistral). Bracket-quoted identifiers are
    # everyday T-SQL, so on SQL Server that silently truncates
    # "SELECT [State], COUNT(*) ..." down to "SELECT ". This replacement
    # keeps brackets intact and still handles fenced blocks and CTEs.
    # ------------------------------------------------------------------
    def extract_sql(self, llm_response: str) -> str:
        if not llm_response:
            return ""

        text = llm_response.replace(r"\_", "_").strip()

        # 1) A fenced ```sql block wins when the model emitted one.
        fenced = re.search(r"```(?:sql)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            candidate = fenced.group(1)
        else:
            # 2) Otherwise take everything from the first SELECT / WITH onward.
            start = re.search(r"\b(WITH|SELECT)\b", text, re.IGNORECASE)
            if not start:
                self.log(f"No SQL found in response: {text[:200]}", title="Warning")
                return ""
            candidate = text[start.start():]

        # Keep only the first statement; drop the trailing semicolon.
        candidate = candidate.split(";")[0].strip()

        # Drop any prose the model appended after a blank line.
        candidate = re.split(r"\n\s*\n(?=[A-Z][a-z])", candidate)[0].strip()

        self.log(f"Extracted SQL: {candidate}", title="SQL")
        return candidate

    # ------------------------------------------------------------------
    # Prompt tuning: make the model speak T-SQL, not generic SQL.
    # ------------------------------------------------------------------
    def get_sql_prompt(self, initial_prompt: str | None, question: str, *args, **kwargs):
        if initial_prompt is None:
            initial_prompt = (
                "You are a Microsoft SQL Server (T-SQL) expert. "
                "Generate a single, syntactically correct T-SQL SELECT query "
                "that answers the question, using only the tables and columns "
                "given in the context below.\n"
                "Rules:\n"
                "- Use TOP (n), never LIMIT.\n"
                "- Always schema-qualify tables, e.g. [sales].[orders].\n"
                "- Never write INSERT, UPDATE, DELETE, DROP, ALTER or EXEC.\n"
                "- Return only the SQL, with no commentary.\n"
            )
        return super().get_sql_prompt(initial_prompt, question, *args, **kwargs)


def build_vanna(settings: Settings | None = None) -> LocalVanna:
    """Create the agent, connect it to SQL Server, and install the guardrails."""
    settings = settings or get_settings()
    vn = LocalVanna(settings)

    vn.connect_to_mssql(odbc_conn_str=settings.odbc_connection_string())
    log.info("Connected to SQL Server | %s", settings.describe())

    if settings.read_only:
        _install_guardrails(vn, settings)
    else:
        vn.run_sql_unguarded = vn.run_sql

    vn.allow_llm_to_see_data = settings.allow_llm_to_see_data
    return vn


def _install_guardrails(vn: LocalVanna, settings: Settings) -> None:
    """Wrap the run_sql installed by connect_to_mssql with the safety policy."""
    inner = vn.run_sql

    def guarded_run_sql(sql: str) -> pd.DataFrame:
        assert_read_only(sql)                      # refuse writes / batches
        limited = apply_row_limit(sql, settings.max_rows)
        if limited != sql:
            log.info("Row limit applied: TOP (%s)", settings.max_rows)
        df = inner(limited)
        if len(df) > settings.max_rows:            # backstop for CTE queries
            log.warning("Truncating result to %s rows", settings.max_rows)
            df = df.head(settings.max_rows)
        return df

    # Trusted introspection queries (written by us, not by the LLM) bypass the
    # row cap so that schema extraction is never silently truncated.
    vn.run_sql_unguarded = inner
    vn.run_sql = guarded_run_sql
    vn.run_sql_is_set = True
    log.info("Read-only guardrails active (max_rows=%s)", settings.max_rows)


__all__ = ["LocalVanna", "build_vanna", "UnsafeSQLError"]
