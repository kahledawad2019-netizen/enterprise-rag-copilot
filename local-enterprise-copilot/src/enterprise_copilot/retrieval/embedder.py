"""
Dense embeddings via Ollama.

Three properties matter here, and each has bitten real systems:

1. **The dimension is detected, never assumed.** Swapping the embedding model
   changes the vector width. If the index was built at 768 and queries arrive
   at 1024, Qdrant raises; but if two models happen to share a width, the
   system silently returns nonsense. Detection plus the manifest check in
   `IndexManifest.is_compatible_with` makes both cases loud.

2. **Indexing and querying use the same model and the same prefixes.** Some
   embedding models are trained asymmetrically, expecting a different prefix
   for documents and queries. Getting that wrong costs recall quietly. The
   prefix policy lives in one place, here.

3. **Failures are explicit.** An embedding call that fails must raise, not
   return a zero vector. A zero vector is a valid input to a similarity search
   and produces plausible, meaningless results.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Embedding failed. Carries an actionable recovery hint."""


@dataclass
class EmbeddingStats:
    """Counters used by the observability layer and the notebook."""

    calls: int = 0
    texts: int = 0
    total_seconds: float = 0.0
    failures: int = 0

    @property
    def texts_per_second(self) -> float:
        return self.texts / self.total_seconds if self.total_seconds else 0.0


class OllamaEmbedder:
    """Embeds text with a local Ollama model.

    `query_prefix` and `document_prefix` default to empty. Qwen3-Embedding does
    not require an asymmetric prefix for retrieval, so adding one would hurt
    rather than help. They exist because a future model may need them, and the
    policy must be changed in exactly one place when it does.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        query_prefix: str = "",
        document_prefix: str = "",
    ) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.embedding_model
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.stats = EmbeddingStats()
        self._dimension: int | None = None

        from ..llm import build_embedding_client

        self._client = build_embedding_client(self.settings)

    # -- dimension ---------------------------------------------------------
    @property
    def dimension(self) -> int:
        """Detected once per process by embedding a probe string."""
        if self._dimension is None:
            self._dimension = len(self.embed_query("dimension probe"))
            log.info("Detected embedding dimension %d for %s", self._dimension, self.model)
        return self._dimension

    # -- embedding ---------------------------------------------------------
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prefixed = [f"{self.document_prefix}{t}" for t in texts]
        return self._embed_batched(prefixed)

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batched([f"{self.query_prefix}{text}"])[0]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Used by multi-query retrieval, where one question becomes several."""
        if not texts:
            return []
        return self._embed_batched([f"{self.query_prefix}{t}" for t in texts])

    def _embed_batched(self, texts: list[str]) -> list[list[float]]:
        batch_size = self.settings.profile.embedding_batch_size
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            vectors.extend(self._embed_once(texts[start:start + batch_size]))
        return vectors

    def _embed_once(self, batch: list[str]) -> list[list[float]]:
        started = time.perf_counter()
        try:
            response = self._client.embed(model=self.model, input=batch)
        except Exception as exc:
            self.stats.failures += 1
            raise EmbeddingError(
                f"Embedding failed with model {self.model!r} at "
                f"{self.settings.ollama.host}: {type(exc).__name__}: {exc}. "
                f"Check Ollama is running and the model is pulled "
                f"(ollama pull {self.model})."
            ) from exc

        vectors = response.get("embeddings")
        if not vectors or len(vectors) != len(batch):
            self.stats.failures += 1
            raise EmbeddingError(
                f"Ollama returned {len(vectors) if vectors else 0} vectors for "
                f"{len(batch)} inputs. The model may not be an embedding model."
            )

        elapsed = time.perf_counter() - started
        self.stats.calls += 1
        self.stats.texts += len(batch)
        self.stats.total_seconds += elapsed
        return [list(v) for v in vectors]

    def describe(self) -> dict[str, object]:
        return {
            "model": self.model,
            "host": self.settings.ollama.host,
            "dimension": self._dimension,   # None until first use, deliberately
            "batch_size": self.settings.profile.embedding_batch_size,
            "calls": self.stats.calls,
            "texts": self.stats.texts,
            "texts_per_second": round(self.stats.texts_per_second, 2),
        }


__all__ = ["EmbeddingError", "EmbeddingStats", "OllamaEmbedder"]
