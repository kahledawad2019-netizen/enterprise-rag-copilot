"""
Sparse retrieval with BM25.

Dense embeddings are excellent at meaning and poor at exact strings. Ask for
"INC-2025-0042" or "SLA-ENT-P1" and a dense retriever returns chunks that are
*about* incidents, not the chunk containing that identifier. BM25 does the
opposite: it nails the literal token and misses paraphrase.

That complementarity is the entire reason hybrid retrieval works, and it is why
this module exists rather than relying on the vector store alone.

The index is rebuilt from the chunks held in the vector store and cached to
disk, so a Streamlit restart does not pay for tokenising the corpus again.
"""

from __future__ import annotations

import logging
import pickle
import re
from dataclasses import dataclass
from pathlib import Path

from ..models.documents import Chunk, RetrievalMethod, ScoredChunk

log = logging.getLogger(__name__)

# Split on non-alphanumerics but keep identifiers such as INC-2025-0042 and
# SLA-ENT-P1 intact, because those are exactly the tokens BM25 is here for.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")

# A deliberately small stop list. Aggressive stop-word removal hurts: "not"
# and "no" carry real meaning in a policy corpus ("no refund is issued").
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "and",
        "or",
        "as",
        "that",
        "this",
        "it",
        "its",
        "from",
        "which",
    }
)


def tokenize(text: str) -> list[str]:
    """Lowercase, keep identifiers whole, drop a short stop list.

    Identifiers are additionally split into parts and both forms kept, so
    "INC-2025-0042" matches a query for either the full code or just "2025".
    """
    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(text.lower()):
        token = match.group(0)
        if token in STOP_WORDS or len(token) == 1:
            continue
        tokens.append(token)
        if "-" in token or "_" in token:
            tokens.extend(p for p in re.split(r"[-_]", token) if p and p not in STOP_WORDS)
    return tokens


@dataclass
class SparseHit:
    chunk_id: str
    score: float
    rank: int


class BM25Index:
    """BM25 over the chunk corpus.

    Wraps `rank_bm25` rather than reimplementing it, but owns tokenisation,
    persistence and filtering, which is where the behaviour that matters lives.
    """

    def __init__(self) -> None:
        self._bm25 = None
        self._chunks: list[Chunk] = []
        self._chunk_ids: list[str] = []
        self._corpus_tokens: list[set[str]] = []

    # -- build -------------------------------------------------------------
    def build(self, chunks: list[Chunk]) -> None:
        from rank_bm25 import BM25Okapi

        if not chunks:
            log.warning("BM25 index built over an empty corpus")
            self._bm25, self._chunks, self._chunk_ids = None, [], []
            self._corpus_tokens = []
            return

        self._chunks = list(chunks)
        self._chunk_ids = [c.chunk_id for c in chunks]

        # Title and section path are included so a query naming the section
        # ("refund policy annual plans") matches even when the body does not
        # repeat those words.
        corpus = [tokenize(f"{c.title} {c.section_path} {c.text}") for c in chunks]
        self._bm25 = BM25Okapi(corpus)
        self._corpus_tokens = [set(tokens) for tokens in corpus]
        log.info("BM25 index built over %d chunks", len(chunks))

    @property
    def size(self) -> int:
        return len(self._chunks)

    @property
    def is_ready(self) -> bool:
        return self._bm25 is not None

    # -- search ------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        allowed_chunk_ids: set[str] | None = None,
    ) -> list[ScoredChunk]:
        """Top-k by BM25.

        `allowed_chunk_ids` applies the same permission and version filters the
        dense path applies in Qdrant. Filtering before ranking (not after)
        matters: otherwise forbidden chunks consume top-k slots and the user
        silently receives fewer usable results.

        None explicitly requests unrestricted search; an empty set denies all
        chunks and must be used when permission lookup fails.
        """
        if not self.is_ready:
            return []

        tokens = tokenize(query)
        if not tokens:
            return []

        bm25 = self._bm25
        if bm25 is None:  # defensive: `is_ready` may change if construction is refactored
            return []
        scores = bm25.get_scores(tokens)
        query_tokens = set(tokens)

        # A document qualifies if it shares at least one query token. Ranking
        # by score alone is not enough: BM25 assigns NEGATIVE idf to a term
        # that appears in more than half the corpus, so on a small corpus a
        # strict `score > 0` filter silently returns nothing even for an exact
        # identifier match. Requiring token overlap keeps the filter meaningful
        # without depending on the sign of the score.
        candidates = [
            (index, float(score))
            for index, score in enumerate(scores)
            if (self._corpus_tokens[index] & query_tokens)
            and (allowed_chunk_ids is None or self._chunk_ids[index] in allowed_chunk_ids)
        ]
        candidates.sort(key=lambda pair: pair[1], reverse=True)

        return [
            ScoredChunk(
                chunk=self._chunks[index],
                score=score,
                method=RetrievalMethod.SPARSE,
                sparse_score=score,
                sparse_rank=rank,
            )
            for rank, (index, score) in enumerate(candidates[:limit], start=1)
        ]

    # -- persistence -------------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            pickle.dump(
                {"chunks": [c.model_dump() for c in self._chunks]},
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        log.info("BM25 corpus saved to %s (%d chunks)", path, len(self._chunks))

    def load(self, path: Path) -> bool:
        """Rebuild from a saved corpus. Returns False when unavailable.

        Only the chunks are persisted, not the fitted BM25 object: pickling a
        third-party model couples the cache to that library's internals, and
        refitting 130 chunks costs milliseconds.
        """
        if not path.exists():
            return False
        try:
            with path.open("rb") as handle:
                payload = pickle.load(handle)
            self.build([Chunk(**c) for c in payload["chunks"]])
            return True
        except Exception as exc:
            log.warning("Could not load BM25 cache from %s: %s. Rebuilding.", path, exc)
            return False


__all__ = ["STOP_WORDS", "BM25Index", "SparseHit", "tokenize"]
