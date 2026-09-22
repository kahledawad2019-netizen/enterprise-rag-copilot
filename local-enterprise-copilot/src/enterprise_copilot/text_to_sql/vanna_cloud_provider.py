"""
Vanna Cloud Text-to-SQL provider.

This is the only component in the project that sends anything off the machine,
so the trade-off is stated here rather than buried in a config file.

## What leaves the machine

| | hybrid (default) | cloud |
|---|---|---|
| Table and column names (DDL) | uploaded once | uploaded once |
| Business glossary | uploaded once | uploaded once |
| Approved SQL examples | uploaded once | uploaded once |
| The user's question | sent per query | sent per query |
| The assembled prompt | no | sent per query |
| Query **results** (customer rows) | never | never |

`hybrid` keeps generation on the local model and uses Vanna Cloud only as the
training store and retriever. `cloud` additionally uses Vanna's hosted model,
which is the mode to pick when there is no local GPU - for example in a
container.

Neither mode changes any safety property. Vanna generates a *string*; that
string still goes through `SQLGuard` (sqlglot AST validation, schema
allow-list, tenant predicate) and `ReadOnlyRunner` before it reaches a
database, exactly as it does for the local providers. Nothing here is
trusted.

## Why results are never sent

Vanna supports feeding query output back into the prompt to refine a
follow-up. That would put real customer rows in a third party's logs.
`allow_llm_to_see_data` is therefore pinned to the setting, which defaults to
false, and is asserted in the test suite rather than left to a code review.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings, get_settings
from .native import extract_sql
from .provider import GeneratedSQL, SQLRequest
from .vanna_provider import VannaTextToSQLProvider, installed_vanna_version

log = logging.getLogger(__name__)


class VannaCloudNotConfiguredError(RuntimeError):
    """Raised with the exact remedy rather than a bare KeyError."""


def _build_cloud_vanna_class(mode: str):
    """Compose the Vanna client for the requested mode.

    Built inside a function so importing this module never requires vanna to
    be installed; `build_provider` catches ImportError and falls back.
    """
    from vanna.legacy.vannadb import VannaDB_VectorStore

    if mode == "cloud":
        from vanna.legacy.remote import VannaDefault

        class _CloudVanna(VannaDefault):
            """Vanna Cloud for both retrieval and generation."""

            def __init__(self, settings: Settings) -> None:
                self.settings = settings
                cloud = settings.vanna_cloud
                super().__init__(
                    model=cloud.model,
                    api_key=cloud.api_key.get_secret_value(),  # type: ignore[union-attr]
                    config={"endpoint": cloud.endpoint},
                )

            def log(self, message: str, title: str = "Info") -> None:
                log.debug("%s: %s", title, message)

            def extract_sql(self, llm_response: str) -> str:
                # The project's extractor, for the same reason the local
                # provider overrides it: one tested implementation, so a
                # provider swap cannot change what counts as the SQL.
                return extract_sql(llm_response)

        return _CloudVanna

    from vanna.legacy.ollama import Ollama

    class _HybridVanna(VannaDB_VectorStore, Ollama):
        """Vanna Cloud holds the training corpus; the local model generates."""

        def __init__(self, settings: Settings) -> None:
            self.settings = settings
            cloud = settings.vanna_cloud

            VannaDB_VectorStore.__init__(
                self,
                vanna_model=cloud.model,
                vanna_api_key=cloud.api_key.get_secret_value(),  # type: ignore[union-attr]
                config={"endpoint": cloud.endpoint},
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
            log.debug("%s: %s", title, message)

        def extract_sql(self, llm_response: str) -> str:
            return extract_sql(llm_response)

    return _HybridVanna


class VannaCloudTextToSQLProvider(VannaTextToSQLProvider):
    """Vanna Cloud, behind the same interface as every other provider.

    Inherits training and generation from the local Vanna provider: the corpus
    assembled from DDL, glossary and approved examples is identical, so a
    comparison between providers measures generation and not who was handed
    better context.
    """

    name = "vanna_cloud"

    def __init__(self, settings: Settings | None = None, **kwargs) -> None:
        # Deliberately skips VannaTextToSQLProvider.__init__, which builds the
        # local Chroma+Ollama client. Everything else it sets up is wanted.
        settings = settings or get_settings()
        super(VannaTextToSQLProvider, self).__init__(settings, **kwargs)

        cloud = self.settings.vanna_cloud
        if not cloud.is_configured:
            raise VannaCloudNotConfiguredError(
                "VANNA_API_KEY is not set. Get a key at https://vanna.ai, then either "
                "add VANNA_API_KEY=... to .env or set it as a container secret. "
                "See docs/vanna_cloud.md."
            )

        self.vanna_version = installed_vanna_version()
        if self.vanna_version == "not installed":
            raise ImportError('vanna is not installed; run: pip install -e ".[vanna]"')

        self.mode = cloud.mode
        self._vanna = _build_cloud_vanna_class(self.mode)(self.settings)
        self._trained = False

        log.info(
            "Vanna Cloud provider ready (mode=%s, corpus=%r, vanna %s)",
            self.mode, cloud.model, self.vanna_version,
        )

    # -- training ----------------------------------------------------------
    def ensure_trained(self, *, force: bool = False) -> dict[str, int]:
        """Upload the corpus, but only if Vanna Cloud does not already hold it.

        Unlike the local Chroma store, this one is durable and shared: the
        corpus survives the process, and every container that starts would
        otherwise upload another copy of all 26 DDL statements, the glossary
        and the examples. Duplicated training data degrades retrieval, so the
        remote count is checked first.
        """
        if self._trained and not force:
            return {}

        if not force:
            existing = self._remote_training_rows()
            if existing > 0:
                log.info(
                    "Vanna Cloud corpus %r already holds %d rows; skipping upload. "
                    "Use force=True after a schema change.",
                    self.settings.vanna_cloud.model, existing,
                )
                self._trained = True
                return {"existing": existing}

        return super().ensure_trained(force=True)

    def _remote_training_rows(self) -> int:
        """How many rows the remote corpus holds. 0 on any failure.

        A network hiccup here must not stop the provider: the worst case of
        guessing zero is that training is re-uploaded, which is wasteful but
        correct, while the worst case of raising is an outage.
        """
        try:
            data = self._vanna.get_training_data()
        except Exception as exc:
            log.warning("Could not read the Vanna Cloud corpus: %s", exc)
            return 0
        try:
            return 0 if data is None else int(len(data))
        except TypeError:
            return 0

    # -- generation --------------------------------------------------------
    def generate_query(self, request: SQLRequest) -> GeneratedSQL:
        self.ensure_trained()
        started = time.perf_counter()

        context = self.schema.build_context(request.question, tenant_id=request.tenant_id)
        question = self._with_tenant_instruction(request)

        try:
            raw = self._vanna.generate_sql(
                question,
                allow_llm_to_see_data=self.settings.vanna_cloud.allow_llm_to_see_data,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Vanna Cloud SQL generation failed: {type(exc).__name__}: {exc}. "
                f"Check VANNA_API_KEY and that the corpus "
                f"{self.settings.vanna_cloud.model!r} exists in the dashboard."
            ) from exc

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
            generated.warnings.append("Vanna Cloud returned no usable SQL")

        self._record_trace(
            vanna_version=self.vanna_version,
            vanna_mode=self.mode,
            vanna_corpus=self.settings.vanna_cloud.model,
            model=(
                "vanna-hosted" if self.mode == "cloud" else self.settings.chat_model
            ),
            tables_offered=context.table_names(),
            glossary_terms=[g["term"] for g in context.glossary],
            generation_ms=generated.generation_ms,
        )
        return generated

    # -- operations --------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """What this provider is doing, for the config page and the trace.

        The API key is not included, and there is no code path that puts it in
        a dictionary that something might later log.
        """
        cloud = self.settings.vanna_cloud
        return {
            "provider": self.name,
            "mode": cloud.mode,
            "corpus": cloud.model,
            "endpoint": cloud.endpoint,
            "generation": "vanna hosted model" if cloud.mode == "cloud" else self.settings.chat_model,
            "sends_question": True,
            "sends_results": cloud.allow_llm_to_see_data,
            "vanna_version": self.vanna_version,
        }


__all__ = ["VannaCloudTextToSQLProvider", "VannaCloudNotConfiguredError"]
