"""
The ingestion pipeline: files in, searchable index out.

    discover -> parse -> clean -> chunk -> embed -> upsert -> manifest

Two behaviours here are what separate a demo from something maintainable:

**Incremental indexing.** Each document's content hash is stored. On re-index,
unchanged documents are skipped entirely. Re-running ingestion after editing
one file should cost one document's worth of embedding, not the whole corpus.

**Stale chunk removal.** When a document is edited so that it produces fewer
chunks, or is deleted from disk, the leftover chunks must be removed. Otherwise
the index keeps serving text that no longer exists in any document — and the
system will cite it, confidently, forever.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings, get_settings
from ..models.documents import Chunk, IndexManifest, ParsedDocument
from ..retrieval.embedder import OllamaEmbedder
from ..retrieval.sparse import BM25Index
from ..retrieval.vector_store import QdrantVectorStore
from .chunking import ChunkingConfig, StructureAwareChunker
from .parsers import ParserError, ParserRegistry

log = logging.getLogger(__name__)


@dataclass
class IngestionReport:
    """What happened during a run. Printed by the CLI and asserted in tests."""

    discovered: int = 0
    parsed: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    chunks_created: int = 0
    chunks_deleted: int = 0
    documents_deleted: int = 0
    duplicates_detected: int = 0
    embedding_seconds: float = 0.0
    total_seconds: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.parsed} parsed, {self.skipped_unchanged} unchanged, "
            f"{self.failed} failed, {self.chunks_created} chunks written, "
            f"{self.chunks_deleted} stale chunks removed"
        )


def sparse_index_path(settings: Settings) -> Path:
    """Where the BM25 cache for the configured index version lives.

    A function rather than only a pipeline property: constructing a pipeline
    builds an embedder, and the retriever needs this path at startup and after
    every upload. On the cloud image that meant loading a second ONNX model
    just to compute a file name.
    """
    return settings.manifests_dir / f"bm25_{settings.vector_store.index_version}.pkl"


class IngestionPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embedder: OllamaEmbedder | None = None,
        store: QdrantVectorStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = ParserRegistry()
        self.chunker = StructureAwareChunker(ChunkingConfig.from_settings(self.settings))
        self.embedder = embedder or OllamaEmbedder(self.settings)
        self.store = store or QdrantVectorStore(self.settings)

    # -- discovery ---------------------------------------------------------
    def discover(self, root: Path | None = None) -> list[Path]:
        root = root or self.settings.documents_dir
        if not root.exists():
            raise FileNotFoundError(f"Document directory not found: {root}")
        suffixes = self.registry.supported_suffixes()
        return sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in suffixes
            and not path.name.endswith(".meta.yaml")
        )

    # -- parse -------------------------------------------------------------
    def parse_all(self, paths: list[Path], report: IngestionReport) -> list[ParsedDocument]:
        documents: list[ParsedDocument] = []
        seen_hashes: dict[str, str] = {}
        seen_ids: set[str] = set()

        for path in paths:
            try:
                document = self.registry.parse(path)
            except ParserError as exc:
                report.failed += 1
                report.errors.append((path.name, exc.reason))
                log.error("Failed to parse %s: %s", path.name, exc.reason)
                continue
            except Exception as exc:
                report.failed += 1
                report.errors.append((path.name, f"{type(exc).__name__}: {exc}"))
                log.exception("Unexpected error parsing %s", path.name)
                continue

            doc_id = document.metadata.doc_id
            if doc_id in seen_ids:
                report.failed += 1
                report.errors.append((path.name, f"duplicate doc_id {doc_id}"))
                log.error("Duplicate doc_id %s in %s - skipping", doc_id, path.name)
                continue
            seen_ids.add(doc_id)

            # Exact-content duplicates: the same policy saved under two names.
            content_hash = document.metadata.content_hash
            if content_hash in seen_hashes:
                report.duplicates_detected += 1
                report.warnings.append(
                    (path.name, f"identical content to {seen_hashes[content_hash]}")
                )
                log.warning("%s duplicates %s", path.name, seen_hashes[content_hash])
            else:
                seen_hashes[content_hash] = path.name

            for warning in document.warnings:
                report.warnings.append((path.name, warning))

            documents.append(document)
            report.parsed += 1

        return documents

    # -- index -------------------------------------------------------------
    def build_index(
        self,
        *,
        root: Path | None = None,
        rebuild: bool = False,
        dry_run: bool = False,
    ) -> tuple[IngestionReport, IndexManifest]:
        started = time.perf_counter()
        report = IngestionReport()

        paths = self.discover(root)
        report.discovered = len(paths)
        log.info("Discovered %d document files", len(paths))

        documents = self.parse_all(paths, report)

        dimension = self.embedder.dimension
        if not dry_run:
            self.store.ensure_collection(dimension, recreate=rebuild)

        existing_hashes = {} if (rebuild or dry_run) else self.store.document_hashes()

        # Documents that disappeared from disk must lose their chunks.
        on_disk = {d.metadata.doc_id for d in documents}
        if not dry_run and not rebuild:
            for doc_id in set(existing_hashes) - on_disk:
                self.store.delete_document(doc_id)
                report.documents_deleted += 1
                log.info("Removed deleted document %s from the index", doc_id)

        all_chunks: list[Chunk] = []
        for document in documents:
            doc_id = document.metadata.doc_id
            content_hash = document.metadata.content_hash

            if not rebuild and existing_hashes.get(doc_id) == content_hash:
                report.skipped_unchanged += 1
                log.debug("Unchanged, skipping: %s", doc_id)
                continue

            chunks = self.chunker.chunk_document(
                document, index_version=self.settings.vector_store.index_version
            )
            if not chunks:
                report.warnings.append((doc_id, "produced no chunks"))
                continue

            # Replace rather than merge: an edited document may yield fewer
            # chunks, and the surplus would otherwise linger and be cited.
            if not dry_run and doc_id in existing_hashes:
                self.store.delete_document(doc_id)
                report.chunks_deleted += 1

            all_chunks.extend(chunks)

        if all_chunks and not dry_run:
            embed_started = time.perf_counter()
            vectors = self.embedder.embed_documents([c.text for c in all_chunks])
            report.embedding_seconds = time.perf_counter() - embed_started
            report.chunks_created = self.store.upsert_chunks(all_chunks, vectors)
            log.info("Upserted %d chunks in %.1fs", report.chunks_created, report.embedding_seconds)
        elif all_chunks:
            report.chunks_created = len(all_chunks)

        manifest = self._build_manifest(documents, dimension, dry_run)
        if not dry_run:
            self.write_manifest(manifest)
            self._rebuild_sparse_index()

        report.total_seconds = time.perf_counter() - started
        return report, manifest

    def _build_manifest(
        self, documents: list[ParsedDocument], dimension: int, dry_run: bool
    ) -> IndexManifest:
        return IndexManifest(
            index_version=self.settings.vector_store.index_version,
            collection=self.settings.vector_store.collection,
            embedding_model=self.settings.embedding_model,
            embedding_dimension=dimension,
            chunk_target_tokens=self.settings.retrieval.chunk_target_tokens,
            chunk_overlap_tokens=self.settings.retrieval.chunk_overlap_tokens,
            built_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
            document_count=len(documents),
            chunk_count=0 if dry_run else self.store.count(),
            documents={d.metadata.doc_id: d.metadata.content_hash for d in documents},
        )

    def _rebuild_sparse_index(self) -> None:
        """BM25 is derived from the vector store, so it is rebuilt after every
        index change. Keeping them in sync automatically removes a whole class
        of bug where sparse search returns documents dense search cannot."""
        chunks = self.store.all_chunks()
        index = BM25Index()
        index.build(chunks)
        index.save(self.sparse_index_path)

    # -- manifest ----------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.settings.manifests_dir / (
            f"index_{self.settings.vector_store.index_version}.json"
        )

    @property
    def sparse_index_path(self) -> Path:
        return sparse_index_path(self.settings)

    def write_manifest(self, manifest: IndexManifest) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        log.info("Manifest written to %s", self.manifest_path)

    def read_manifest(self) -> IndexManifest | None:
        if not self.manifest_path.exists():
            return None
        return IndexManifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))

    # -- validation --------------------------------------------------------
    def validate_index(self) -> tuple[bool, list[str]]:
        """Check the index is usable before any query runs.

        The dangerous failure is a configuration change that does not raise:
        a different embedding model with the same dimension returns confident
        nonsense. Comparing against the manifest catches it.
        """
        problems: list[str] = []

        stored = self.read_manifest()
        if stored is None:
            return False, ["no manifest found - run: python scripts/build_index.py"]

        current = IndexManifest(
            index_version=self.settings.vector_store.index_version,
            collection=self.settings.vector_store.collection,
            embedding_model=self.settings.embedding_model,
            embedding_dimension=self.embedder.dimension,
            chunk_target_tokens=self.settings.retrieval.chunk_target_tokens,
            chunk_overlap_tokens=self.settings.retrieval.chunk_overlap_tokens,
            built_at_utc="",
        )
        compatible, reason = current.is_compatible_with(stored)
        if not compatible:
            problems.append(reason)

        count = self.store.count()
        if count == 0:
            problems.append("index is empty - run: python scripts/build_index.py")
        elif count != stored.chunk_count:
            problems.append(
                f"index holds {count} chunks but the manifest records "
                f"{stored.chunk_count}; the index changed outside the pipeline"
            )

        if not self.sparse_index_path.exists():
            problems.append("BM25 index missing - re-run the ingestion pipeline")

        return not problems, problems


__all__ = ["IngestionPipeline", "IngestionReport"]
