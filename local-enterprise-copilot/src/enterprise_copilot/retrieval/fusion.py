"""
Rank fusion and diversity selection.

## Why Reciprocal Rank Fusion rather than score blending

The obvious way to combine dense and sparse results is a weighted sum of their
scores. It does not work, because the two scores are not comparable: cosine
similarity is bounded in [-1, 1] and clusters tightly around 0.5-0.9, while
BM25 is unbounded and depends on corpus statistics. Normalising them (min-max
over the returned window) is unstable — the normaliser changes with every
query, so a document's contribution depends on what else happened to be
retrieved.

RRF ignores scores and uses **ranks**:

    score(d) = sum over retrievers of  1 / (k + rank(d))

This is scale-free, needs no tuning per retriever, and is robust to one
retriever being badly calibrated. `k` (default 60, from the original paper)
damps the influence of the very top ranks so a single retriever cannot
dominate the fused list.

The cost is that RRF discards score magnitude: a document ranked first with an
overwhelming margin fuses the same as one ranked first by a hair. In practice
the reranker recovers that information, which is why the pipeline reranks after
fusing rather than instead of fusing.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict

from ..models.documents import RetrievalMethod, ScoredChunk

log = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    result_lists: list[list[ScoredChunk]],
    *,
    k: int = 60,
    weights: list[float] | None = None,
    limit: int | None = None,
) -> list[ScoredChunk]:
    """Fuse ranked lists, preserving each retriever's score for the debugger.

    Args:
        result_lists: one ranked list per retriever.
        k: RRF damping constant.
        weights: optional per-retriever weight; defaults to equal.
        limit: truncate the fused list.
    """
    if not result_lists:
        return []
    live = [lst for lst in result_lists if lst]
    if not live:
        return []
    if len(live) == 1:
        fused_single = []
        for item in live[0][:limit] if limit else live[0]:
            copy = item.model_copy()
            copy.fused_score = item.score
            copy.method = RetrievalMethod.HYBRID
            fused_single.append(copy)
        return fused_single

    if weights is None:
        weights = [1.0] * len(result_lists)
    if len(weights) != len(result_lists):
        raise ValueError(f"{len(weights)} weights for {len(result_lists)} result lists")

    fused_scores: dict[str, float] = defaultdict(float)
    best: dict[str, ScoredChunk] = {}

    for results, weight in zip(result_lists, weights, strict=True):
        for rank, scored in enumerate(results, start=1):
            chunk_id = scored.chunk.chunk_id
            fused_scores[chunk_id] += weight / (k + rank)

            # Merge provenance: a chunk found by both retrievers keeps both
            # scores and both ranks, which is what the debugger displays.
            if chunk_id not in best:
                best[chunk_id] = scored.model_copy()
            else:
                existing = best[chunk_id]
                if scored.dense_score is not None:
                    existing.dense_score = scored.dense_score
                    existing.dense_rank = scored.dense_rank
                if scored.sparse_score is not None:
                    existing.sparse_score = scored.sparse_score
                    existing.sparse_rank = scored.sparse_rank

    ordered = sorted(fused_scores.items(), key=lambda pair: pair[1], reverse=True)
    if limit:
        ordered = ordered[:limit]

    fused: list[ScoredChunk] = []
    for chunk_id, score in ordered:
        item = best[chunk_id]
        item.fused_score = score
        item.score = score
        item.method = RetrievalMethod.HYBRID
        fused.append(item)
    return fused


def deduplicate(results: list[ScoredChunk]) -> list[ScoredChunk]:
    """Drop repeated chunk ids and near-identical text.

    Near-duplicates arise legitimately: a parent chunk and its child overlap,
    and the chunker's overlap window repeats a paragraph. Two copies of the
    same sentence in the evidence wastes context and makes the model more
    confident than the evidence warrants.

    Hash the full normalized text so passages sharing a long introduction
    retain their distinct conclusions.
    """
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    unique: list[ScoredChunk] = []

    for item in results:
        chunk_id = item.chunk.chunk_id
        if chunk_id in seen_ids:
            continue
        normalized = " ".join(item.chunk.text.lower().split())
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if fingerprint in seen_text:
            continue
        seen_ids.add(chunk_id)
        seen_text.add(fingerprint)
        unique.append(item)

    if len(unique) < len(results):
        log.debug("Deduplicated %d -> %d results", len(results), len(unique))
    return unique


def maximal_marginal_relevance(
    results: list[ScoredChunk],
    *,
    limit: int,
    lambda_param: float = 0.7,
) -> list[ScoredChunk]:
    """Trade relevance against diversity when selecting the final evidence.

    Pure top-k often returns five chunks from the same section, all saying the
    same thing. For a question that needs two different documents — a policy
    and a postmortem, say — that is a failure even though every chunk is
    individually relevant.

    MMR picks the next chunk by

        lambda * relevance  -  (1 - lambda) * max similarity to those chosen

    Similarity here is token Jaccard rather than cosine over vectors: the
    vectors are not carried back from Qdrant, and Jaccard is sufficient to
    detect the near-duplicate case this is guarding against.

    lambda = 1.0 reduces to plain top-k; lower values buy diversity.

    Shift negative scores before scaling so relevance stays non-negative and
    preserves score order. Equal scores carry equal relevance.
    """
    if not results:
        return []
    if limit >= len(results) or lambda_param >= 1.0:
        return results[:limit]

    from .sparse import tokenize

    token_sets = {r.chunk.chunk_id: set(tokenize(r.chunk.text)) for r in results}
    selected: list[ScoredChunk] = [results[0]]
    remaining = list(results[1:])

    scale = max(abs(r.score) for r in results) or 1.0
    offset = min(0.0, min(r.score / scale for r in results))
    max_score = max(r.score / scale for r in results) - offset

    while remaining and len(selected) < limit:
        best_item, best_value = None, float("-inf")
        for candidate in remaining:
            relevance = (candidate.score / scale - offset) / max_score if max_score else 1.0
            candidate_tokens = token_sets[candidate.chunk.chunk_id]
            similarity = max(
                (_jaccard(candidate_tokens, token_sets[s.chunk.chunk_id]) for s in selected),
                default=0.0,
            )
            value = lambda_param * relevance - (1 - lambda_param) * similarity
            if value > best_value:
                best_item, best_value = candidate, value

        if best_item is None:
            break
        selected.append(best_item)
        remaining.remove(best_item)

    return selected


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def apply_authority_preference(results: list[ScoredChunk]) -> list[ScoredChunk]:
    """Nudge binding documents above advisory ones at comparable relevance.

    The corpus contains a genuine authority conflict: the Pricing Policy caps
    discounts at 25 percent (authority: policy) while the Sales guidance
    mentions 30 percent for strategic accounts (authority: guidance). Retrieval
    alone has no reason to prefer either.

    The adjustment is deliberately small (up to 5 percent). It breaks ties in
    favour of binding policy; it must never let an irrelevant policy outrank a
    highly relevant guidance document, because that would be a different and
    worse failure.

    Add a fraction of the score magnitude so authority improves negative
    scores too, rather than multiplying them into a larger penalty.
    """
    from ..models.documents import AuthorityLevel

    if not results:
        return results

    adjusted: list[ScoredChunk] = []
    for item in results:
        copy = item.model_copy()
        try:
            rank = AuthorityLevel(item.chunk.authority).rank
        except ValueError:
            rank = 2
        copy.score = item.score + abs(item.score) * 0.0125 * (rank - 1)
        adjusted.append(copy)

    adjusted.sort(key=lambda r: r.score, reverse=True)
    return adjusted


__all__ = [
    "apply_authority_preference",
    "deduplicate",
    "maximal_marginal_relevance",
    "reciprocal_rank_fusion",
]
