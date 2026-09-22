"""
Cross-encoder reranking.

## Why a second model at all

Dense retrieval embeds the query and each document *independently*, then
compares vectors. That is fast enough to search a whole corpus, but the model
never sees the query and the document together, so it cannot reason about how
they relate. It matches topic, not answer.

A cross-encoder scores the pair `(query, chunk)` in one forward pass. It is far
more accurate and far too slow to run over a corpus — so the pipeline uses
retrieval to get from 130 chunks to ~30 candidates, then the cross-encoder to
pick the best 8. Cheap model for recall, expensive model for precision.

## Graceful degradation

The reranker is optional (`pip install -e ".[rerank]"` pulls PyTorch, ~2.5 GB).
When it is unavailable, retrieval must keep working with the fused ranking
rather than failing. `NoOpReranker` is that fallback, and the system reports
which one is active instead of pretending.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from ..config import Settings, get_settings
from ..models.documents import RetrievalMethod, ScoredChunk

log = logging.getLogger(__name__)


class Reranker(ABC):
    """Interface. Two implementations: cross-encoder and no-op."""

    name: str = "base"

    @abstractmethod
    def rerank(self, query: str, results: list[ScoredChunk], *, limit: int) -> list[ScoredChunk]: ...

    @property
    def is_active(self) -> bool:
        return True

    def describe(self) -> dict[str, object]:
        return {"reranker": self.name, "active": self.is_active}


class NoOpReranker(Reranker):
    """Pass-through. Used when reranking is disabled or unavailable.

    Deliberately not silent: the reason is recorded so the UI and the
    evaluation report can state that reranking was not applied, rather than
    attributing the fused ranking to a reranker that never ran.
    """

    name = "none"

    def __init__(self, reason: str = "reranking disabled") -> None:
        self.reason = reason

    def rerank(self, query: str, results: list[ScoredChunk], *, limit: int) -> list[ScoredChunk]:
        return results[:limit]

    @property
    def is_active(self) -> bool:
        return False

    def describe(self) -> dict[str, object]:
        return {"reranker": self.name, "active": False, "reason": self.reason}


class CrossEncoderReranker(Reranker):
    """Local multilingual cross-encoder (BGE family by default).

    Runs on CPU by design: the 8 GB GPU is already holding the chat model, and
    a mid-request OOM is a worse failure than a slightly slower rerank. See
    ADR-001.
    """

    def __init__(self, model_name: str, *, device: str = "cpu") -> None:
        self.name = model_name
        self.device = device
        self._model = None
        self._load_seconds = 0.0
        self.total_pairs = 0
        self.total_seconds = 0.0

    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            started = time.perf_counter()
            log.info("Loading reranker %s on %s (first call only)", self.name, self.device)
            self._model = CrossEncoder(self.name, device=self.device, max_length=512)
            self._load_seconds = time.perf_counter() - started
            log.info("Reranker loaded in %.1fs", self._load_seconds)
        return self._model

    def rerank(self, query: str, results: list[ScoredChunk], *, limit: int) -> list[ScoredChunk]:
        if not results:
            return []

        model = self._ensure_model()
        pairs = [(query, item.chunk.text) for item in results]

        started = time.perf_counter()
        scores = model.predict(pairs, show_progress_bar=False)
        elapsed = time.perf_counter() - started

        self.total_pairs += len(pairs)
        self.total_seconds += elapsed
        log.debug("Reranked %d pairs in %.2fs", len(pairs), elapsed)

        reranked: list[ScoredChunk] = []
        for item, score in zip(results, scores, strict=True):
            copy = item.model_copy()
            copy.rerank_score = float(score)
            copy.score = float(score)
            copy.method = RetrievalMethod.RERANKED
            reranked.append(copy)

        reranked.sort(key=lambda r: r.score, reverse=True)
        return reranked[:limit]

    def describe(self) -> dict[str, object]:
        return {
            "reranker": self.name,
            "active": True,
            "device": self.device,
            "load_seconds": round(self._load_seconds, 2),
            "pairs_scored": self.total_pairs,
            "seconds_per_pair": round(
                self.total_seconds / self.total_pairs, 4) if self.total_pairs else 0.0,
        }


def build_reranker(settings: Settings | None = None) -> Reranker:
    """Return the configured reranker, degrading gracefully.

    Three ways to end up with a no-op, each reported distinctly so the cause is
    never a mystery: disabled in config, no model in the profile, or
    sentence-transformers not installed.
    """
    settings = settings or get_settings()

    if not settings.retrieval.enable_reranking:
        return NoOpReranker("RETRIEVAL_ENABLE_RERANKING=false")

    model_name = settings.profile.reranker_model
    if not model_name:
        return NoOpReranker(f"profile '{settings.profile_name.value}' defines no reranker")

    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return NoOpReranker(
            'sentence-transformers not installed - run: pip install -e ".[rerank]"'
        )

    return CrossEncoderReranker(model_name, device=settings.profile.reranker_device)


__all__ = ["CrossEncoderReranker", "NoOpReranker", "Reranker", "build_reranker"]
