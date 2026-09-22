"""
Citation extraction and validation.

A model asked to cite its sources will sometimes cite one that does not exist.
It will write `[D7]` when only D1 to D5 were supplied, or invent a section
number that sounds right. The citation looks authoritative and is unfalsifiable
by the reader, which makes it more dangerous than an obvious error.

This module closes that gap by checking every citation against the evidence
package **after** generation:

* a citation naming a label that was never supplied is marked invalid
* an answer with no citations at all is flagged as ungrounded
* evidence that was supplied but never used is reported, because a
  consistently unused piece of evidence usually means retrieval is returning
  the wrong thing

Validation runs on the text; it never silently edits the answer. A rewritten
answer would hide the failure from evaluation, which is the opposite of useful.
"""

from __future__ import annotations

import logging
import re

from ..models.evidence import Answer, AnswerStatus, Citation, EvidencePackage

log = logging.getLogger(__name__)

# Any bracketed group, then the evidence labels inside it.
#
# Two patterns rather than one because models do not cite tidily. Asked for
# [D1] they will write [D1, 3.1], [D1 section 3.1] or [D1, D2] - a label mixed
# with a section reference. A single strict pattern requiring every
# comma-separated part to be a label silently drops the whole citation, and the
# answer is then reported as ungrounded when it was in fact correctly cited.
BRACKET_GROUP = re.compile(r"\[([^\[\]]{1,80})\]")
LABEL_TOKEN = re.compile(r"\b([A-Z]\d{1,3})\b")


def extract_citation_ids(text: str) -> list[str]:
    """Pull every cited label out of an answer, in order, without duplicates.

    Tolerant by design: it finds the labels inside a bracket regardless of what
    else the model put there.
    """
    found: list[str] = []
    for group in BRACKET_GROUP.finditer(text):
        for label in LABEL_TOKEN.findall(group.group(1)):
            if label not in found:
                found.append(label)
    return found


def validate_citations(text: str, package: EvidencePackage) -> list[Citation]:
    """Check each citation against the evidence that was actually supplied."""
    valid_ids = package.valid_ids()
    citations: list[Citation] = []

    for evidence_id in extract_citation_ids(text):
        evidence = package.by_id(evidence_id)
        if evidence is not None:
            citations.append(Citation(
                evidence_id=evidence_id, is_valid=True, label=evidence.citation_label()
            ))
        else:
            citations.append(Citation(
                evidence_id=evidence_id,
                is_valid=False,
                reason=(
                    f"cites {evidence_id}, which was not in the evidence "
                    f"(supplied: {', '.join(sorted(valid_ids)) or 'none'})"
                ),
            ))
            log.warning("Fabricated citation %s in answer", evidence_id)

    return citations


def unused_evidence_ids(text: str, package: EvidencePackage) -> list[str]:
    """Evidence supplied to the model but never cited.

    Not an error on its own. Persistently unused evidence is a retrieval
    signal: the pipeline is returning passages the generator does not find
    useful, which usually means `final_evidence_chunks` is too high or ranking
    is off.
    """
    cited = set(extract_citation_ids(text))
    return [e.evidence_id for e in package.all_evidence if e.evidence_id not in cited]


# Phrases that indicate the model declined for lack of evidence.
#
# This is a heuristic and is labelled as one. It exists because retrieval
# always returns its top k, relevant or not, so an evidence package is never
# empty and "the model abstained" is otherwise indistinguishable from "the
# model made an ungrounded claim". Both produce zero citations, but one is
# correct behaviour and the other is a failure, and the status field has to
# tell them apart.
#
# A proper fix is a relevance threshold on the reranker score, so genuinely
# irrelevant evidence is dropped before it reaches the generator. That is a
# retrieval change and is deliberately not being made here, because the
# threshold would have to be chosen against the held-out set.
ABSTENTION_MARKERS = (
    "no policy on", "not mention", "does not mention", "no information",
    "not covered", "no relevant", "cannot answer", "do not have information",
    "does not contain", "is not addressed", "no evidence", "not specified",
    "not available in", "unable to find", "no details",
)


def looks_like_abstention(text: str) -> bool:
    """Heuristic: did the answer decline for lack of evidence?

    Only the opening is checked. An answer that declines says so immediately;
    a well-grounded answer often ends with a caveat like "the evidence does not
    mention mid-term downgrades", which is a note on completeness, not a
    refusal to answer. Scanning the whole text conflates the two, and the
    200-character window is what separates them.
    """
    opening = " ".join(text.lower().split())[:200]
    return any(marker in opening for marker in ABSTENTION_MARKERS)


def assess_answer(answer: Answer) -> Answer:
    """Validate citations and set the answer's status.

    Mutates and returns the answer so the caller has one object carrying the
    text, its citations, and the verdict on both.
    """
    if answer.evidence is None:
        answer.warnings.append("no evidence package attached; citations cannot be validated")
        return answer

    answer.citations = validate_citations(answer.text, answer.evidence)

    invalid = [c for c in answer.citations if not c.is_valid]
    if invalid:
        answer.warnings.append(
            f"{len(invalid)} fabricated citation(s): "
            f"{', '.join(c.evidence_id for c in invalid)}"
        )

    unused = unused_evidence_ids(answer.text, answer.evidence)
    abstained = looks_like_abstention(answer.text)

    if unused and len(unused) == len(answer.evidence.all_evidence) and not abstained:
        # Only a warning when the model made claims without citing anything.
        # Declining to answer is correct behaviour, not an ungrounded claim.
        answer.warnings.append("no evidence was cited; the answer is ungrounded")

    # Status is only inferred when the caller has not already set it to a
    # terminal value such as REFUSED or CLARIFICATION_NEEDED.
    if answer.status is AnswerStatus.ANSWERED:
        if answer.evidence.is_empty or (abstained and not answer.citations):
            answer.status = AnswerStatus.INSUFFICIENT_EVIDENCE
        elif not answer.citations:
            answer.status = AnswerStatus.PARTIAL
        elif answer.evidence.conflicts:
            answer.status = AnswerStatus.CONFLICTING_SOURCES

    return answer


def format_sources(answer: Answer) -> str:
    """Render the source list shown beneath an answer."""
    cited = answer.cited_evidence()
    if not cited:
        return "No sources cited."

    lines = ["Sources:"]
    for evidence in cited:
        detail = evidence.citation_label()
        if evidence.effective_date:
            detail += f" (effective {evidence.effective_date})"
        lines.append(f"  [{evidence.evidence_id}] {detail}")
    return "\n".join(lines)


__all__ = [
    "assess_answer", "extract_citation_ids", "format_sources",
    "unused_evidence_ids", "validate_citations",
]
