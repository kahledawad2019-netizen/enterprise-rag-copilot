"""
Query routing.

Decides what kind of question this is before any retrieval or SQL generation
happens:

    DOCUMENT_RAG   policy, procedure, definition       -> documents
    TEXT_TO_SQL    counts, totals, rankings            -> database
    MULTI_SOURCE   policy *versus* actual              -> both
    CLARIFY        ambiguous; guessing would mislead   -> ask one question
    REFUSE         destructive, or out of scope        -> decline

## Why refusal starts as a rule

Testing Phase 5 surfaced a concrete failure: asked to "delete all customers",
the model produced an unrelated `SELECT TOP (1) customer_id`. The SQL guard
allowed it, correctly — it is a harmless read — and the system then answered
"There is one customer in the database". No data was at risk, but the user got
a confident, wrong-looking answer to a request that should have been refused.

So destructive intent is detected by **deterministic rules that run before the
model is consulted**. A model asked "is this destructive?" can be talked out of
its answer; a regular expression cannot.

## Why a rule is not enough on its own

Regexes only catch the phrasings somebody anticipated. Measured against fifteen
evasive rewordings, `DESTRUCTIVE_PATTERNS` and `EXFILTRATION_PATTERNS` caught
three: "Empty the support tickets table" and "Get rid of every client we have
on file" both sailed through.

So a **semantic screen** (`security/intent_classifier.py`) now runs immediately
after the rules, on the requests they cleared. The ordering is the whole design:

    rules refuse        -> refused, model never consulted
    rules clear         -> screen may still refuse
    screen unavailable  -> exactly the previous behaviour

The screen can only ever *narrow* what is allowed. It has no path to clear
something a rule refused, because a refused request never reaches it. That is
what makes it safe to let a model participate in a security decision at all.

## Why rewriting must preserve identifiers

The rewriter turns a follow-up into a standalone question. Left unconstrained,
models "helpfully" normalise `INC-2025-0042` into "the June incident", which
destroys the exact token BM25 needs — the one retrieval path that reliably
finds it. Identifiers are extracted before rewriting and re-checked after; if
one went missing, the rewrite is discarded.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum

from ..config import Settings, get_settings
from ..security.intent_classifier import IntentVerdict, LLMIntentScreen

log = logging.getLogger(__name__)

ROUTER_PROMPT_VERSION = "1.0.0"


class Route(StrEnum):
    DOCUMENT_RAG = "document_rag"
    TEXT_TO_SQL = "text_to_sql"
    MULTI_SOURCE = "multi_source"
    CLARIFY = "clarify"
    REFUSE = "refuse"


# ---------------------------------------------------------------------------
# Deterministic rules, applied before the model is asked anything
# ---------------------------------------------------------------------------

# Intent to change data. Matched on the request, not on generated SQL.
DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(delete|remove|erase|wipe|purge)\b.{0,40}\b(all|every|record|row|customer|table|data|database)\b",
            re.I,
        ),
        "asks to delete data",
    ),
    # TRUNCATE is a SQL verb with no innocent English reading in this context,
    # so it is destructive on its own. DROP is ambiguous ("drop me a line"), so
    # it still needs an object.
    #
    # The object list is deliberately wide. An earlier version required
    # table|database|schema|index|view, and "Truncate the audit log" therefore
    # slipped through to the SQL path instead of being refused - found by
    # evals/security_and_routing.jsonl case SEC-004.
    (re.compile(r"\btruncate\b", re.I), "asks to truncate data"),
    (
        re.compile(
            r"\bdrop\b.{0,30}\b(table|database|schema|index|view|log|audit|record|"
            r"data|column|constraint|everything)\b",
            re.I,
        ),
        "asks to drop a database object",
    ),
    (
        re.compile(
            r"\b(update|modify|change|set|alter)\b.{0,40}\b(all|every|table|column|record|row|schema)\b",
            re.I,
        ),
        "asks to modify data or schema",
    ),
    (re.compile(r"\binsert\s+into\b", re.I), "asks to insert data"),
    (
        re.compile(r"\b(grant|revoke)\b.{0,30}\b(access|permission|admin|role|privilege)\b", re.I),
        "asks to change permissions",
    ),
    (
        re.compile(
            r"\b(disable|bypass|turn\s+off)\b.{0,30}\b(security|guard|restriction|validation|filter)\b",
            re.I,
        ),
        "asks to disable a security control",
    ),
)

# Attempts to extract system internals or credentials.
EXFILTRATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(connection\s+string|password|credential|secret|api[_\s]?key)\b", re.I),
        "asks for credentials",
    ),
    (
        re.compile(r"\b(system\s+prompt|your\s+instructions?|environment\s+variables?)\b", re.I),
        "asks to reveal system configuration",
    ),
    (
        re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b", re.I),
        "attempts to override system instructions",
    ),
    (re.compile(r"\b(unrestricted|developer)\s+mode\b", re.I), "attempts to change operating mode"),
)

# Exact tokens that must survive rewriting: INC-2025-0042, DOC-REF-001,
# SLA-ENT-P1, CUST-00042, NW-ANALYTICS, P1, 2025-06-14.
#
# Segments allow up to 12 characters: an earlier cap of 6 silently failed on
# product codes such as NW-ANALYTICS, so the identifier was never extracted and
# a rewrite was free to drop it.
IDENTIFIER_PATTERN = re.compile(
    r"\b(?:[A-Z]{2,5}-[A-Z0-9]{2,12}(?:-[A-Z0-9]{2,12})*|[A-Z]{2,}\d+|\d{4}-\d{2}-\d{2})\b"
)

# Strong signals that a question needs the database rather than a document.
SQL_SIGNALS = re.compile(
    r"\b(how many|how much|count|total|sum|average|avg|top \d+|highest|lowest|"
    r"per (month|quarter|year|region|customer|product)|trend|rank|list all|"
    r"breakdown|compare .* (revenue|arr|mrr|churn)|which customers?)\b",
    re.I,
)

# Strong signals that a question is about written policy.
DOC_SIGNALS = re.compile(
    r"\b(policy|policies|procedure|guideline|sla|contract|terms|what does .* mean|"
    r"definition of|how do (we|i)|process for|escalation|postmortem|root cause|"
    r"entitled to|allowed to|required to)\b",
    re.I,
)

# Both at once: the defining shape of a multi-source question.
MULTI_SIGNALS = re.compile(
    r"\b(and (also )?(summari[sz]e|explain|what does)|compare .* with .*|"
    r"versus|vs\.?|against the (policy|sla|contract)|"
    r"(policy|sla|contract).{0,40}(actual|real|measured)|"
    r"(actual|real|measured).{0,40}(policy|sla|contract))\b",
    re.I,
)

# Vague references that cannot be answered without knowing which one is meant.
AMBIGUITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^\s*(what|which|how)\s+(is|are|was)\s+the\s+(policy|response time|number|value|rate|score)\s*\??\s*$",
            re.I,
        ),
        "which policy or metric is meant is not stated",
    ),
    (
        re.compile(r"^\s*tell me about\s+\w+\s*\.?\s*$", re.I),
        "the subject is named too loosely to identify",
    ),
)


@dataclass
class RoutingDecision:
    """Where a question is going, and why."""

    route: Route
    original_query: str
    rewritten_query: str = ""
    reason: str = ""
    confidence: float = 0.0
    decided_by: str = "rules"  # rules | llm | fallback

    identifiers: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)

    # Labelled rather than positional. An earlier version used
    # subquestions[0] for documents and subquestions[-1] for data, which
    # silently sent "What is the SLA policy?" to the SQL generator whenever the
    # model happened to order them the other way round.
    document_subquestion: str | None = None
    data_subquestion: str | None = None

    latency_ms: float = 0.0
    prompt_version: str = ROUTER_PROMPT_VERSION

    @property
    def needs_documents(self) -> bool:
        return self.route in (Route.DOCUMENT_RAG, Route.MULTI_SOURCE)

    @property
    def needs_sql(self) -> bool:
        return self.route in (Route.TEXT_TO_SQL, Route.MULTI_SOURCE)

    @property
    def is_terminal(self) -> bool:
        """Routes that answer without retrieving anything."""
        return self.route in (Route.REFUSE, Route.CLARIFY)

    def summary(self) -> str:
        return (
            f"{self.route.value} (by {self.decided_by}, conf={self.confidence:.2f}) "
            f"ids={self.identifiers or '-'} | {self.reason}"
        )


ROUTER_SYSTEM = """You classify questions for a company assistant that can read internal \
documents and query a company database. You output JSON only.

