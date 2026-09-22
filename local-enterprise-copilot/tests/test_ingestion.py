"""
Ingestion tests: parsing, cleaning, chunking, and the corpus invariants the
evaluation set depends on.

The chunking tests are the important ones. A chunker that splits a policy
clause in half produces evidence that reads as complete but is not, and the
resulting wrong answer is very hard to trace back to its cause.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from enterprise_copilot.config import PROJECT_ROOT
from enterprise_copilot.ingestion.chunking import ChunkingConfig, StructureAwareChunker
from enterprise_copilot.ingestion.parsers import (
    MarkdownParser,
    ParserError,
    ParserRegistry,
    clean_text,
    extract_sections,
    strip_headers_footers,
)
from enterprise_copilot.models.documents import (
    AuthorityLevel,
    DocumentStatus,
    estimate_tokens,
)

DOCS_DIR = PROJECT_ROOT / "data" / "documents"


@pytest.fixture(scope="module")
def corpus() -> list[Path]:
    paths = sorted(DOCS_DIR.glob("*.md"))
    if not paths:
        pytest.skip(f"No documents in {DOCS_DIR}")
    return paths


@pytest.fixture(scope="module")
def parsed(corpus: list[Path]) -> list:
    registry = ParserRegistry()
    return [registry.parse(p) for p in corpus]


class TestCleaning:
    def test_collapses_blank_line_runs(self) -> None:
        assert clean_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_normalises_smart_quotes(self) -> None:
        """A query typed with a plain apostrophe must match the document text."""
        assert clean_text("the customer\u2019s plan") == "the customer's plan"

    def test_strips_trailing_whitespace(self) -> None:
        assert clean_text("line one   \nline two") == "line one\nline two"

    def test_preserves_table_pipes_and_indentation(self) -> None:
        text = "| a | b |\n|---|---|\n| 1 | 2 |"
        assert clean_text(text) == text

    def test_removes_repeated_headers(self) -> None:
        pages = [f"Northwind Cloud Confidential\nBody {n}\nPage {n}" for n in range(5)]
        cleaned = strip_headers_footers(pages)
        assert all("Northwind Cloud Confidential" not in p for p in cleaned)
        assert all(f"Body {n}" in cleaned[n] for n in range(5))

    def test_keeps_headers_in_short_documents(self) -> None:
        """Two pages is not enough evidence that a line is furniture."""
        pages = ["Header\nBody one", "Header\nBody two"]
        assert strip_headers_footers(pages) == pages


class TestSectionExtraction:
    def test_builds_breadcrumbs_from_heading_levels(self) -> None:
        text = "# Policy\n\nIntro\n\n## Refunds\n\nBody\n\n### Annual\n\nDetail"
        sections = extract_sections(text)
        annual = next(s for s in sections if s.heading == "Annual")
        assert annual.breadcrumb == ["Policy", "Refunds"]
        assert annual.path == "Policy > Refunds > Annual"

    def test_handles_document_with_no_headings(self) -> None:
        sections = extract_sections("Just a paragraph with no heading.")
        assert len(sections) == 1
        assert sections[0].heading == ""

    def test_pops_stack_when_level_decreases(self) -> None:
        text = "# A\n\nx\n\n## B\n\ny\n\n# C\n\nz"
        sections = extract_sections(text)
        c = next(s for s in sections if s.heading == "C")
        assert c.breadcrumb == []


class TestMarkdownParser:
    def test_rejects_a_file_without_front_matter(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        path.write_text("# No front matter\n\nBody", encoding="utf-8")
        with pytest.raises(ParserError, match="front matter"):
            MarkdownParser().parse(path)

    def test_rejects_an_invalid_doc_id(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        path.write_text(
            "---\ndoc_id: NOT-VALID\ntitle: X\ndoc_type: refund_policy\n"
            "effective_date: 2025-01-01\n---\n\nBody",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="doc_id"):
            MarkdownParser().parse(path)

    def test_content_hash_is_stable(self, corpus: list[Path]) -> None:
        parser = MarkdownParser()
        first = parser.parse(corpus[0]).metadata.content_hash
        second = parser.parse(corpus[0]).metadata.content_hash
        assert first == second and len(first) == 16

    def test_unsupported_extension_is_reported_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.rtf"
        path.write_text("x", encoding="utf-8")
        with pytest.raises(ParserError, match="unsupported file type"):
            ParserRegistry().parse(path)


class TestCorpusInvariants:
    """Properties the evaluation set relies on. If these break, evals lie."""

    def test_every_document_parses(self, parsed: list) -> None:
        assert len(parsed) >= 19

    def test_doc_ids_are_unique(self, parsed: list) -> None:
        ids = [d.metadata.doc_id for d in parsed]
        assert len(ids) == len(set(ids)), "duplicate doc_id in the corpus"

    def test_superseded_versions_exist(self, parsed: list) -> None:
        """Version-sensitive evaluation needs both versions present."""
        superseded = {
            d.metadata.doc_id for d in parsed if d.metadata.status == DocumentStatus.SUPERSEDED
        }
        assert {"DOC-REF-000", "DOC-SLA-000"} <= superseded

    def test_superseded_documents_point_at_their_replacement(self, parsed: list) -> None:
        by_id = {d.metadata.doc_id: d.metadata for d in parsed}
        for doc_id in ("DOC-REF-000", "DOC-SLA-000"):
            metadata = by_id[doc_id]
            assert metadata.superseded_by in by_id, f"{doc_id} points at a missing document"

    def test_authority_conflict_is_present(self, parsed: list) -> None:
        """Pricing policy (policy) vs sales guidance (guidance) on discounts."""
        by_id = {d.metadata.doc_id: d.metadata for d in parsed}
        assert by_id["DOC-PRC-001"].authority == AuthorityLevel.POLICY
        assert by_id["DOC-SAL-001"].authority == AuthorityLevel.GUIDANCE
        assert by_id["DOC-PRC-001"].authority.rank > by_id["DOC-SAL-001"].authority.rank

    def test_postmortem_ids_match_the_database(self, parsed: list) -> None:
        """support.incidents.postmortem_doc_id references these exact ids."""
        ids = {d.metadata.doc_id for d in parsed}
        assert {"DOC-PM-2025-0042", "DOC-PM-2025-0031", "DOC-PM-2026-0007"} <= ids

    def test_injection_document_is_marked_and_restricted(self, parsed: list) -> None:
        doc = next(d for d in parsed if d.metadata.doc_id == "DOC-TST-001")
        assert doc.metadata.access_group == "security"
        assert "DO NOT FOLLOW" in doc.text.upper()

    def test_no_document_parses_with_warnings_about_lost_content(self, parsed: list) -> None:
        for document in parsed:
            for warning in document.warnings:
                assert "no text" not in warning.lower(), f"{document.metadata.doc_id}: {warning}"


class TestChunking:
    @pytest.fixture(scope="class")
    def chunker(self) -> StructureAwareChunker:
        return StructureAwareChunker(ChunkingConfig())

    def test_chunk_ids_are_deterministic(self, parsed: list, chunker) -> None:
        first = [c.chunk_id for c in chunker.chunk_document(parsed[0])]
        second = [c.chunk_id for c in chunker.chunk_document(parsed[0])]
        assert first == second

    def test_chunk_ids_are_unique_across_the_corpus(self, parsed: list, chunker) -> None:
        ids = [c.chunk_id for d in parsed for c in chunker.chunk_document(d)]
        assert len(ids) == len(set(ids))

    def test_every_chunk_carries_provenance(self, parsed: list, chunker) -> None:
        """A chunk that cannot say where it came from cannot be cited."""
        for document in parsed:
            for chunk in chunker.chunk_document(document):
                assert chunk.doc_id and chunk.title and chunk.version
                assert chunk.doc_content_hash, "needed for incremental indexing"
                assert chunk.access_group and chunk.tenant

    def test_breadcrumb_is_prefixed_to_the_text(self, parsed: list, chunker) -> None:
        document = next(d for d in parsed if d.metadata.doc_id == "DOC-REF-001")
        chunks = chunker.chunk_document(document)
        with_path = [c for c in chunks if c.section_path]
        assert with_path
        assert all(c.text.startswith("[") for c in with_path)

    def test_refund_clause_is_not_split(self, parsed: list, chunker) -> None:
        """The clause the evaluation set asserts on must survive intact."""
        document = next(d for d in parsed if d.metadata.doc_id == "DOC-REF-001")
        chunks = chunker.chunk_document(document)
        # Whitespace is normalised because the source Markdown hard-wraps the
        # clause across two lines; the test is about the clause surviving
        # chunking, not about how the file happens to be wrapped.
        matching = [
            c
            for c in chunks
            if "pro-rata basis within the first 30 days" in " ".join(c.text.split())
        ]
        assert matching, "the 30-day refund clause was split across chunks"

    def test_table_rows_stay_with_their_header(self, parsed: list, chunker) -> None:
        """A retrieved '| 15 minutes |' with no header is unusable."""
        document = next(d for d in parsed if d.metadata.doc_id == "DOC-SLA-001")
        for chunk in chunker.chunk_document(document):
            rows = [line for line in chunk.text.split("\n") if line.strip().startswith("|")]
            if len(rows) > 2:
                assert any("Priority" in r or "Tier" in r or "---" in r for r in rows), (
                    f"table fragment without a header in {chunk.chunk_id}"
                )

    def test_no_chunk_exceeds_the_hard_ceiling(self, parsed: list, chunker) -> None:
        for document in parsed:
            for chunk in chunker.chunk_document(document):
                assert chunk.token_estimate <= chunker.config.max_tokens * 1.4, (
                    f"{chunk.chunk_id} is {chunk.token_estimate} tokens"
                )

    def test_split_sections_get_a_parent(self, parsed: list, chunker) -> None:
        small = StructureAwareChunker(ChunkingConfig(target_tokens=60, max_tokens=80))
        document = next(d for d in parsed if d.metadata.doc_id == "DOC-SLA-001")
        chunks = small.chunk_document(document)
        children = [c for c in chunks if c.parent_chunk_id]
        assert children, "no parent-child relationships were created"
        parents = {c.chunk_id for c in chunks}
        assert all(c.parent_chunk_id in parents for c in children)

    def test_empty_sections_are_skipped(self, chunker) -> None:
        from enterprise_copilot.ingestion.parsers import MarkdownParser  # noqa: F401
        from enterprise_copilot.models.documents import (
            DocumentMetadata,
            ParsedDocument,
            Section,
        )

        document = ParsedDocument(
            metadata=DocumentMetadata(
                doc_id="DOC-TMP-001",
                title="T",
                doc_type="refund_policy",
                version="1.0",
                effective_date="2025-01-01",
            ),
            text="x",
            sections=[Section(heading="Empty", level=2, text="   ")],
        )
        assert chunker.chunk_document(document) == []


class TestTokenEstimate:
    def test_scales_with_length(self) -> None:
        assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)

    def test_never_returns_zero(self) -> None:
        assert estimate_tokens("") >= 1
