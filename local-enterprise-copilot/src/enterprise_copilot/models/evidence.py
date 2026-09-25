"""
Evidence and answer models.

The central idea: **an answer is only as good as the evidence it can point at.**
Everything the generator is allowed to say must trace back to a piece of
evidence in this package, and `citations.py` enforces that after generation.

Two kinds of evidence exist and are deliberately kept distinct:

* `DOCUMENT` — a passage from a policy or guide. It states what the company
  *says*.
* `SQL_RESULT` — rows returned by a validated read-only query. It states what
  the data *shows*.

Blurring the two is a real failure mode. "Enterprise customers get a 15-minute
first response" is a policy claim; "Acme's average first response was 42
minutes" is a data claim. An answer that presents one as the other is wrong in
a way that reads as authoritative, which is worse than being obviously wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .documents import ScoredChunk


class EvidenceType(StrEnum):
    DOCUMENT = "document"
    SQL_RESULT = "sql_result"
    GLOSSARY = "glossary"


class Evidence(BaseModel):
    """One citable unit of support for an answer."""

    evidence_id: str = Field(description="Citation label used in the answer, e.g. 'D1' or 'S1'")
    evidence_type: EvidenceType

    text: str = Field(description="What the generator sees")
    structured: dict[str, Any] | None = Field(
        default=None, description="Rows and SQL for SQL_RESULT evidence"
    )

    # Provenance
    source_id: str = ""  # doc_id, or the view/table queried
    source_title: str = ""
    version: str = ""
    section: str = ""
    page: int | None = None
    effective_date: str = ""
    authority: str = ""

    # How it was retrieved, carried through for the debugger and the trace
    retrieval_method: str = ""
    retrieval_score: float | None = None
    rerank_score: float | None = None

    # Permission metadata, kept so an answer can be audited after the fact
    access_group: str = ""
    tenant: str = ""

    def citation_label(self) -> str:
        parts = [self.source_title or self.source_id]
        if self.version:
            parts.append(f"v{self.version}")
        if self.section:
            parts.append(self.section)
        return ", ".join(parts)

    def short_reference(self) -> str:
        return f"[{self.evidence_id}] {self.citation_label()}"


class EvidencePackage(BaseModel):
    """Everything the generator is allowed to use for one question.

    Built once, then frozen in practice: the generator receives this and
    nothing else, so any claim it makes that is not supported here is
    detectable after the fact.
    """

    question: str
    rewritten_question: str | None = None
    route: str = "document_rag"

    document_evidence: list[Evidence] = Field(default_factory=list)
    sql_evidence: list[Evidence] = Field(default_factory=list)
    glossary_evidence: list[Evidence] = Field(default_factory=list)

    conflicts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    built_at_utc: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    @property
    def all_evidence(self) -> list[Evidence]:
        return [*self.document_evidence, *self.sql_evidence, *self.glossary_evidence]

    @property
    def is_empty(self) -> bool:
        return not self.all_evidence

    def by_id(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.all_evidence if e.evidence_id == evidence_id), None)

    def valid_ids(self) -> set[str]:
        return {e.evidence_id for e in self.all_evidence}

    def sources_summary(self) -> list[dict[str, str]]:
        """Compact source list for the UI."""
        return [
            {
                "id": e.evidence_id,
                "type": e.evidence_type.value,
                "label": e.citation_label(),
                "version": e.version,
                "section": e.section,
            }
            for e in self.all_evidence
        ]


class Citation(BaseModel):
    """A citation extracted from a generated answer, after validation."""

    evidence_id: str
    is_valid: bool
    reason: str = ""
    label: str = ""


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    PARTIAL = "partial"  # answered, but some of the question is unsupported
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_SOURCES = "conflicting_sources"
    REFUSED = "refused"
    CLARIFICATION_NEEDED = "clarification_needed"
    DIRECT = "direct"  # small talk / capabilities; no evidence was needed


class Answer(BaseModel):
    """A generated answer with its citations and validation verdict."""

    question: str
    text: str
    status: AnswerStatus = AnswerStatus.ANSWERED

    citations: list[Citation] = Field(default_factory=list)
    evidence: EvidencePackage | None = None

    generated_sql: str | None = None
    sql_row_count: int | None = None

    model: str = ""
    prompt_version: str = ""
    trace_id: str = ""
    latency_ms: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None

    warnings: list[str] = Field(default_factory=list)

    @property
    def has_invalid_citations(self) -> bool:
        return any(not c.is_valid for c in self.citations)

    @property
    def is_grounded(self) -> bool:
        """An answer is grounded when it cites at least one real piece of evidence.

        An uncited answer is not necessarily wrong, but it is unverifiable,
        which for this system is the same thing.
        """
        return bool(self.citations) and not self.has_invalid_citations

    def cited_evidence(self) -> list[Evidence]:
        if self.evidence is None:
            return []
        return [
            e
            for c in self.citations
            if c.is_valid and (e := self.evidence.by_id(c.evidence_id)) is not None
        ]


def build_document_evidence(results: list[ScoredChunk], *, start_index: int = 1) -> list[Evidence]:
    """Turn retrieved chunks into citable evidence, labelled D1, D2, ..."""
    evidence: list[Evidence] = []
    for offset, item in enumerate(results):
        chunk = item.chunk
        evidence.append(
            Evidence(
                evidence_id=f"D{start_index + offset}",
                evidence_type=EvidenceType.DOCUMENT,
                text=chunk.text,
                source_id=chunk.doc_id,
                source_title=chunk.title,
                version=chunk.version,
                section=chunk.section_path,
                page=chunk.page,
                effective_date=chunk.effective_date,
                authority=chunk.authority,
                retrieval_method=item.method.value,
                retrieval_score=item.score,
                rerank_score=item.rerank_score,
                access_group=chunk.access_group,
                tenant=chunk.tenant,
            )
        )
    return evidence


def detect_conflicts(evidence: list[Evidence]) -> list[str]:
    """Flag evidence that disagrees, so the answer can say so rather than pick.

    Two signals are used, both structural rather than semantic:

    * the same document appearing at two different versions
    * two documents of the same type at different authority levels

    This is intentionally shallow. Detecting semantic contradiction reliably
    would need another model, and a false "these sources conflict" is more
    damaging than a missed one, because it undermines a correct answer.
    """
    conflicts: list[str] = []

    by_source: dict[str, set[str]] = {}
    for item in evidence:
        if item.evidence_type is EvidenceType.DOCUMENT and item.version:
            by_source.setdefault(item.source_id, set()).add(item.version)
    for source_id, versions in by_source.items():
        if len(versions) > 1:
            conflicts.append(
                f"{source_id} appears at versions {', '.join(sorted(versions))}; "
                f"the most recent effective date takes precedence."
            )

    by_title: dict[str, set[str]] = {}
    for item in evidence:
        if item.evidence_type is EvidenceType.DOCUMENT and item.authority:
            by_title.setdefault(item.source_title, set()).add(item.authority)

    authorities = {
        item.authority
        for item in evidence
        if item.evidence_type is EvidenceType.DOCUMENT and item.authority
    }
    if "policy" in authorities and "guidance" in authorities:
        conflicts.append(
            "Evidence includes both binding policy and advisory guidance. "
            "Where they differ, the policy is authoritative."
        )

    return conflicts


__all__ = [
    "Answer",
    "AnswerStatus",
    "Citation",
    "Evidence",
    "EvidencePackage",
    "EvidenceType",
    "build_document_evidence",
    "detect_conflicts",
]