ROUTES
- "document_rag": answered from written policy, procedure, contract or definition.
    e.g. "What is the refund policy for annual plans?", "What does active customer mean?"
- "text_to_sql": answered by counting, totalling or ranking records in the database.
    e.g. "Which five customers have the highest ARR?", "How many tickets are open?"
- "multi_source": needs BOTH a written rule AND actual figures, usually to compare them.
    e.g. "Show customers with more than three SLA breaches and summarise the SLA policy.",
         "Compare the contractual response time with the actual response time for Acme."
- "clarify": genuinely ambiguous. Answering would require guessing which thing is meant.
    e.g. "What is the response time?" (which tier? which priority?)

RULES
- Choose "multi_source" only when the answer genuinely needs both. A question that merely \
mentions a policy term is still document_rag.
- Choose "clarify" only when a reasonable person could not tell what is being asked. \
Do not use it for questions that are merely broad.
- When rewriting, KEEP every code, identifier, date and proper name EXACTLY as written. \
Never replace "INC-2025-0042" with a description.

OUTPUT (JSON only, no prose)
{"route": "...", "confidence": 0.0-1.0, "reason": "one short sentence",
 "rewritten_query": "standalone version of the question",
 "document_subquestion": "the part answered from written policy, or null",
 "data_subquestion": "the part answered by querying records, or null"}

