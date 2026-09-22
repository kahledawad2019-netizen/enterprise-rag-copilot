"""
Structure-aware chunking.

Chunking is the single highest-leverage decision in a RAG system, and the
naive approach — split every N characters — is where most systems quietly lose
their accuracy. A fixed-size split will happily cut a refund clause in half, so
the retrieved chunk says "may be refunded on a pro-rata basis within the first"
and the model invents the rest.

This chunker works on the document's own structure:

* **Sections are the unit.** A section that fits becomes one chunk, intact.
* **Oversized sections split on paragraphs**, never mid-sentence.
* **Tables stay whole**, and the header row is repeated onto every piece if a
  large table must be split, so "| 15 minutes |" is never orphaned from the
  column it belongs to.
* **Numbered clauses are kept together** with their clause number.
* **Every chunk is prefixed with its breadcrumb**, so an embedding of
  "3.1 Enterprise annual plans may be refunded..." also carries
  "Refund and Credit Policy > Annual plans".
* **Parent-child links** let retrieval return a precise chunk and then expand
  to the whole section when the answer needs surrounding context.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..models.documents import Chunk, ParsedDocument, Section, estimate_tokens

log = logging.getLogger(__name__)

TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
CLAUSE_START = re.compile(r"^\s*(\d+\.\d+|\d+\))\s+")
LIST_ITEM = re.compile(r"^\s*([-*+]|\d+\.)\s+")


@dataclass(frozen=True)
class ChunkingConfig:
    """Chunk sizing. Tuned against evals/, not guessed.

    `target_tokens` is a target, not a hard cap: a section slightly over the
    target is kept whole rather than split, because a clean boundary is worth
    more than an exact size.
    """

    target_tokens: int = 400
    overlap_tokens: int = 60
    min_tokens: int = 50
    max_tokens: int = 900  # hard ceiling; beyond this we must split
    include_breadcrumb: bool = True
    keep_tables_whole: bool = True

    @classmethod
    def from_settings(cls, settings) -> ChunkingConfig:
        retrieval = settings.retrieval
        return cls(
            target_tokens=retrieval.chunk_target_tokens,
            overlap_tokens=retrieval.chunk_overlap_tokens,
            min_tokens=retrieval.chunk_min_tokens,
        )


class StructureAwareChunker:
    def __init__(self, config: ChunkingConfig | None = None) -> None:
        self.config = config or ChunkingConfig()

    # -- public API --------------------------------------------------------
    def chunk_document(self, document: ParsedDocument, index_version: str = "v1") -> list[Chunk]:
        metadata = document.metadata
        chunks: list[Chunk] = []
        counter = 0

        for section in document.sections:
            if not section.text.strip():
                continue

            pieces = self._split_section(section)

            # When a section had to be split, the whole section becomes the
            # parent so retrieval can expand a precise hit back to full context.
            parent_id: str | None = None
            if len(pieces) > 1:
                parent_id = Chunk.make_id(metadata.doc_id, metadata.version, section.path, -1)
                chunks.append(
                    self._build(
                        metadata,
                        section,
                        self._decorate(section, section.text),
                        counter=-1,
                        index_version=index_version,
                        parent_id=None,
                    )
                )

            for piece in pieces:
                chunk = self._build(
                    metadata,
                    section,
                    self._decorate(section, piece),
                    counter=counter,
                    index_version=index_version,
                    parent_id=parent_id,
                )
                if chunk.token_estimate >= self.config.min_tokens or len(pieces) == 1:
                    chunks.append(chunk)
                    counter += 1
                else:
                    log.debug(
                        "Dropping %d-token fragment in %s", chunk.token_estimate, section.path
                    )

        if not chunks:
            log.warning("Document %s produced no chunks", metadata.doc_id)
        return chunks

    # -- internals ---------------------------------------------------------
    def _decorate(self, section: Section, text: str) -> str:
        """Prefix the breadcrumb so the chunk is self-describing once retrieved."""
        if not self.config.include_breadcrumb or not section.path:
            return text.strip()
        return f"[{section.path}]\n\n{text.strip()}"

    def _split_section(self, section: Section) -> list[str]:
        text = section.text.strip()
        if estimate_tokens(text) <= self.config.max_tokens:
            return [text]

        blocks = self._to_blocks(text)
        pieces: list[str] = []
        current: list[str] = []
        current_tokens = 0

        for block in blocks:
            block_tokens = estimate_tokens(block)

            # A single block larger than the ceiling has to be broken down.
            if block_tokens > self.config.max_tokens:
                if current:
                    pieces.append("\n\n".join(current))
                    current, current_tokens = [], 0
                pieces.extend(self._split_oversized_block(block))
                continue

            if current_tokens + block_tokens > self.config.target_tokens and current:
                pieces.append("\n\n".join(current))
                current, current_tokens = self._carry_overlap(current)

            current.append(block)
            current_tokens += block_tokens

        if current:
            pieces.append("\n\n".join(current))
        return [p for p in pieces if p.strip()]

    def _to_blocks(self, text: str) -> list[str]:
        """Group lines into atomic blocks: tables, clauses, lists, paragraphs.

        A block is something that must not be cut. Grouping first means the
        size logic never has to reason about Markdown syntax.
        """
        lines = text.split("\n")
        blocks: list[str] = []
        buffer: list[str] = []
        in_table = False

        def flush() -> None:
            nonlocal buffer
            if buffer:
                joined = "\n".join(buffer).strip()
                if joined:
                    blocks.append(joined)
                buffer = []

        for line in lines:
            is_table_row = bool(TABLE_ROW.match(line))

            if is_table_row and not in_table:
                flush()  # a table starts a new block
                in_table = True
            elif in_table and not is_table_row:
                if line.strip():
                    flush()  # the table ended
                    in_table = False
                else:
                    continue  # blank line inside a table: keep going

            # A new numbered clause starts a new block unless we are in a table.
            if not in_table and CLAUSE_START.match(line) and buffer:
                flush()

            if not line.strip() and not in_table:
                flush()
                continue

            buffer.append(line)

        flush()
        return blocks

    def _split_oversized_block(self, block: str) -> list[str]:
        """Break a block that exceeds the ceiling.

        Tables split on row boundaries with the header repeated. Everything
        else splits on sentence boundaries, never mid-sentence.
        """
        lines = block.split("\n")
        if self.config.keep_tables_whole and len(lines) > 2 and TABLE_ROW.match(lines[0]):
            return self._split_table(lines)

        sentences = re.split(r"(?<=[.!?])\s+", block)
        pieces: list[str] = []
        current: list[str] = []
        tokens = 0
        for sentence in sentences:
            sentence_tokens = estimate_tokens(sentence)
            if tokens + sentence_tokens > self.config.target_tokens and current:
                pieces.append(" ".join(current))
                current, tokens = [], 0
            current.append(sentence)
            tokens += sentence_tokens
        if current:
            pieces.append(" ".join(current))
        return pieces

    def _split_table(self, lines: list[str]) -> list[str]:
        """Split a large table, repeating the header on each piece.

        Without the repeated header, a retrieved fragment reads
        `| Enterprise | 15 minutes |` with no indication that the second column
        is the first-response target. With it, the fragment is still readable.
        """
        header = (
            lines[:2]
            if len(lines) > 1 and set(lines[1].replace("|", "").strip()) <= {"-", " ", ":"}
            else lines[:1]
        )
        body = lines[len(header) :]

        header_tokens = estimate_tokens("\n".join(header))
        pieces: list[str] = []
        current: list[str] = []
        tokens = header_tokens

        for row in body:
            row_tokens = estimate_tokens(row)
            if tokens + row_tokens > self.config.target_tokens and current:
                pieces.append("\n".join(header + current))
                current, tokens = [], header_tokens
            current.append(row)
            tokens += row_tokens

        if current:
            pieces.append("\n".join(header + current))
        return pieces

    def _carry_overlap(self, blocks: list[str]) -> tuple[list[str], int]:
        """Carry the tail of the previous chunk into the next one.

        Overlap exists so a fact that sits on a boundary appears whole in at
        least one chunk. It is capped at one block to avoid duplicating large
        amounts of text across the index.
        """
        if not blocks or self.config.overlap_tokens <= 0:
            return [], 0
        tail = blocks[-1]
        if estimate_tokens(tail) <= self.config.overlap_tokens:
            return [tail], estimate_tokens(tail)
        return [], 0

    def _build(
        self,
        metadata,
        section: Section,
        text: str,
        *,
        counter: int,
        index_version: str,
        parent_id: str | None,
    ) -> Chunk:
        import hashlib

        return Chunk(
            chunk_id=Chunk.make_id(metadata.doc_id, metadata.version, section.path, counter),
            doc_id=metadata.doc_id,
            text=text,
            section_path=section.path,
            heading=section.heading,
            chunk_index=counter,
            parent_chunk_id=parent_id,
            title=metadata.title,
            doc_type=str(metadata.doc_type),
            version=metadata.version,
            effective_date=metadata.effective_date.isoformat(),
            status=str(metadata.status),
            authority=str(metadata.authority),
            access_group=metadata.access_group,
            tenant=metadata.tenant,
            department=metadata.department,
            token_estimate=estimate_tokens(text),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            doc_content_hash=metadata.content_hash,
            index_version=index_version,
        )


__all__ = ["ChunkingConfig", "StructureAwareChunker"]
