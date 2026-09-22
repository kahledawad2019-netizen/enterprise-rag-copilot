"""
Core document and chunk models.

These types are the contract between ingestion, indexing and retrieval. Every
stage of the pipeline moves one of these, so a change here is visible at every
boundary rather than hidden inside a dict.

Three ideas drive the design:

1. **Provenance is not optional.** A chunk that cannot say which document,
   version and section it came from cannot be cited, and an answer without a
   citation is not verifiable. Metadata travels with the chunk, never beside it.

2. **Version and authority are first-class.** The corpus deliberately contains
   superseded policies and contradictions. Retrieval must be able to prefer the
   current, authoritative document rather than whichever chunk happens to embed
   closest.

3. **Access control is carried, not assumed.** Each chunk knows its access
   group and tenant, so permission filtering happens during retrieval instead
   of being bolted on after the model has already seen the text.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DocumentType(StrEnum):
    """What kind of document this is. Drives filtering and prompt framing."""

    PRODUCT_CATALOG = "product_catalog"
    PRICING_POLICY = "pricing_policy"
    REFUND_POLICY = "refund_policy"
    CONTRACT_GUIDE = "contract_guide"
    SLA_POLICY = "sla_policy"
    ONBOARDING_GUIDE = "onboarding_guide"
    SUPPORT_ESCALATION = "support_escalation"
    SECURITY_POLICY = "security_policy"
    INCIDENT_PROCEDURE = "incident_procedure"
    INCIDENT_POSTMORTEM = "incident_postmortem"
    SALES_POLICY = "sales_policy"
    KPI_GLOSSARY = "kpi_glossary"
    CHURN_GUIDE = "churn_guide"
    HEALTH_METHODOLOGY = "health_methodology"
    SECURITY_TEST = "security_test"


class AuthorityLevel(StrEnum):
    """How much weight a document carries when sources disagree.

    The corpus contains genuine contradictions. When a `policy` document and a
    `guidance` document conflict, the policy wins and the answer says so.
    """

    POLICY = "policy"  # binding, contractual
    STANDARD = "standard"  # internal standard, must follow
    GUIDANCE = "guidance"  # recommended practice
    REFERENCE = "reference"  # informational
    DRAFT = "draft"  # not yet in force

    @property
    def rank(self) -> int:
        return {"policy": 5, "standard": 4, "guidance": 3, "reference": 2, "draft": 1}[self.value]


class DocumentStatus(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    DRAFT = "draft"
    ARCHIVED = "archived"


class DocumentMetadata(BaseModel):
    """Everything known about a document apart from its text.

    Parsed from the YAML front matter of each Markdown file, so the metadata
    lives with the content and cannot drift away from it.
    """

    model_config = {"frozen": False}

    doc_id: str = Field(description="Stable identifier, e.g. DOC-REF-001")
    title: str
    doc_type: DocumentType
    version: str = Field(description="Semantic-ish version, e.g. '2.1'")
    effective_date: date
    expiry_date: date | None = None
    status: DocumentStatus = DocumentStatus.CURRENT
    authority: AuthorityLevel = AuthorityLevel.REFERENCE

    department: str = "General"
    owner: str = "Unassigned"

    # Access control, applied as a Qdrant payload filter at query time.
    access_group: str = "public"
    tenant: str = "all"  # "all" or a tenant_code such as NWC-EU

    # Lineage
    supersedes: str | None = None
    superseded_by: str | None = None
    related_docs: list[str] = Field(default_factory=list)

    source_path: str = ""
    content_hash: str = ""
    language: str = "en"
    tags: list[str] = Field(default_factory=list)

    @field_validator("doc_id")
    @classmethod
    def _validate_doc_id(cls, v: str) -> str:
        # Two shapes are valid: DOC-REF-001 for policies, and the year-scoped
        # DOC-PM-2025-0042 used by postmortems, which must match the
        # postmortem_doc_id values already stored in support.incidents.
        if not re.fullmatch(r"DOC-[A-Z]{2,4}(?:-[0-9]{3,4})+", v):
            raise ValueError(f"doc_id {v!r} must look like DOC-REF-001 or DOC-PM-2025-0042")
        return v

    @property
    def is_current(self) -> bool:
        return self.status == DocumentStatus.CURRENT

    def citation_label(self) -> str:
        """Short human-readable label shown next to an answer."""
        return f"{self.title} v{self.version}"


class ParsedDocument(BaseModel):
    """A document after parsing and cleaning, before chunking.

    `text` is retained so the extracted content can be inspected before it is
    embedded. Debugging a bad answer almost always starts here: was the text
    extracted correctly, or did the parser mangle it?
    """

    metadata: DocumentMetadata
    text: str
    sections: list[Section] = Field(default_factory=list)
    page_count: int | None = None
    parser: str = "markdown"
    warnings: list[str] = Field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    def compute_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


class Section(BaseModel):
    """A heading and the body beneath it.

    Chunking works on sections rather than raw character offsets, which is what
    keeps a numbered policy clause or an FAQ answer intact.
    """

    heading: str
    level: int = Field(ge=1, le=6)
    text: str
    start_char: int = 0
    end_char: int = 0
    breadcrumb: list[str] = Field(default_factory=list)

    @property
    def path(self) -> str:
        """`Refunds > Enterprise plans > Annual contracts`."""
        return " > ".join([*self.breadcrumb, self.heading]) if self.heading else ""


class Chunk(BaseModel):
    """An indexed unit of text, carrying its full provenance.

    `chunk_id` is deterministic: the same document content always produces the
    same ids, so re-indexing replaces chunks in place instead of duplicating
    them, and a stale chunk can be identified and deleted.
    """

    chunk_id: str
    doc_id: str
    text: str

    # Where it came from
    section_path: str = ""
    heading: str = ""
    page: int | None = None
    chunk_index: int = 0
    parent_chunk_id: str | None = None

    # Copied from the document so retrieval can filter without a second lookup.
    title: str = ""
    doc_type: str = ""
    version: str = ""
    effective_date: str = ""
    status: str = "current"
    authority: str = "reference"
    access_group: str = "public"
    tenant: str = "all"
    department: str = ""

    token_estimate: int = 0
    content_hash: str = ""  # hash of THIS chunk's text
    doc_content_hash: str = ""  # hash of the whole source document
    index_version: str = "v1"

    def payload(self) -> dict[str, Any]:
        """Flat dict stored in Qdrant alongside the vector."""
        return self.model_dump(exclude={"text"}) | {"text": self.text}

    @staticmethod
    def make_id(doc_id: str, version: str, section_path: str, chunk_index: int) -> str:
        """Deterministic id. Changing the text of a section changes its chunks'
        content hash but not their ids, so updates are replacements."""
        raw = f"{doc_id}|{version}|{section_path}|{chunk_index}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


class RetrievalMethod(StrEnum):
    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"
    RERANKED = "reranked"
    PARENT = "parent_expansion"


class ScoredChunk(BaseModel):
    """A chunk with the scores that got it here.

    Every score is kept rather than collapsed into one number, because the
    retrieval debugger has to show *why* a chunk was selected: it may rank
    first on BM25 and twentieth on dense, and that difference is the finding.
    """

    chunk: Chunk
    score: float
    method: RetrievalMethod
    dense_score: float | None = None
    sparse_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    dense_rank: int | None = None
    sparse_rank: int | None = None

    def explain(self) -> str:
        parts = [f"method={self.method.value}", f"score={self.score:.4f}"]
        if self.dense_score is not None:
            parts.append(f"dense={self.dense_score:.4f}(#{self.dense_rank})")
        if self.sparse_score is not None:
            parts.append(f"sparse={self.sparse_score:.4f}(#{self.sparse_rank})")
        if self.rerank_score is not None:
            parts.append(f"rerank={self.rerank_score:.4f}")
        return " ".join(parts)


class IndexManifest(BaseModel):
    """A record of what is in the index and how it was built.

    Without this, "why did the answer change?" is unanswerable. With it, the
    embedding model, chunking parameters and document versions that produced
    the current index are all recoverable.
    """

    index_version: str
    collection: str
    embedding_model: str
    embedding_dimension: int
    chunk_target_tokens: int
    chunk_overlap_tokens: int
    built_at_utc: str
    document_count: int = 0
    chunk_count: int = 0
    documents: dict[str, str] = Field(
        default_factory=dict,
        description="doc_id -> content_hash, used to detect changes on re-index",
    )

    def is_compatible_with(self, other: IndexManifest) -> tuple[bool, str]:
        """Can an index built under `other` be queried by this configuration?

        A mismatched embedding model or dimension silently returns nonsense
        rather than failing, which is the worst possible failure mode, so it is
        checked explicitly before any query runs.
        """
        if self.embedding_model != other.embedding_model:
            return False, (
                f"embedding model changed: index built with {other.embedding_model!r}, "
                f"now configured for {self.embedding_model!r}. Rebuild the index."
            )
        if self.embedding_dimension != other.embedding_dimension:
            return False, (
                f"embedding dimension changed: {other.embedding_dimension} -> "
                f"{self.embedding_dimension}. Rebuild the index."
            )
        if self.index_version != other.index_version:
            return False, (f"index version changed: {other.index_version} -> {self.index_version}.")
        return True, "compatible"


def estimate_tokens(text: str) -> int:
    """Rough token count without loading a tokenizer.

    English averages ~4 characters per token. This is used only for chunk
    sizing, where being 10% out changes nothing; it is never used for context
    budgeting, where the real tokenizer is used instead.
    """
    return max(1, len(text) // 4)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


__all__ = [
    "AuthorityLevel",
    "Chunk",
    "DocumentMetadata",
    "DocumentStatus",
    "DocumentType",
    "IndexManifest",
    "ParsedDocument",
    "Path",
    "RetrievalMethod",
    "ScoredChunk",
    "Section",
    "estimate_tokens",
    "slugify",
]