For multi_source you MUST fill BOTH subquestion fields, and each must be a complete
standalone question:
  "document_subquestion": "What does the SLA policy say about response targets?"
  "data_subquestion": "Which customers have more than three SLA breaches?"
For every other route, set both to null."""


class QueryRouter:
    def __init__(
        self, settings: Settings | None = None, *, intent_screen: LLMIntentScreen | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._client = None
        # Injectable so a test can supply a screen with a stub client rather
        # than reaching a live model.
        self.intent_screen = intent_screen or LLMIntentScreen(self.settings)
        self._last_intent_verdict: IntentVerdict | None = None

    @property
    def last_intent_verdict(self) -> IntentVerdict | None:
        """The most recent screening result, for the trace and the debugger."""
        return self._last_intent_verdict

    # -- main entry point --------------------------------------------------
    def route(self, question: str, *, use_llm: bool = True) -> RoutingDecision:
        started = time.perf_counter()
        decision = self._route_inner(question, use_llm=use_llm)
        decision.latency_ms = (time.perf_counter() - started) * 1000
        log.info("Router: %s", decision.summary())
        return decision

    def _route_inner(self, question: str, *, use_llm: bool) -> RoutingDecision:
        identifiers = self.extract_identifiers(question)
        dates = self.extract_dates(question)

        # ---- Rules first. These are not negotiable and never reach the model.
        blocked = self._check_forbidden(question)
        if blocked is not None:
            return RoutingDecision(
                route=Route.REFUSE,
                original_query=question,
                rewritten_query=question,
                reason=blocked,
                confidence=1.0,
                decided_by="rules",
                identifiers=identifiers,
                dates=dates,
            )

        # ---- Then the semantic screen, on what the rules already cleared.
        #
        # This can only ADD a refusal. A request the rules blocked never reaches
        # it, so there is no path by which the model can clear something the
        # rules refused. Every failure mode degrades to the previous behaviour.
        # See security/intent_classifier.py for the full argument.
        if use_llm:
            verdict = self.intent_screen.screen(question)
            if verdict.refuse:
                return RoutingDecision(
                    route=Route.REFUSE,
                    original_query=question,
                    rewritten_query=question,
                    reason=verdict.explanation,
                    confidence=verdict.confidence,
                    decided_by="llm_intent_screen",
                    identifiers=identifiers,
                    dates=dates,
                )
            self._last_intent_verdict = verdict

        ambiguous = self._check_ambiguous(question)
        if ambiguous is not None:
            return RoutingDecision(
                route=Route.CLARIFY,
                original_query=question,
                rewritten_query=question,
                reason=ambiguous,
                confidence=0.9,
                decided_by="rules",
                identifiers=identifiers,
                dates=dates,
            )

        # ---- Then the model, for the genuinely fuzzy distinction.
        if use_llm:
            decision = self._classify_with_llm(question, identifiers, dates)
            if decision is not None:
                return decision

        return self._classify_with_heuristics(question, identifiers, dates)

    # -- rules -------------------------------------------------------------
    def _check_forbidden(self, question: str) -> str | None:
        """Destructive or exfiltration intent. Checked before anything else."""
        for pattern, reason in DESTRUCTIVE_PATTERNS:
            if pattern.search(question):
                return f"this system is read-only and {reason}"
        for pattern, reason in EXFILTRATION_PATTERNS:
            if pattern.search(question):
                return f"the request {reason}"
        return None

    def _check_ambiguous(self, question: str) -> str | None:
        for pattern, reason in AMBIGUITY_PATTERNS:
            if pattern.search(question.strip()):
                return reason
        return None

    # -- classification ----------------------------------------------------
    def _classify_with_llm(
        self, question: str, identifiers: list[str], dates: list[str]
    ) -> RoutingDecision | None:
        try:
            client = self._client
            if client is None:
                from ..llm import build_chat_client

                client = build_chat_client(self.settings)
                self._client = client

            response = client.chat(
                model=self.settings.chat_model,
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM},
                    {"role": "user", "content": f"Question: {question}"},
                ],
                format="json",
                options={
                    "temperature": 0.0,
                    "num_predict": 300,
                    "num_ctx": self.settings.profile.chat_context_tokens,
                },
                keep_alive=self.settings.ollama.keep_alive,
            )
            payload = json.loads(response["message"]["content"])
        except Exception as exc:
            log.warning("LLM routing failed (%s); falling back to heuristics", exc)
            return None

        try:
            route = Route(str(payload.get("route", "")).strip().lower())
        except ValueError:
            log.warning(
                "Router returned an unknown route %r; using heuristics", payload.get("route")
            )
            return None

        # The model must not refuse or clarify: those are rule decisions, and
        # letting the model choose them lets it decline legitimate questions.
        if route is Route.REFUSE:
            log.info("Model proposed REFUSE but no rule matched; treating as document_rag")
            route = Route.DOCUMENT_RAG

        rewritten = self._safe_rewrite(question, payload.get("rewritten_query", ""), identifiers)

        return RoutingDecision(
            route=route,
            original_query=question,
            rewritten_query=rewritten,
            reason=str(payload.get("reason", ""))[:200],
            confidence=float(payload.get("confidence", 0.5) or 0.5),
            decided_by="llm",
            identifiers=identifiers,
            dates=dates,
            document_subquestion=_clean_subquestion(payload.get("document_subquestion"))
            if route is Route.MULTI_SOURCE
            else None,
            data_subquestion=_clean_subquestion(payload.get("data_subquestion"))
            if route is Route.MULTI_SOURCE
            else None,
        )

    def _classify_with_heuristics(
        self, question: str, identifiers: list[str], dates: list[str]
    ) -> RoutingDecision:
        """Deterministic fallback, used when Ollama is unavailable.

        The system must still route when the model is down, rather than
        failing the request outright.
        """
        has_sql = bool(SQL_SIGNALS.search(question))
        has_doc = bool(DOC_SIGNALS.search(question))
        has_multi = bool(MULTI_SIGNALS.search(question))

        if has_multi or (has_sql and has_doc):
            route, reason = Route.MULTI_SOURCE, "mentions both a written rule and a measure"
        elif has_sql:
            route, reason = Route.TEXT_TO_SQL, "asks for a count, total or ranking"
        elif has_doc:
            route, reason = Route.DOCUMENT_RAG, "asks about written policy or process"
        else:
            route, reason = Route.DOCUMENT_RAG, "no strong signal; documents are the safer default"

        return RoutingDecision(
            route=route,
            original_query=question,
            rewritten_query=question,
            reason=reason,
            confidence=0.6,
            decided_by="fallback",
            identifiers=identifiers,
            dates=dates,
        )

    # -- rewriting ---------------------------------------------------------
    def _safe_rewrite(self, original: str, rewritten: str, identifiers: list[str]) -> str:
        """Accept a rewrite only if it kept every identifier.

        A rewrite that drops `INC-2025-0042` looks harmless and destroys the
        exact-match path that is the only reliable way to find that document.
        """
        rewritten = (rewritten or "").strip()
        if not rewritten or len(rewritten) < 5:
            return original

        missing = [i for i in identifiers if i.lower() not in rewritten.lower()]
        if missing:
            log.info("Rewrite dropped identifiers %s; keeping the original question", missing)
            return original
        return rewritten

    # -- extraction --------------------------------------------------------
    @staticmethod
    def extract_identifiers(question: str) -> list[str]:
        found: list[str] = []
        for match in IDENTIFIER_PATTERN.finditer(question):
            token = match.group(0)
            if token not in found:
                found.append(token)
        return found

    @staticmethod
    def extract_dates(question: str) -> list[str]:
        """ISO dates, and month-year or quarter-year references."""
        patterns = [
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b(?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{4}\b",
            r"\bQ[1-4]\s*\d{4}\b",
            r"\b\d{4}\s*Q[1-4]\b",
        ]
        found: list[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, question, re.I):
                token = match.group(0)
                if token not in found:
                    found.append(token)
        return found


def _clean_subquestion(value: object) -> str | None:
    """Accept a subquestion only if it is a usable standalone question."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 8 or text.lower() in ("null", "none", "n/a"):
        return None
    return text


__all__ = ["ROUTER_PROMPT_VERSION", "QueryRouter", "Route", "RoutingDecision"]
