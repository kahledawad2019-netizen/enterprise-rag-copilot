"""
Qdrant vector store.

Runs embedded (in-process, local path) by default because the reference machine
has no Docker; `QDRANT_MODE=server` switches to a real service with no code
change. See ADR-001.

The payload filters implemented here are the mechanism behind three
requirements that are otherwise hand-waved:

* **Tenant isolation** — a chunk tagged `NWC-EU` is unreachable for a user in
  `NWC-NA`, enforced at the search call rather than by filtering afterwards.
  Filtering after retrieval still lets the wrong chunks occupy the top-k slots,
  so the user silently gets worse results than they should.
* **Access groups** — a `finance` document never reaches a `guest` user.
* **Version and status** — superseded policies are excluded by default, which
  is what stops the 2023 refund policy outranking the current one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..config import Settings, get_settings
from ..models.documents import Chunk, RetrievalMethod, ScoredChunk

if TYPE_CHECKING:  # pragma: no cover
    from qdrant_client import QdrantClient

log = logging.getLogger(__name__)


class VectorStoreError(RuntimeError):
    """Qdrant could not be reached or used, with a recovery hint."""


class RetrievalFilter:
    """Declarative filter, translated into a Qdrant payload filter.

    Kept as our own type rather than exposing Qdrant's model, so the rest of the
    system does not depend on the vector database's API.
    """

    def __init__(
        self,
        *,
        tenant: str | None = None,
        access_groups: list[str] | None = None,
        doc_types: list[str] | None = None,
        doc_ids: list[str] | None = None,
        current_only: bool = True,
        exclude_doc_types: list[str] | None = None,
    ) -> None:
        self.tenant = tenant
        self.access_groups = access_groups
        self.doc_types = doc_types
        self.doc_ids = doc_ids
        self.current_only = current_only
        self.exclude_doc_types = exclude_doc_types

    def to_qdrant(self) -> Any | None:
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        must: list[Any] = []
        must_not: list[Any] = []

        if self.tenant:
            # "all" documents are visible to every tenant; tenant-specific ones
            # only to their own tenant.
            must.append(
                Filter(should=[
                    FieldCondition(key="tenant", match=MatchValue(value="all")),
                    FieldCondition(key="tenant", match=MatchValue(value=self.tenant)),
                ])
            )
        if self.access_groups:
            must.append(FieldCondition(key="access_group", match=MatchAny(any=self.access_groups)))
        if self.doc_types:
            must.append(FieldCondition(key="doc_type", match=MatchAny(any=self.doc_types)))
        if self.doc_ids:
            must.append(FieldCondition(key="doc_id", match=MatchAny(any=self.doc_ids)))
        if self.current_only:
            must.append(FieldCondition(key="status", match=MatchValue(value="current")))
        if self.exclude_doc_types:
            must_not.append(
                FieldCondition(key="doc_type", match=MatchAny(any=self.exclude_doc_types))
            )

        if not must and not must_not:
            return None
        return Filter(must=must or None, must_not=must_not or None)

    def describe(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "access_groups": self.access_groups,
            "doc_types": self.doc_types,
            "doc_ids": self.doc_ids,
            "current_only": self.current_only,
            "exclude_doc_types": self.exclude_doc_types,
        }


class QdrantVectorStore:
    def __init__(self, settings: Settings | None = None, *, dimension: int | None = None) -> None:
        self.settings = settings or get_settings()
        self.collection = self.settings.vector_store.collection
        self._dimension = dimension
        self._client: QdrantClient | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _connect(self) -> QdrantClient:
        from qdrant_client import QdrantClient

        config = self.settings.vector_store
        try:
            if config.mode == "embedded":
                config.path.mkdir(parents=True, exist_ok=True)
                return QdrantClient(path=str(config.path))
            return QdrantClient(
                url=config.url,
                api_key=config.api_key.get_secret_value() if config.api_key else None,
                timeout=30,
            )
        except Exception as exc:
            hint = (
                "Embedded Qdrant is single-process: close any other process "
                "holding that path (a running Streamlit app, or a notebook), "
                "or set QDRANT_MODE=server."
                if config.mode == "embedded"
                else f"Is Qdrant running at {config.url}?"
            )
            raise VectorStoreError(
                f"Cannot open Qdrant in {config.mode} mode: "
                f"{type(exc).__name__}: {exc}. {hint}"
            ) from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> QdrantVectorStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- collection --------------------------------------------------------
    def ensure_collection(self, dimension: int, *, recreate: bool = False) -> None:
        from qdrant_client.models import Distance, VectorParams

        self._dimension = dimension
        exists = self.client.collection_exists(self.collection)

        if exists and recreate:
            log.info("Recreating collection %s", self.collection)
            self.client.delete_collection(self.collection)
            exists = False

        if exists:
            info = self.client.get_collection(self.collection)
            existing_dim = info.config.params.vectors.size
            if existing_dim != dimension:
                raise VectorStoreError(
                    f"Collection {self.collection!r} was built with dimension "
                    f"{existing_dim}, but the configured embedding model produces "
                    f"{dimension}. Rebuild the index: "
                    f"python scripts\\build_index.py --rebuild"
                )
            return

        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
        )
        self._create_payload_indexes()
        log.info("Created collection %s (dim=%d, cosine)", self.collection, dimension)

    def _create_payload_indexes(self) -> None:
        """Index the fields used in filters.

        Without these Qdrant scans payloads, which is fine at 130 chunks and
        painful at 130,000. Creating them now keeps the filter path honest as
        the corpus grows.
        """
        from qdrant_client.models import PayloadSchemaType

        for field in ("doc_id", "doc_type", "tenant", "access_group", "status", "version"):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:  # already exists, or unsupported in embedded mode
                log.debug("Payload index for %s not created: %s", field, exc)

    # -- writes ------------------------------------------------------------
    def upsert_chunks(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """Insert or replace chunks. Deterministic ids make this idempotent."""
        from qdrant_client.models import PointStruct

        if len(chunks) != len(vectors):
            raise VectorStoreError(
                f"{len(chunks)} chunks but {len(vectors)} vectors - refusing to upsert"
            )
        if not chunks:
            return 0

        points = [
            PointStruct(id=_point_id(chunk.chunk_id), vector=vector, payload=chunk.payload())
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        for start in range(0, len(points), 128):
            self.client.upsert(collection_name=self.collection, points=points[start:start + 128])
        return len(points)

    def delete_document(self, doc_id: str) -> None:
        """Remove every chunk of a document.

        Used when a document is deleted or re-versioned. Without this, a stale
        chunk keeps being retrieved and cited long after the document is gone,
        which is the classic "the bot quotes a policy we withdrew" failure.
        """
        from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

        self.client.delete(
            collection_name=self.collection,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))])
            ),
        )
        log.info("Deleted all chunks for %s", doc_id)

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        from qdrant_client.models import PointIdsList

        if not chunk_ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=PointIdsList(points=[_point_id(c) for c in chunk_ids]),
        )

    # -- reads -------------------------------------------------------------
    def search(
        self,
        vector: list[float],
        *,
        limit: int = 20,
        filters: RetrievalFilter | None = None,
    ) -> list[ScoredChunk]:
        try:
            response = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                limit=limit,
                query_filter=filters.to_qdrant() if filters else None,
                with_payload=True,
            )
        except Exception as exc:
            raise VectorStoreError(
                f"Qdrant search failed: {type(exc).__name__}: {exc}"
            ) from exc

        results: list[ScoredChunk] = []
        for rank, point in enumerate(response.points, start=1):
            chunk = _chunk_from_payload(point.payload or {})
            results.append(ScoredChunk(
                chunk=chunk, score=float(point.score), method=RetrievalMethod.DENSE,
                dense_score=float(point.score), dense_rank=rank,
            ))
        return results

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """Fetch one chunk by id, used for parent expansion."""
        records = self.client.retrieve(
            collection_name=self.collection, ids=[_point_id(chunk_id)], with_payload=True
        )
        if not records:
            return None
        return _chunk_from_payload(records[0].payload or {})

    def all_chunks(self) -> list[Chunk]:
        """Every chunk in the collection. Used to build the BM25 index."""
        chunks: list[Chunk] = []
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection, limit=256,
                offset=offset, with_payload=True, with_vectors=False,
            )
            chunks.extend(_chunk_from_payload(p.payload or {}) for p in points)
            if offset is None:
                break
        return chunks

    def count(self) -> int:
        if not self.client.collection_exists(self.collection):
            return 0
        return self.client.count(self.collection, exact=True).count

    def document_hashes(self) -> dict[str, str]:
        """doc_id -> content_hash for every indexed document.

        This is what makes incremental indexing possible: a document whose hash
        is unchanged is skipped entirely.
        """
        hashes: dict[str, str] = {}
        for chunk in self.all_chunks():
            # doc_content_hash, not content_hash: the latter hashes the chunk's
            # own text and could never match a document-level hash, which
            # silently defeated incremental indexing.
            if chunk.doc_content_hash:
                hashes.setdefault(chunk.doc_id, chunk.doc_content_hash)
        return hashes


def _point_id(chunk_id: str) -> int:
    """Qdrant point ids must be int or UUID; chunk ids are hex, so convert.

    The chunk id is a 24-character SHA-1 prefix, giving 96 bits. Truncating to
    63 bits keeps collisions vanishingly unlikely at corpus scale while fitting
    a signed 64-bit integer.
    """
    return int(chunk_id[:16], 16) & 0x7FFFFFFFFFFFFFFF


def _chunk_from_payload(payload: dict[str, Any]) -> Chunk:
    data = dict(payload)
    data.setdefault("chunk_id", "")
    data.setdefault("doc_id", "")
    data.setdefault("text", "")
    known = set(Chunk.model_fields)
    return Chunk(**{k: v for k, v in data.items() if k in known})


__all__ = ["QdrantVectorStore", "RetrievalFilter", "VectorStoreError"]
