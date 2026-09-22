"""
The agent workflow.

One entry point, `Copilot.ask`, which runs the pipeline the architecture
describes:

    question
      -> route            (rules, then model)
      -> retrieve docs    if the route needs them
      -> generate SQL     if the route needs it
      -> validate + run   always through the guard and the read-only runner
      -> build one evidence package
      -> generate the answer
      -> validate citations
      -> trace

This is deliberately a plain class with explicit steps rather than an agent
framework. Every stage is independently testable, the control flow is readable
top to bottom, and there is no hidden retry or tool-selection logic to reason
about when an answer comes out wrong.

## Multi-source is the interesting case

"Show customers with more than three SLA breaches and summarise the SLA policy"
needs the database for the count and the document corpus for the policy. The
two kinds of evidence are gathered independently and combined into **one**
package where document evidence is labelled `D1..` and data evidence `S1..`.
Keeping them distinct is what lets the answer say "the policy requires 15
minutes [D2], and the measured average was 42 minutes [S1]" instead of blurring
a contractual commitment into a measurement.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..database.read_only_runner import QueryBlockedError, QueryExecutionError, QueryResult
from ..generation.answerer import Answerer, GenerationError
from ..observability.tracing import Tracer, get_tracer
from ..models.evidence import (
    Answer,
    AnswerStatus,
    Evidence,
    EvidencePackage,
    EvidenceType,
    build_document_evidence,
    detect_conflicts,
)
from ..retrieval.hybrid import HybridRetriever, RetrievalTrace, UserContext
from ..text_to_sql.provider import SQLRequest, TextToSQLProvider, build_provider
from .router import QueryRouter, Route, RoutingDecision

log = logging.getLogger(__name__)


@dataclass
class CopilotTrace:
    """Everything that happened, for the observability layer and the UI."""

    trace_id: str
    question: str
    user: str = ""
    tenant_id: int | None = None

    routing: RoutingDecision | None = None
    retrieval: RetrievalTrace | None = None
    generated_sql: str | None = None
    sql_validation: str | None = None
    sql_blocked_reason: str | None = None
    sql_error: str | None = None
    sql_row_count: int | None = None

    stage_ms: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total_ms(self) -> float:
        return sum(self.stage_ms.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "question": self.question,
            "user": self.user,
            "tenant_id": self.tenant_id,
            "route": self.routing.route.value if self.routing else None,
            "route_decided_by": self.routing.decided_by if self.routing else None,
            "identifiers": self.routing.identifiers if self.routing else [],
            "retrieval": self.retrieval.summary() if self.retrieval else None,
            "generated_sql": self.generated_sql,
            "sql_validation": self.sql_validation,
            "sql_blocked_reason": self.sql_blocked_reason,
            "sql_error": self.sql_error,
            "sql_row_count": self.sql_row_count,
            "stage_ms": {k: round(v, 1) for k, v in self.stage_ms.items()},
            "total_ms": round(self.total_ms, 1),
            "errors": self.errors,
        }


class Copilot:
    """The assembled system."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        retriever: HybridRetriever | None = None,
        answerer: Answerer | None = None,
        router: QueryRouter | None = None,
        sql_provider: TextToSQLProvider | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.router = router or QueryRouter(self.settings)
        self.retriever = retriever or HybridRetriever(self.settings)
        self.answerer = answerer or Answerer(self.settings)
        self._sql_provider = sql_provider
        self._sql_provider_failed = False
        self.tracer: Tracer = get_tracer(self.settings)

    @property
    def sql_provider(self) -> TextToSQLProvider | None:
        """Built lazily: a document-only question should not pay for it."""
        if self._sql_provider is None and not self._sql_provider_failed:
            try:
                self._sql_provider = build_provider(self.settings)
            except Exception as exc:  # noqa: BLE001
                log.warning("Text-to-SQL unavailable: %s", exc)
                self._sql_provider_failed = True
        return self._sql_provider

    # -- main entry point --------------------------------------------------
    def ask(
        self,
        question: str,
        *,
        user: UserContext | None = None,
        tenant_id: int | None = None,
        strategy: str = "reranked",
        approve_sql: bool = False,
    ) -> tuple[Answer, CopilotTrace]:
        user = user or UserContext.admin()
        self.tracer.reset()
        trace = CopilotTrace(
            trace_id=self.tracer.new_trace(), question=question,
            user=user.user_name, tenant_id=tenant_id,
        )

        # ---- route ----
        started = time.perf_counter()
        with self.tracer.span("routing", question=question, user=user.user_name) as span:
            decision = self.router.route(question)
            span.set(route=decision.route.value, decided_by=decision.decided_by,
                     confidence=decision.confidence, identifiers=decision.identifiers,
                     prompt_version=decision.prompt_version)
        trace.routing = decision
        trace.stage_ms["routing"] = (time.perf_counter() - started) * 1000

        if decision.route is Route.REFUSE:
            answer = self._refuse(question, decision, trace)
            self._finish(trace, answer)
            return answer, trace
        if decision.route is Route.CLARIFY:
            answer = self._clarify(question, decision, trace)
            self._finish(trace, answer)
            return answer, trace

        # ---- gather evidence ----
        document_evidence: list[Evidence] = []
        sql_evidence: list[Evidence] = []
        sql_result: QueryResult | None = None

        if decision.needs_documents:
            document_evidence = self._gather_documents(decision, user, strategy, trace)

        if decision.needs_sql:
            sql_evidence, sql_result = self._gather_sql(
                decision, user, tenant_id, trace, approve_sql
            )

        package = EvidencePackage(
            question=question,
            rewritten_question=decision.rewritten_query,
            route=decision.route.value,
            document_evidence=document_evidence,
            sql_evidence=sql_evidence,
            conflicts=detect_conflicts(document_evidence),
        )
        # Without this the generator sees an empty evidence package, has no
        # idea why, and invents a plausible-sounding non-answer. It happened:
        # a failed query produced advice to "check the Sales Dashboard", which
        # does not exist.
        if decision.needs_sql and not sql_evidence:
            if trace.sql_blocked_reason:
                package.notes.append(
                    f"The database query was blocked by the safety guard: "
                    f"{trace.sql_blocked_reason}. Say so plainly; do not guess the figures."
                )
            elif trace.sql_error:
                package.notes.append(
                    "The database query was generated but failed to run, so no figures "
                    "are available. State that the query failed and that the number "
                    "could not be retrieved. Do NOT invent a figure, and do NOT suggest "
                    "dashboards, reports or tools - none exist."
                )
            else:
                package.notes.append(
                    "No database result is available for this question. Say so plainly."
                )

        if decision.route is Route.MULTI_SOURCE:
            package.notes.append(
                "This answer combines written policy with database figures. "
                "Policy statements are cited [D...]; measured values are cited [S...]."
            )

        # ---- answer ----
        started = time.perf_counter()
        try:
            with self.tracer.span(
                "generation",
                evidence_count=len(package.all_evidence),
                conflicts=len(package.conflicts),
            ) as span:
                answer = self.answerer.answer(package, trace_id=trace.trace_id)
                span.set(status=answer.status.value, grounded=answer.is_grounded,
                         citations=len(answer.citations), model=answer.model)
        except GenerationError as exc:
            trace.errors.append(f"generation: {exc}")
            answer = Answer(
                question=question,
                text=f"The answer could not be generated: {exc}",
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                evidence=package, trace_id=trace.trace_id,
            )
        trace.stage_ms["generation"] = (time.perf_counter() - started) * 1000

        if sql_result is not None:
            answer.generated_sql = sql_result.executed_sql
            answer.sql_row_count = sql_result.row_count
        elif trace.generated_sql:
            answer.generated_sql = trace.generated_sql

        self._finish(trace, answer)
        return answer, trace

    def _finish(self, trace: CopilotTrace, answer: Answer) -> None:
        """Write the trace to disk. Never allowed to fail the request."""
        try:
            self.tracer.flush({
                "question": trace.question,
                "user": trace.user,
                "tenant_id": trace.tenant_id,
                "route": trace.routing.route.value if trace.routing else None,
                "answer_status": answer.status.value,
                "grounded": answer.is_grounded,
                "citation_count": len(answer.citations),
                "generated_sql": trace.generated_sql,
                "sql_validation": trace.sql_validation,
                "sql_row_count": trace.sql_row_count,
                "model": answer.model,
                "prompt_version": answer.prompt_version,
                "index_version": self.settings.vector_store.index_version,
                "errors": trace.errors,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("Trace flush failed: %s", exc)

    # -- terminal routes ---------------------------------------------------
    def _refuse(self, question: str, decision: RoutingDecision, trace: CopilotTrace) -> Answer:
        started = time.perf_counter()
        try:
            answer = self.answerer.refuse(question, decision.reason)
        except GenerationError:
            # A refusal must still happen when the model is unreachable.
            answer = Answer(
                question=question,
                text=f"I cannot do that: {decision.reason}. This system is read-only "
                     f"and can only answer questions about company documents and data.",
                status=AnswerStatus.REFUSED,
            )
        answer.trace_id = trace.trace_id
        trace.stage_ms["refusal"] = (time.perf_counter() - started) * 1000
        return answer

    def _clarify(self, question: str, decision: RoutingDecision, trace: CopilotTrace) -> Answer:
        started = time.perf_counter()
        try:
            answer = self.answerer.clarify(question, decision.reason)
        except GenerationError:
            answer = Answer(
                question=question,
                text=f"Could you be more specific? {decision.reason}.",
                status=AnswerStatus.CLARIFICATION_NEEDED,
            )
        answer.trace_id = trace.trace_id
        trace.stage_ms["clarification"] = (time.perf_counter() - started) * 1000
        return answer

    # -- evidence gathering ------------------------------------------------
    def _gather_documents(
        self, decision: RoutingDecision, user: UserContext,
        strategy: str, trace: CopilotTrace,
    ) -> list[Evidence]:
        started = time.perf_counter()
        # For multi-source, prefer the document-facing subquestion when the
        # router produced one: it is a cleaner retrieval query than the
        # compound original.
        query = decision.rewritten_query or decision.original_query
        if decision.route is Route.MULTI_SOURCE and decision.document_subquestion:
            query = decision.document_subquestion

        try:
            with self.tracer.span("retrieval", query=query, strategy=strategy) as span:
                results, retrieval_trace = self.retriever.retrieve(
                    query, strategy=strategy, user=user,
                    rewritten_query=None if query == decision.original_query else query,
                )
                span.set(
                    dense=len(retrieval_trace.dense_results),
                    sparse=len(retrieval_trace.sparse_results),
                    final=len(results),
                    chunk_ids=[r.chunk.chunk_id for r in results],
                    scores=[round(r.score, 4) for r in results],
                )
            trace.retrieval = retrieval_trace
            evidence = build_document_evidence(results)
        except Exception as exc:  # noqa: BLE001
            trace.errors.append(f"retrieval: {type(exc).__name__}: {exc}")
            log.warning("Document retrieval failed: %s", exc)
            evidence = []

        trace.stage_ms["retrieval"] = (time.perf_counter() - started) * 1000
        return evidence

    def _gather_sql(
        self, decision: RoutingDecision, user: UserContext,
        tenant_id: int | None, trace: CopilotTrace, approved: bool,
    ) -> tuple[list[Evidence], QueryResult | None]:
        provider = self.sql_provider
        if provider is None:
            trace.errors.append("text-to-sql provider unavailable")
            return [], None

        # The DATA half specifically. Using the rewritten query here was the
        # bug that sent "What is the SLA policy?" to the SQL generator.
        if decision.route is Route.MULTI_SOURCE:
            query = decision.data_subquestion or decision.original_query
        else:
            query = decision.rewritten_query or decision.original_query

        request = SQLRequest(
            question=query, tenant_id=tenant_id,
            app_user=user.user_name, trace_id=trace.trace_id,
        )

        started = time.perf_counter()
        try:
            with self.tracer.span("sql_generation", question=query,
                                  provider=provider.name) as span:
                generated = provider.generate_and_repair(request)
                span.set(
                    sql=generated.sql,
                    tables=generated.context.table_names() if generated.context else [],
                )
        except Exception as exc:  # noqa: BLE001
            trace.errors.append(f"sql generation: {type(exc).__name__}: {exc}")
            trace.stage_ms["sql_generation"] = (time.perf_counter() - started) * 1000
            return [], None
        trace.stage_ms["sql_generation"] = (time.perf_counter() - started) * 1000

        trace.generated_sql = generated.sql
        if generated.is_empty:
            trace.errors.append("no SQL was produced")
            return [], None

        started = time.perf_counter()
        try:
            with self.tracer.span("sql_execution", sql=generated.sql) as span:
                result = provider.execute_query(
                    generated.sql, request=request, approved=approved
                )
                span.set(rows=result.row_count, duration_ms=result.duration_ms)
            trace.sql_validation = "allowed"
        except QueryBlockedError as exc:
            trace.sql_validation = "blocked"
            trace.sql_blocked_reason = exc.result.reason
            trace.stage_ms["sql_execution"] = (time.perf_counter() - started) * 1000
            log.info("SQL blocked for trace %s: %s", trace.trace_id, exc.result.reason)
            return [], None
        except QueryExecutionError as exc:
            # The guard PASSED and the server rejected the query. Labelling
            # this "failed" next to the guard verdict made it look as though
            # the guard had failed, which it had not.
            trace.sql_validation = "execution_error"
            trace.sql_error = str(exc)[:400]
            trace.errors.append(f"sql execution: {exc}")
            trace.stage_ms["sql_execution"] = (time.perf_counter() - started) * 1000
            return [], None

        trace.stage_ms["sql_execution"] = (time.perf_counter() - started) * 1000
        trace.sql_row_count = result.row_count

        return [self._sql_evidence(result)], result

    @staticmethod
    def _sql_evidence(result: QueryResult) -> Evidence:
        """Wrap query output as citable evidence labelled S1.

        The SQL itself is included in the text so the model can describe how the
        figure was obtained, and so a reader can check it.
        """
        return Evidence(
            evidence_id="S1",
            evidence_type=EvidenceType.SQL_RESULT,
            text=(
                f"Query executed against the company database:\n{result.executed_sql}\n\n"
                f"Result ({result.row_count} rows):\n{result.preview(limit=20)}"
            ),
            structured={
                "sql": result.executed_sql,
                "columns": result.columns,
                "rows": result.rows[:50],
                "row_count": result.row_count,
                "truncated": result.truncated,
            },
            source_id=", ".join(result.validation.tables) if result.validation else "database",
            source_title="Company database",
            retrieval_method="text_to_sql",
        )

    def close(self) -> None:
        self.retriever.close()


__all__ = ["Copilot", "CopilotTrace"]
