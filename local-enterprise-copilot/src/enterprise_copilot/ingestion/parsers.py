"""
Document parsers.

Each parser turns a file into a `ParsedDocument`: cleaned text, a section tree,
and validated metadata. Markdown is the primary format; PDF and DOCX exist so
the pipeline handles the formats a real enterprise corpus actually contains.

Two rules apply to every parser:

* **Never silently lose content.** If something cannot be extracted, record a
  warning on the document rather than dropping it quietly. A missing paragraph
  that nobody noticed is far worse than a visible warning.
* **Keep structure.** Headings, numbered clauses and table rows carry meaning.
  A parser that flattens them to a wall of text destroys the signal the chunker
  depends on.
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from ..models.documents import (
    DocumentMetadata,
    ParsedDocument,
    Section,
)

log = logging.getLogger(__name__)

FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


class ParserError(Exception):
    """A document could not be parsed. Carries the path for triage."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path.name}: {reason}")
        self.path = path
        self.reason = reason


class DocumentParser(ABC):
    """Interface every parser implements."""

    name: str = "base"

    @abstractmethod
    def can_parse(self, path: Path) -> bool: ...

    @abstractmethod
    def parse(self, path: Path) -> ParsedDocument: ...


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------
def clean_text(text: str) -> str:
    """Normalise whitespace without destroying structure.

    Deliberately conservative: it removes noise that hurts retrieval (repeated
    blank lines, page furniture, non-breaking spaces) and leaves everything
    that carries meaning (indentation in lists, table pipes, clause numbers).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("​", "")
    # Smart quotes and dashes -> ASCII, so a query typed with a plain
    # apostrophe still matches the document text in BM25.
    for fancy, plain in (("‘", "'"), ("’", "'"), ("“", '"'),
                         ("”", '"'), ("–", "-"), ("—", "-")):
        text = text.replace(fancy, plain)
    text = re.sub(r"[ \t]+\n", "\n", text)      # trailing spaces
    text = re.sub(r"\n{3,}", "\n\n", text)      # collapse blank runs
    return text.strip()


def strip_headers_footers(pages: list[str]) -> list[str]:
    """Remove lines that repeat on most pages.

    A page header repeated fifty times becomes the most frequent text in the
    corpus and pollutes both BM25 and the embeddings. Only lines appearing on
    more than 60 percent of pages are removed, so genuine repeated content in
    a short document survives.
    """
    if len(pages) < 3:
        return pages

    first_lines: dict[str, int] = {}
    last_lines: dict[str, int] = {}
    for page in pages:
        lines = [line.strip() for line in page.split("\n") if line.strip()]
        if not lines:
            continue
        first_lines[lines[0]] = first_lines.get(lines[0], 0) + 1
        last_lines[lines[-1]] = last_lines.get(lines[-1], 0) + 1

    threshold = len(pages) * 0.6
    repeated = {line for line, count in {**first_lines, **last_lines}.items()
                if count >= threshold and len(line) < 120}

    if not repeated:
        return pages

    log.debug("Removing %d repeated header/footer lines", len(repeated))
    cleaned = []
    for page in pages:
        kept = [line for line in page.split("\n") if line.strip() not in repeated]
        cleaned.append("\n".join(kept))
    return cleaned


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------
def extract_sections(text: str) -> list[Section]:
    """Split Markdown into sections, keeping the heading hierarchy.

    The breadcrumb matters: a chunk labelled only "3.1" is useless, while
    "Refund and Credit Policy > Annual plans > 3.1" is self-describing. That
    breadcrumb is prepended to each chunk before embedding, which measurably
    improves retrieval on questions that name the section topic.
    """
    matches = list(HEADING.finditer(text))
    if not matches:
        return [Section(heading="", level=1, text=text.strip(),
                        start_char=0, end_char=len(text))]

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []   # (level, heading)

    # Any preamble before the first heading.
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(Section(heading="", level=1, text=preamble,
                                    start_char=0, end_char=matches[0].start()))

    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()

        while stack and stack[-1][0] >= level:
            stack.pop()
        breadcrumb = [h for _, h in stack]

        sections.append(Section(
            heading=heading, level=level, text=body,
            start_char=match.start(), end_char=body_end, breadcrumb=breadcrumb,
        ))
        stack.append((level, heading))

    return sections


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
class MarkdownParser(DocumentParser):
    """Markdown with YAML front matter.

    Metadata lives in the file itself rather than in a sidecar, so a document
    and its version, authority and access group cannot drift apart.
    """

    name = "markdown"

    def can_parse(self, path: Path) -> bool:
        return path.suffix.lower() in {".md", ".markdown"}

    def parse(self, path: Path) -> ParsedDocument:
        raw = path.read_text(encoding="utf-8")
        warnings: list[str] = []

        match = FRONT_MATTER.match(raw)
        if not match:
            raise ParserError(path, "missing YAML front matter (--- block at the top)")

        try:
            front: dict[str, Any] = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ParserError(path, f"invalid YAML front matter: {exc}") from exc

        body = clean_text(raw[match.end():])
        metadata = self._build_metadata(path, front, body, warnings)

        return ParsedDocument(
            metadata=metadata, text=body, sections=extract_sections(body),
            parser=self.name, warnings=warnings,
        )

    def _build_metadata(
        self, path: Path, front: dict[str, Any], body: str, warnings: list[str]
    ) -> DocumentMetadata:
        for key in ("effective_date", "expiry_date"):
            value = front.get(key)
            if isinstance(value, str):
                front[key] = datetime.strptime(value, "%Y-%m-%d").date()
            elif isinstance(value, datetime):
                front[key] = value.date()

        front.setdefault("version", "1.0")
        front["version"] = str(front["version"])
        front["source_path"] = str(path)
        front["content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

        missing = [k for k in ("doc_id", "title", "doc_type") if not front.get(k)]
        if missing:
            raise ParserError(path, f"front matter missing required keys: {missing}")

        if not front.get("effective_date"):
            warnings.append("no effective_date; defaulting to today")
            front["effective_date"] = date.today()

        return DocumentMetadata(**front)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
class PdfParser(DocumentParser):
    """PDF via pypdf.

    Metadata comes from a sidecar `<name>.meta.yaml` when present, because PDFs
    have no reliable place to carry the fields this system needs. Page numbers
    are preserved so a citation can point at a page.
    """

    name = "pdf"

    def can_parse(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def parse(self, path: Path) -> ParsedDocument:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise ParserError(path, "pypdf is not installed") from exc

        warnings: list[str] = []
        try:
            reader = PdfReader(str(path))
        except Exception as exc:
            raise ParserError(path, f"could not open PDF: {exc}") from exc

        if getattr(reader, "is_encrypted", False):
            raise ParserError(path, "PDF is encrypted")

        pages: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # a single bad page must not lose the document
                warnings.append(f"page {number} failed to extract: {exc}")
                pages.append("")

        empty = sum(1 for p in pages if not p.strip())
        if empty == len(pages):
            warnings.append(
                "no text extracted from any page; this is likely a scanned PDF "
                "that needs OCR (see ingestion.ocr)"
            )
        elif empty:
            warnings.append(f"{empty} of {len(pages)} pages produced no text")

        pages = strip_headers_footers(pages)
        text = clean_text("\n\n".join(pages))
        metadata = self._sidecar_metadata(path, text, warnings)

        return ParsedDocument(
            metadata=metadata, text=text, sections=extract_sections(text),
            page_count=len(pages), parser=self.name, warnings=warnings,
        )

    def _sidecar_metadata(
        self, path: Path, text: str, warnings: list[str]
    ) -> DocumentMetadata:
        sidecar = path.with_suffix(".meta.yaml")
        front: dict[str, Any] = {}
        if sidecar.exists():
            front = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
        else:
            warnings.append(f"no sidecar metadata at {sidecar.name}; using defaults")

        for key in ("effective_date", "expiry_date"):
            if isinstance(front.get(key), str):
                front[key] = datetime.strptime(front[key], "%Y-%m-%d").date()

        front.setdefault("doc_id", _fallback_doc_id(path))
        front.setdefault("title", path.stem.replace("-", " ").title())
        front.setdefault("doc_type", "product_catalog")
        front.setdefault("effective_date", date.today())
        front["version"] = str(front.get("version", "1.0"))
        front["source_path"] = str(path)
        front["content_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return DocumentMetadata(**front)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
class DocxParser(DocumentParser):
    """DOCX via python-docx.

    Word heading styles map onto Markdown heading levels, and tables are
    rendered as Markdown pipe tables so the header row stays attached to its
    data rows through chunking.
    """

    name = "docx"

    def can_parse(self, path: Path) -> bool:
        return path.suffix.lower() == ".docx"

    def parse(self, path: Path) -> ParsedDocument:
        try:
            import docx
        except ImportError as exc:  # pragma: no cover
            raise ParserError(path, "python-docx is not installed") from exc

        warnings: list[str] = []
        try:
            document = docx.Document(str(path))
        except Exception as exc:
            raise ParserError(path, f"could not open DOCX: {exc}") from exc

        parts: list[str] = []
        for paragraph in document.paragraphs:
            content = paragraph.text.strip()
            if not content:
                continue
            style = (paragraph.style.name or "").lower()
            if style.startswith("heading"):
                try:
                    level = int(style.replace("heading", "").strip() or 1)
                except ValueError:
                    level = 1
                parts.append(f"{'#' * min(level, 6)} {content}")
            elif style.startswith("title"):
                parts.append(f"# {content}")
            else:
                parts.append(content)

        for index, table in enumerate(document.tables):
            rendered = self._render_table(table)
            if rendered:
                parts.append(rendered)
            else:
                warnings.append(f"table {index + 1} was empty")

        text = clean_text("\n\n".join(parts))
        if not text:
            warnings.append("document produced no text")

        metadata = self._sidecar_metadata(path, text, warnings)
        return ParsedDocument(
            metadata=metadata, text=text, sections=extract_sections(text),
            parser=self.name, warnings=warnings,
        )

    @staticmethod
    def _render_table(table: Any) -> str:
        rows = [[cell.text.strip().replace("|", "\\|") for cell in row.cells]
                for row in table.rows]
        rows = [r for r in rows if any(r)]
        if not rows:
            return ""
        header, *body = rows
        lines = ["| " + " | ".join(header) + " |",
                 "|" + "|".join("---" for _ in header) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in body]
        return "\n".join(lines)

    def _sidecar_metadata(
        self, path: Path, text: str, warnings: list[str]
    ) -> DocumentMetadata:
        return PdfParser()._sidecar_metadata(path, text, warnings)


def _fallback_doc_id(path: Path) -> str:
    """Deterministic id for a file with no declared doc_id."""
    digest = hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:3]
    return f"DOC-GEN-{int(digest, 16) % 1000:03d}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
DEFAULT_PARSERS: list[DocumentParser] = [MarkdownParser(), PdfParser(), DocxParser()]


class ParserRegistry:
    """Chooses a parser by file type."""

    def __init__(self, parsers: list[DocumentParser] | None = None) -> None:
        self.parsers = parsers if parsers is not None else DEFAULT_PARSERS

    def supported_suffixes(self) -> set[str]:
        return {".md", ".markdown", ".pdf", ".docx"}

    def parse(self, path: Path) -> ParsedDocument:
        for parser in self.parsers:
            if parser.can_parse(path):
                return parser.parse(path)
        raise ParserError(
            path,
            f"unsupported file type {path.suffix!r}. "
            f"Supported: {', '.join(sorted(self.supported_suffixes()))}",
        )


__all__ = [
    "DocumentParser", "DocxParser", "MarkdownParser", "ParserError",
    "ParserRegistry", "PdfParser", "clean_text", "extract_sections",
    "strip_headers_footers",
]
