"""
The retrieval pipeline.

    query
      -> dense search (Qdrant)      \\
      -> sparse search (BM25)        > fuse (RRF) -> dedupe -> rerank -> MMR -> evidence
      -> permission & version filter/

Four strategies are exposed rather than one, because the specification requires
them to be *compared* rather than asserted:

    DENSE     embedding similarity only
    SPARSE    BM25 only
    HYBRID    both, fused with RRF
    RERANKED  hybrid followed by a cross-encoder

Every stage records what it did into a `RetrievalTrace`, which is what the
Retrieval Debugger page and the evaluation harness both read. Retrieval that
cannot explain itself cannot be tuned.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..config import Settings, get_settings
from ..models.documents import RetrievalMethod, ScoredChunk
from ..reranking.policy import RerankDecision
from ..reranking.policy import decide as decide_rerank
from ..reranking.reranker import Reranker, build_reranker
from .embedder import OllamaEmbedder
from .fusion import (
    apply_authority_preference,
    deduplicate,
    maximal_marginal_relevance,
    reciprocal_rank_fusion,
)
from .sparse import BM25Index
from .vector_store import QdrantVectorStore, RetrievalFilter

log = logging.getLogger(__name__)


class RetrievalStrategy(StrEnum):
    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"
    RERANKED = "reranked"


@dataclass
class RetrievalTrace:
    """Everything the debugger and the evaluator need to explain a result."""

    query: str
    rewritten_query: str | None = None
    strategy: str = "reranked"
    filters: dict[str, Any] = field(default_factory=dict)

    dense_results: list[ScoredChunk] = field(default_factory=list)
    sparse_results: list[ScoredChunk] = field(default_factory=list)
    fused_results: list[ScoredChunk] = field(default_factory=list)
    reranked_results: list[ScoredChunk] = field(default_factory=list)
    final_results: list[ScoredChunk] = field(default_factory=list)

    stage_seconds: dict[str, float] = field(default_factory=dict)
    reranker: dict[str, Any] = field(default_factory=dict)
    rerank_decision: str = ""
    parent_expansions: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return sum(self.stage_seconds.values())

    def summary(self) -> str:
        return (
            f"{self.strategy}: dense={len(self.dense_results)} "
            f"sparse={len(self.sparse_results)} fused={len(self.fused_results)} "
            f"final={len(self.final_results)} in {self.total_seconds * 1000:.0f}ms"
        )


@dataclass
class UserContext:
    """Who is asking. Drives tenant and access-group filtering.

    Carried explicitly rather than read from a global, so a test can construct
    a user in another tenant and prove isolation holds.
    """

    user_name: str = "guest"
    tenant: str = "all"
    access_groups: list[str] = field(default_factory=lambda: ["public"])
    is_admin: bool = False

    @classmethod
    def admin(cls) -> UserContext:
        return cls(
            user_name="admin",
            tenant="all",
            is_admin=True,
            access_groups=["public", "internal", "finance", "support", "security", "exec"],
        )


class HybridRetriever:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embedder: OllamaEmbedder | None = None,
        store: QdrantVectorStore | None = None,
        sparse: BM25Index | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or OllamaEmbedder(self.settings)
        self.store = store or QdrantVectorStore(self.settings)
        self.reranker = reranker or build_reranker(self.settings)
        self.sparse = sparse if sparse is not None else self._load_sparse()

    def _load_sparse(self) -> BM25Index:
        """Load the cached BM25 corpus, rebuilding from Qdrant if absent."""
        from ..ingestion.pipeline import sparse_index_path

        index = BM25Index()
        path = sparse_index_path(self.settings)
        if index.load(path):
            return index

        log.info("No BM25 cache at %s; rebuilding from the vector store", path)
        try:
            index.build(self.store.all_chunks())
        except Exception as exc:
            log.warning("Could not build the BM25 index: %s. Sparse search disabled.", exc)
        return index

    # -- main entry point --------------------------------------------------
    def retrieve(
        self,
        query: str,
        *,
        strategy: RetrievalStrategy | str = RetrievalStrategy.RERANKED,
        user: UserContext | None = None,
        limit: int | None = None,
        filters: RetrievalFilter | None = None,
        rewritten_query: str | None = None,
        expand_parents: bool | None = None,
    ) -> tuple[list[ScoredChunk], RetrievalTrace]:
        strategy = RetrievalStrategy(strategy)
        user = user or UserContext.admin()
        profile = self.settings.profile
        limit = limit or profile.final_evidence_chunks
        filters = filters or self.build_filters(user)

        # The rewritten query is used for retrieval; the original is kept for
        # the trace and for reranking, where the user's own wording scores best.
        search_text = rewritten_query or query

        trace = RetrievalTrace(
            query=query,
            rewritten_query=rewritten_query,
            strategy=strategy.value,
            filters=filters.describe(),
        )

        allowed_ids = (
            self._allowed_chunk_ids(filters)
            if strategy
            in (RetrievalStrategy.SPARSE, RetrievalStrategy.HYBRID, RetrievalStrategy.RERANKED)
            else None
        )

        # --- dense ---
        if strategy in (
            RetrievalStrategy.DENSE,
            RetrievalStrategy.HYBRID,
            RetrievalStrategy.RERANKED,
        ):
            started = time.perf_counter()
            vector = self.embedder.embed_query(search_text)
            trace.stage_seconds["embed_query"] = time.perf_counter() - started

            started = time.perf_counter()
            trace.dense_results = self.store.search(
                vector, limit=profile.dense_candidates, filters=filters
            )
            trace.stage_seconds["dense_search"] = time.perf_counter() - started

        # --- sparse ---
        if strategy in (
            RetrievalStrategy.SPARSE,
            RetrievalStrategy.HYBRID,
            RetrievalStrategy.RERANKED,
        ):
            started = time.perf_counter()
            trace.sparse_results = self.sparse.search(
                search_text, limit=profile.sparse_candidates, allowed_chunk_ids=allowed_ids
            )
            trace.stage_seconds["sparse_search"] = time.perf_counter() - started
            if not self.sparse.is_ready:
                trace.notes.append("BM25 index unavailable; sparse results are empty")

        # --- combine ---
        if strategy is RetrievalStrategy.DENSE:
            candidates = trace.dense_results
        elif strategy is RetrievalStrategy.SPARSE:
            candidates = trace.sparse_results
        else:
            started = time.perf_counter()
            candidates = reciprocal_rank_fusion(
                [trace.dense_results, trace.sparse_results],
                k=self.settings.retrieval.rrf_k,
            )
            trace.stage_seconds["fusion"] = time.perf_counter() - started
            trace.fused_results = candidates

        candidates = deduplicate(candidates)

        # --- rerank, but only when it can plausibly change the answer ---
        if strategy is RetrievalStrategy.RERANKED and candidates:
            decision: RerankDecision = decide_rerank(
                query,
                candidates,
                dense=trace.dense_results,
                sparse=trace.sparse_results,
                policy=self.settings.retrieval.rerank_policy,
                margin_threshold=self.settings.retrieval.rerank_margin_threshold,
            )
            trace.rerank_decision = str(decision)
            if decision.should_rerank:
                started = time.perf_counter()
                candidates = self.reranker.rerank(
                    query, candidates[: profile.reranker_top_n], limit=max(limit * 2, limit)
                )
                trace.stage_seconds["rerank"] = time.perf_counter() - started
                trace.reranked_results = candidates
            else:
                log.debug("Reranking skipped - %s", decision.reason)
        trace.reranker = self.reranker.describe()

        # --- authority preference, then diversity ---
        candidates = apply_authority_preference(candidates)

        started = time.perf_counter()
        final = maximal_marginal_relevance(
            candidates, limit=limit, lambda_param=self.settings.retrieval.mmr_lambda
        )
        trace.stage_seconds["select"] = time.perf_counter() - started

        # --- parent expansion ---
        expand = (
            self.settings.retrieval.enable_parent_expansion
            if expand_parents is None
            else expand_parents
        )
        if expand:
            started = time.perf_counter()
            final, expansions = self._expand_parents(final)
            trace.parent_expansions = expansions
            trace.stage_seconds["parent_expansion"] = time.perf_counter() - started

        trace.final_results = final
        log.debug(trace.summary())
        return final, trace

    # -- helpers -----------------------------------------------------------
    def build_filters(self, user: UserContext) -> RetrievalFilter:
        """Translate a user into a retrieval filter.

        The security-test document is excluded from ordinary retrieval: it
        contains prompt-injection payloads and exists for evaluation, not to be
        surfaced in answers. It remains reachable when a caller asks for it by
        doc_type, which is how the injection tests exercise it.
        """
        return RetrievalFilter(
            tenant=None if user.tenant == "all" else user.tenant,
            access_groups=user.access_groups,
            current_only=True,
            exclude_doc_types=["security_test"],
        )

    def _allowed_chunk_ids(self, filters: RetrievalFilter) -> set[str]:
        """Chunk ids permitted by the filter, for BM25.

        BM25 has no payload filtering of its own, so the permitted set is
        computed from the vector store and applied *before* ranking. Filtering
        after ranking would let forbidden chunks occupy top-k slots and quietly
        shrink the user's result set.

        A failed lookup permits nothing, including for admins whose version
        and document-type restrictions still apply.
        """
        try:
            chunks = self.store.all_chunks()
        except Exception as exc:
            log.warning("Could not compute the allowed chunk set: %s", exc)
            return set()

        allowed: set[str] = set()
        for chunk in chunks:
            if filters.current_only and chunk.status != "current":
                continue
            if filters.tenant and chunk.tenant not in ("all", filters.tenant):
                continue
            if filters.access_groups and chunk.access_group not in filters.access_groups:
                continue
            if filters.doc_types and chunk.doc_type not in filters.doc_types:
                continue
            if filters.exclude_doc_types and chunk.doc_type in filters.exclude_doc_types:
                continue
            allowed.add(chunk.chunk_id)
        return allowed

    def _expand_parents(self, results: list[ScoredChunk]) -> tuple[list[ScoredChunk], int]:
        """Replace a child chunk with its parent section when one exists.

        A precise chunk is best for *finding* the answer; the surrounding
        section is often better for *answering* it, because the clause above
        carries the condition the clause below depends on.
        """
        expanded: list[ScoredChunk] = []
        count = 0
        seen: set[str] = set()

        for item in results:
            parent_id = item.chunk.parent_chunk_id
            if not parent_id or parent_id in seen:
                if item.chunk.chunk_id not in seen:
                    expanded.append(item)
                    seen.add(item.chunk.chunk_id)
                continue

            parent = self.store.get_chunk(parent_id)
            if parent is None:
                expanded.append(item)
                seen.add(item.chunk.chunk_id)
                continue

            copy = item.model_copy()
            copy.chunk = parent
            copy.method = RetrievalMethod.PARENT
            expanded.append(copy)
            seen.add(parent_id)
            count += 1

        return expanded, count

    def compare_strategies(
        self, query: str, *, user: UserContext | None = None, limit: int = 8
    ) -> dict[str, tuple[list[ScoredChunk], RetrievalTrace]]:
        """Run all four strategies over one query.

        Used by the notebook and the Retrieval Debugger to show, rather than
        assert, what reranking buys on a given question.
        """
        return {
            strategy.value: self.retrieve(query, strategy=strategy, user=user, limit=limit)
            for strategy in RetrievalStrategy
        }

    def close(self) -> None:
        self.store.close()


__all__ = ["HybridRetriever", "RetrievalStrategy", "RetrievalTrace", "UserContext"]
