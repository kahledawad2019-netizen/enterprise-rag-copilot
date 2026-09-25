"""
Validation and storage for documents uploaded through the UI.

An upload is untrusted in every way that matters here:

* **The filename** can carry path separators or reserved names. It is reduced
  to a safe slug and never used as a path component as given.
* **The type** is decided by content, not only by extension. A `.pdf` that
  does not start with `%PDF-` is refused, and a `.docx` must be a zip.
* **The size** is capped before anything is parsed.
* **The text** is data, never instructions. It reaches the model inside the
  same `<evidence>` delimiters as the curated corpus, and the prompt's
  injection rules apply to it unchanged. Nothing in a document is executed.

Uploaded documents are public within the deployment (`access_group: public`,
`tenant: all`), because the uploader has no way to assign a permission, and
guessing one would be worse than being plain about it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

import yaml

ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf", ".docx"}
UPLOAD_SUBDIR = "uploads"


class UploadRejectedError(ValueError):
    """The upload is refused; the message is safe to show to the user."""


def _slug(filename: str) -> str:
    stem = Path(filename.replace("\\", "/")).name
    stem = Path(stem).stem
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return slug[:60] or "document"


def title_for(filename: str) -> str:
    stem = Path(Path(filename.replace("\\", "/")).name).stem
    title = re.sub(r"[_\-]+", " ", stem).strip()
    return title[:120].title() or "Uploaded document"


# Decompression bounds. A .docx is a zip and a PDF has compressed streams, so a
# small upload can expand enormously while being parsed - in the API process,
# under the copilot lock. These are checked before any parsing.
MAX_DOCX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_DOCX_ENTRIES = 2000
MAX_PDF_PAGES = 300


def doc_id_for(content: bytes) -> str:
    """DOC-UPL-NNNN-NNNN: 10^8 values, and it matches the corpus doc_id pattern.

    Derived from the content, so re-uploading the same file maps to the same
    document; a four-digit id collided on two short test files.
    """
    number = int(hashlib.sha256(content).hexdigest()[:12], 16) % 100_000_000
    return f"DOC-UPL-{number // 10_000:04d}-{number % 10_000:04d}"


def validate(filename: str, content: bytes, *, max_bytes: int) -> str:
    """Return the normalised suffix, or raise UploadRejectedError."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise UploadRejectedError(
            f"Unsupported file type {suffix or '(none)'}. Upload .md, .txt, .pdf or .docx."
        )
    if not content:
        raise UploadRejectedError("The file is empty.")
    if len(content) > max_bytes:
        raise UploadRejectedError(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")

    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise UploadRejectedError("The file does not look like a PDF.")
    if suffix == ".docx" and not content.startswith(b"PK\x03\x04"):
        raise UploadRejectedError("The file does not look like a .docx document.")
    if suffix in {".md", ".markdown", ".txt"}:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadRejectedError("Text files must be UTF-8.") from exc
    return ".md" if suffix in {".markdown", ".txt"} else suffix


def _check_docx(content: bytes) -> None:
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise UploadRejectedError("The file does not look like a .docx document.") from exc
    if len(entries) > MAX_DOCX_ENTRIES or sum(e.file_size for e in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
        raise UploadRejectedError("The document expands to more than this deployment accepts.")


def _check_pdf(content: bytes) -> None:
    import io

    from pypdf import PdfReader

    try:
        pages = len(PdfReader(io.BytesIO(content)).pages)
    except Exception as exc:  # any parser failure means unreadable
        raise UploadRejectedError("The PDF could not be read.") from exc
    if pages > MAX_PDF_PAGES:
        raise UploadRejectedError(f"PDFs are limited to {MAX_PDF_PAGES} pages here.")


def existing_upload(documents_dir: Path, doc_id: str) -> Path | None:
    """The stored file for this doc_id, if one exists."""
    folder = documents_dir / UPLOAD_SUBDIR
    if not folder.exists():
        return None
    suffix = doc_id.removeprefix("DOC-UPL-")
    for path in folder.iterdir():
        if path.suffix.lower() in {".md", ".pdf", ".docx"} and path.stem.endswith(suffix):
            return path
    return None


def store(filename: str, content: bytes, documents_dir: Path, *, max_bytes: int) -> tuple[Path, str]:
    """Validate and write the upload. Returns (path, doc_id)."""
    suffix = validate(filename, content, max_bytes=max_bytes)
    doc_id = doc_id_for(content)
    target_dir = documents_dir / UPLOAD_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_slug(filename)}-{doc_id.removeprefix('DOC-UPL-')}{suffix}"

    metadata = {
        "doc_id": doc_id,
        "title": title_for(filename),
        "doc_type": "uploaded",
        "version": "1.0",
        "effective_date": date.today().isoformat(),
        "status": "current",
        "authority": "reference",
        "department": "Uploaded",
        "owner": "Uploaded via UI",
        "access_group": "public",
        "tenant": "all",
        "tags": ["uploaded"],
    }

    if suffix == ".md":
        text = content.decode("utf-8").replace("\r\n", "\n")
        if text.startswith("---"):
            # The uploader's own front matter is not trusted: it could claim a
            # restricted access group or impersonate a curated doc_id. Drop it.
            _, _, rest = text.partition("---")
            _, sep, body = rest.partition("---")
            text = body if sep else text
        if not re.search(r"^#\s", text, flags=re.MULTILINE):
            text = f"# {metadata['title']}\n\n{text}"
        front = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
        target.write_text(f"---\n{front}---\n\n{text.strip()}\n", encoding="utf-8")
    else:
        target.write_bytes(content)
        target.with_suffix(".meta.yaml").write_text(
            yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
        )
    return target, doc_id


def count_uploads(documents_dir: Path) -> int:
    folder = documents_dir / UPLOAD_SUBDIR
    if not folder.exists():
        return 0
    return sum(1 for p in folder.iterdir() if p.suffix.lower() in {".md", ".pdf", ".docx"})
