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
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..database.read_only_runner import QueryBlockedError, QueryExecutionError, QueryResult
from ..generation.answerer import Answerer, GenerationError
from ..models.evidence import (
    Answer,
    AnswerStatus,
    Evidence,
    EvidencePackage,
    EvidenceType,
    build_document_evidence,
    detect_conflicts,
)
from ..observability.tracing import Tracer, get_tracer
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
    sql_repairs: int = 0  # self-corrections after a database error
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


@dataclass
class Prepared:
    """The result of routing and evidence gathering, before generation."""

    trace: CopilotTrace
    answer: Answer | None = None  # set for terminal routes
    package: EvidencePackage | None = None
    sql_result: QueryResult | None = None


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
                self._sql_provider = build_provider(
                    self.settings, embedder=getattr(self.retriever, "embedder", None)
                )
            except Exception as exc:
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
        prepared = self.prepare(
            question, user=user, tenant_id=tenant_id, strategy=strategy, approve_sql=approve_sql
        )
        if prepared.answer is not None:
            return prepared.answer, prepared.trace
        return self._generate(prepared), prepared.trace

    def ask_stream(
        self,
        question: str,
        *,
        user: UserContext | None = None,
        tenant_id: int | None = None,
        strategy: str = "reranked",
    ) -> Iterator[tuple[str, Any]]:
        """Stream an answer as events.

        Yields `("prepared", Prepared)` once evidence is gathered - so a UI can
        show sources before the first token - then `("token", str)` for each
        piece of text, and finally `("answer", Answer)`, validated exactly
        like a batch answer.
        """
        prepared = self.prepare(question, user=user, tenant_id=tenant_id, strategy=strategy)
        yield "prepared", prepared
        if prepared.answer is not None:
            yield "answer", prepared.answer
            return

        assert prepared.package is not None
        trace = prepared.trace
        started = time.perf_counter()
        parts: list[str] = []
        try:
            for token in self.answerer.stream_answer(prepared.package):
                parts.append(token)
                yield "token", token
            _, status = self.answerer._prompt_for(prepared.package)
            answer = self.answerer.finish(
                prepared.package,
                "".join(parts),
                status=status,
                started=started,
                trace_id=trace.trace_id,
            )
        except GenerationError as exc:
            answer = self._generation_failed(prepared, exc)
        trace.stage_ms["generation"] = (time.perf_counter() - started) * 1000
        self._attach_sql(prepared, answer)
        self._finish(trace, answer)
        yield "answer", answer

    def prepare(
        self,
        question: str,
        *,
        user: UserContext | None = None,
        tenant_id: int | None = None,
        strategy: str = "reranked",
        approve_sql: bool = False,
    ) -> Prepared:
        """Route and gather evidence. Returns a finished answer for terminal
        routes (refuse, clarify), otherwise the evidence package to answer from."""
        user = user or UserContext.admin()
        self.tracer.reset()
        trace = CopilotTrace(
            trace_id=self.tracer.new_trace(),
            question=question,
            user=user.user_name,
            tenant_id=tenant_id,
        )

        # ---- route ----
        started = time.perf_counter()
        with self.tracer.span("routing", question=question, user=user.user_name) as span:
            decision = self.router.route(question, use_llm=self.settings.router_uses_llm)
            span.set(
                route=decision.route.value,
                decided_by=decision.decided_by,
                confidence=decision.confidence,
                identifiers=decision.identifiers,
                prompt_version=decision.prompt_version,
            )
        trace.routing = decision
        trace.stage_ms["routing"] = (time.perf_counter() - started) * 1000

        if decision.route is Route.REFUSE:
            answer = self._refuse(question, decision, trace)
            self._finish(trace, answer)
            return Prepared(trace=trace, answer=answer)
        if decision.route is Route.CLARIFY:
            answer = self._clarify(question, decision, trace)
            self._finish(trace, answer)
            return Prepared(trace=trace, answer=answer)
        if decision.route is Route.DIRECT:
            answer = self._direct(question, decision, trace)
            self._finish(trace, answer)
            return Prepared(trace=trace, answer=answer)

        # ---- gather evidence ----
        document_evidence: list[Evidence] = []
        sql_evidence: list[Evidence] = []
        sql_result: QueryResult | None = None

        # Documents-only deployments (DATABASE_BACKEND=none) never touch the
        # database. A data question still searches the documents - the KPI
        # glossary or a policy often answers part of it - and the model is told
        # plainly that live figures are unavailable, rather than waiting on a
        # connection timeout that can never succeed.
        sql_available = self.settings.sql_enabled
        needs_documents = decision.needs_documents or (decision.needs_sql and not sql_available)

        run_sql = decision.needs_sql and sql_available
        if needs_documents and run_sql:
            # Hybrid: the two halves are independent, so they run side by
            # side. On a hosted model this roughly halves the wait; on a local
            # one retrieval simply overlaps SQL generation. The worker adopts
            # the caller's open span so its spans nest under the same parent.
            stack = self.tracer.current_stack()

            def documents() -> list[Evidence]:
                self.tracer.adopt(stack)
                return self._gather_documents(decision, user, strategy, trace)

            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="hybrid-docs") as pool:
                future = pool.submit(documents)
                sql_evidence, sql_result = self._gather_sql(
                    decision, user, tenant_id, trace, approve_sql
                )
                document_evidence = future.result()
        elif needs_documents:
            document_evidence = self._gather_documents(decision, user, strategy, trace)
        elif run_sql:
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
        if decision.needs_sql and not sql_available:
            package.notes.append(
                "Live database queries are not enabled in this deployment, so no measured "
                "figures (counts, totals, rankings) are available. Answer only what the "
                "documents support, and state plainly that the data part of the question "
                "cannot be answered here. Do NOT invent figures."
            )
        elif decision.needs_sql and not sql_evidence:
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

        return Prepared(trace=trace, package=package, sql_result=sql_result)

    def _generate(self, prepared: Prepared) -> Answer:
        assert prepared.package is not None
        package, trace = prepared.package, prepared.trace
        started = time.perf_counter()
        try:
            with self.tracer.span(
                "generation",
                evidence_count=len(package.all_evidence),
                conflicts=len(package.conflicts),
            ) as span:
                answer = self.answerer.answer(package, trace_id=trace.trace_id)
                span.set(
                    status=answer.status.value,
                    grounded=answer.is_grounded,
                    citations=len(answer.citations),
                    model=answer.model,
                )
        except GenerationError as exc:
            answer = self._generation_failed(prepared, exc)
        trace.stage_ms["generation"] = (time.perf_counter() - started) * 1000

        self._attach_sql(prepared, answer)
        self._finish(trace, answer)
        return answer

    def _generation_failed(self, prepared: Prepared, exc: Exception) -> Answer:
        # The detail stays in the trace and the log; the user-facing text
        # must not carry provider error bodies, hosts or paths.
        prepared.trace.errors.append(f"generation: {exc}")
        log.warning("Generation failed: %s", exc)
        return Answer(
            question=prepared.trace.question,
            text="The answer could not be generated because the language model is "
            "unavailable right now. Please try again shortly.",
            status=AnswerStatus.INSUFFICIENT_EVIDENCE,
            evidence=prepared.package,
            trace_id=prepared.trace.trace_id,
        )

    @staticmethod
    def _attach_sql(prepared: Prepared, answer: Answer) -> None:
        if prepared.sql_result is not None:
            answer.generated_sql = prepared.sql_result.executed_sql
            answer.sql_row_count = prepared.sql_result.row_count
        elif prepared.trace.generated_sql:
            answer.generated_sql = prepared.trace.generated_sql

    def _finish(self, trace: CopilotTrace, answer: Answer) -> None:
        """Write the trace to disk. Never allowed to fail the request."""
        try:
            self.tracer.flush(
                {
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
                }
            )
        except Exception as exc:
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

    def _direct(self, question: str, decision: RoutingDecision, trace: CopilotTrace) -> Answer:
        """Small talk and "what can you do". A template, not a model call:
        instant on a CPU model, and it cannot invent company facts."""
        sources = "company documents (policies, SLAs, incident reports, glossary)"
        if self.settings.sql_enabled:
            sources += " and the company database (customers, subscriptions, billing, support)"
        examples = [
            "What is the refund policy for annual plans?",
            "What happened in INC-2025-0042?",
        ]
        if self.settings.sql_enabled:
            examples += [
                "Which five customers have the highest ARR?",
                "Show customers with more than three SLA breaches and summarise the SLA policy.",
            ]
        capabilities = (
            f"I answer questions from {sources}, and every answer cites its sources. "
            "I am read-only: I cannot change, delete or export data.\n\n"
            "Try for example:\n" + "\n".join(f"- {e}" for e in examples)
        )
        text = {
            "greeting": "Hello! " + capabilities,
            "thanks": "You're welcome. Ask me anything else about the company's "
            "policies" + (" or data." if self.settings.sql_enabled else "."),
        }.get(decision.reason, capabilities)
        trace.stage_ms["direct"] = 0.0
        return Answer(
            question=question,
            text=text,
            status=AnswerStatus.DIRECT,
            trace_id=trace.trace_id,
        )

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
        self,
        decision: RoutingDecision,
        user: UserContext,
        strategy: str,
        trace: CopilotTrace,
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
                    query,
                    strategy=strategy,
                    user=user,
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
        except Exception as exc:
            trace.errors.append(f"retrieval: {type(exc).__name__}: {exc}")
            log.warning("Document retrieval failed: %s", exc)
            evidence = []

        trace.stage_ms["retrieval"] = (time.perf_counter() - started) * 1000
        return evidence

    def _gather_sql(
        self,
        decision: RoutingDecision,
        user: UserContext,
        tenant_id: int | None,
        trace: CopilotTrace,
        approved: bool,
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
            question=query,
            tenant_id=tenant_id,
            app_user=user.user_name,
            trace_id=trace.trace_id,
        )

        started = time.perf_counter()
        try:
            with self.tracer.span("sql_generation", question=query, provider=provider.name) as span:
                generated = provider.generate_and_repair(request)
                span.set(
                    sql=generated.sql,
                    tables=generated.context.table_names() if generated.context else [],
                )
        except Exception as exc:
            trace.errors.append(f"sql generation: {type(exc).__name__}: {exc}")
            trace.stage_ms["sql_generation"] = (time.perf_counter() - started) * 1000
            return [], None
        trace.stage_ms["sql_generation"] = (time.perf_counter() - started) * 1000

        trace.generated_sql = generated.sql
        if generated.is_empty:
            trace.errors.append("no SQL was produced")
            return [], None

        started = time.perf_counter()
        sql = generated.sql
        retries = max(0, self.settings.sql_execution_retries)
        for attempt in range(retries + 1):
            try:
                with self.tracer.span("sql_execution", sql=sql, attempt=attempt) as span:
                    result = provider.execute_query(sql, request=request, approved=approved)
                    span.set(rows=result.row_count, duration_ms=result.duration_ms)
                trace.sql_validation = "allowed"
                break
            except QueryBlockedError as exc:
                # Never retried: a blocked query is a policy decision, and
                # asking the model to "fix" it is asking it to evade the guard.
                trace.sql_validation = "blocked"
                trace.sql_blocked_reason = exc.result.reason
                trace.stage_ms["sql_execution"] = (time.perf_counter() - started) * 1000
                log.info("SQL blocked for trace %s: %s", trace.trace_id, exc.result.reason)
                return [], None
            except QueryExecutionError as exc:
                # The guard PASSED and the server rejected the query. Labelling
                # this "failed" next to the guard verdict made it look as though
                # the guard had failed, which it had not.
                log.info("SQL attempt %d failed: %s", attempt + 1, str(exc)[:300])
                repaired = (
                    provider._repair_sql(sql, [_database_error(exc)])
                    if attempt < retries
                    else ""
                )
                if not repaired or repaired.strip() == sql.strip():
                    trace.errors.append(f"sql execution: {exc}")
                    trace.sql_validation = "execution_error"
                    trace.sql_error = str(exc)[:400]
                    trace.stage_ms["sql_execution"] = (time.perf_counter() - started) * 1000
                    return [], None
                # The corrected query goes back through execute_query, so the
                # guard validates it exactly like the first one.
                log.info("SQL self-correction attempt %d for trace %s", attempt + 1, trace.trace_id)
                sql = repaired
                trace.generated_sql = sql
                trace.sql_repairs += 1

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


def _database_error(exc: Exception) -> str:
    """The server's complaint, for the repair prompt.

    Only the database message goes back to the model - it names the bad
    column or function, which is what makes a repair possible. The wrapper
    text is dropped so the model is not told how the application is built.
    """
    text = str(exc)
    marker = "failed at the server: "
    if marker in text:
        text = text.split(marker, 1)[1]
    return "The database rejected the query: " + text[:400]


__all__ = ["Copilot", "CopilotTrace"]
