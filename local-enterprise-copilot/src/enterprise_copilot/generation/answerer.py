"""
Answer generation against a local Ollama model.

The flow is deliberately linear and inspectable:

    retrieve -> build evidence package -> prompt -> generate -> validate citations

Nothing is hidden behind a framework, because every one of those steps is
something we need to be able to show, test and measure independently.

Two design choices are worth stating:

**The generator never retrieves.** It receives an `EvidencePackage` and uses
only that. This means the same generator can be driven by document retrieval,
by SQL results, or by both, and it makes "what was the model actually shown?"
a question with an exact answer.

**Validation happens after generation, not during.** The model is not trusted
to police its own citations; `citations.assess_answer` checks them against the
evidence that was supplied. A fabricated citation surfaces as a warning rather
than being quietly corrected, because evaluation needs to see it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from ..config import Settings, get_settings
from ..models.documents import ScoredChunk
from ..models.evidence import (
    Answer,
    AnswerStatus,
    EvidencePackage,
    build_document_evidence,
    detect_conflicts,
)
from .citations import assess_answer
from .prompts import (
    ANSWER_TEMPLATE,
    CLARIFICATION_TEMPLATE,
    NO_EVIDENCE_TEMPLATE,
    PROMPT_VERSION,
    REFUSAL_TEMPLATE,
    SYSTEM_PROMPT,
    format_evidence_block,
)

log = logging.getLogger(__name__)


class GenerationError(RuntimeError):
    """Generation failed, with a recovery hint."""


class Answerer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        import ollama

        self._client = ollama.Client(
            self.settings.ollama.host, timeout=self.settings.ollama.timeout_seconds
        )

    # -- evidence ----------------------------------------------------------
    def build_package(
        self,
        question: str,
        results: list[ScoredChunk],
        *,
        rewritten_question: str | None = None,
        route: str = "document_rag",
    ) -> EvidencePackage:
        evidence = build_document_evidence(results)
        return EvidencePackage(
            question=question,
            rewritten_question=rewritten_question,
            route=route,
            document_evidence=evidence,
            conflicts=detect_conflicts(evidence),
        )

    # -- generation --------------------------------------------------------
    def answer(
        self,
        package: EvidencePackage,
        *,
        trace_id: str = "",
    ) -> Answer:
        """Generate an answer from an evidence package and validate its citations."""
        started = time.perf_counter()

        if package.is_empty:
            # The notes must reach the model here too. Without them it is told
            # only "no evidence", has no idea why, and fills the silence with
            # something plausible - a failed query produced advice to "check
            # the Sales Reports", which do not exist.
            prompt = NO_EVIDENCE_TEMPLATE.format(question=package.question) + _build_notes(package)
            status = AnswerStatus.INSUFFICIENT_EVIDENCE
        else:
            conflict_note = _build_notes(package)
            prompt = ANSWER_TEMPLATE.format(
                question=package.question,
                evidence=format_evidence_block(package.all_evidence),
                conflict_note=conflict_note,
            )
            status = AnswerStatus.ANSWERED

        text, tokens_in, tokens_out = self._generate(prompt)

        answer = Answer(
            question=package.question,
            text=text,
            status=status,
            evidence=package,
            model=self.settings.chat_model,
            prompt_version=PROMPT_VERSION,
            trace_id=trace_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        return assess_answer(answer)

    def stream_answer(self, package: EvidencePackage) -> Iterator[str]:
        """Token stream for the Streamlit UI.

        Citation validation cannot run until the text is complete, so the UI
        must call `assess_answer` on the accumulated text once the stream ends.
        """
        if package.is_empty:
            prompt = NO_EVIDENCE_TEMPLATE.format(question=package.question)
        else:
            conflict_note = _build_notes(package)
            prompt = ANSWER_TEMPLATE.format(
                question=package.question,
                evidence=format_evidence_block(package.all_evidence),
                conflict_note=conflict_note,
            )

        try:
            stream = self._client.chat(
                model=self.settings.chat_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                options=self._options(),
                keep_alive=self.settings.ollama.keep_alive,
                stream=True,
            )
            for part in stream:
                content = part.get("message", {}).get("content", "")
                if content:
                    yield content
        except Exception as exc:
            raise GenerationError(
                f"Streaming failed with {self.settings.chat_model!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    # -- special responses -------------------------------------------------
    def clarify(self, question: str, reason: str) -> Answer:
        """Ask one clarifying question instead of guessing."""
        text, *_ = self._generate(
            CLARIFICATION_TEMPLATE.format(question=question, reason=reason)
        )
        return Answer(
            question=question, text=text, status=AnswerStatus.CLARIFICATION_NEEDED,
            model=self.settings.chat_model, prompt_version=PROMPT_VERSION,
        )

    def refuse(self, question: str, reason: str) -> Answer:
        """Decline a request that must not be carried out.

        Generated rather than templated so the wording fits the request, but
        the *decision* to refuse is made by the router and the SQL guard, never
        by the model.
        """
        text, *_ = self._generate(
            REFUSAL_TEMPLATE.format(question=question, reason=reason)
        )
        return Answer(
            question=question, text=text, status=AnswerStatus.REFUSED,
            model=self.settings.chat_model, prompt_version=PROMPT_VERSION,
            warnings=[f"refused: {reason}"],
        )

    # -- internals ---------------------------------------------------------
    def _options(self) -> dict[str, object]:
        profile = self.settings.profile
        return {
            "num_ctx": profile.chat_context_tokens,
            "temperature": profile.chat_temperature,
            "num_predict": profile.chat_max_tokens,
        }

    def _generate(self, prompt: str) -> tuple[str, int | None, int | None]:
        try:
            response = self._client.chat(
                model=self.settings.chat_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                options=self._options(),
                keep_alive=self.settings.ollama.keep_alive,
            )
        except Exception as exc:
            raise GenerationError(
                f"Generation failed with model {self.settings.chat_model!r} at "
                f"{self.settings.ollama.host}: {type(exc).__name__}: {exc}. "
                f"Check Ollama is running and the model is pulled."
            ) from exc

        return (
            response["message"]["content"].strip(),
            response.get("prompt_eval_count"),
            response.get("eval_count"),
        )


def _build_notes(package: EvidencePackage) -> str:
    """Render conflict warnings and pipeline notes for the prompt.

    Pipeline notes matter as much as conflicts: when a SQL query failed, the
    model must be told, or it will fill the silence with something plausible.
    """
    sections: list[str] = []
    if package.conflicts:
        sections.append(
            "NOTE ON CONFLICTING SOURCES:\n"
            + "\n".join(f"- {c}" for c in package.conflicts)
        )
    if package.notes:
        sections.append(
            "IMPORTANT CONTEXT ABOUT THIS REQUEST:\n"
            + "\n".join(f"- {n}" for n in package.notes)
        )
    return ("\n" + "\n\n".join(sections) + "\n") if sections else ""


__all__ = ["Answerer", "GenerationError"]
