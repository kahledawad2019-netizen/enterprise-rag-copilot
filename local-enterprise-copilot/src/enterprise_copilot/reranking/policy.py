"""
When reranking is worth its cost.

## The measurement that motivated this

On 96 held-out questions, reranking versus hybrid:

```
delta NDCG +0.023, 95 % CI [-0.013, +0.059], p = 0.217
wins 12 / losses 6 / ties 78
median latency 2,428 ms versus 67 ms
```

The interval contains zero: the improvement is **not distinguishable from
noise** at this sample size, and it costs roughly 36x the latency. Meanwhile
**78 of 96 queries are completely unchanged** by it -- the cross-encoder is
re-scoring a ranking it then leaves alone.

But the per-band breakdown shows it is not useless either:

| overlap band | hybrid | reranked | delta |
|---|---|---|---|
| low (semantic, hardest) | 0.845 | 0.874 | +0.029 |
| medium | 0.981 | 0.981 | 0.000 |
| high (exact wording) | 0.931 | 1.000 | +0.069 |

and on the dev set reranking actively **hurt** exact-identifier questions
(`exact_code` 1.000 -> 0.852), because BM25 had already ranked them perfectly
and the cross-encoder reshuffled on semantic similarity.

## The first attempt at a policy, and why it failed

The original version skipped reranking when the top fused result led the
runner-up by >= 35 %. Measured against the held-out set, that rule fired on
**2 of 25 queries** and changed nothing: NDCG, wins, losses and ties came back
byte-identical to the always-rerank run.

The reason is arithmetic, not tuning. RRF scores a document at `1/(k + rank)`
with `k = 60`, so rank 1 scores 0.0164 and rank 2 scores 0.0161 -- a 1.6 %
gap. Summed over two retrievers the spread stays tiny:

```
median top-1 margin over the held-out set   0.048
largest margin observed at all              0.112
```

A 35 % relative lead is **unreachable by construction**. RRF deliberately
discards score magnitude in favour of rank position, so asking it for a
confidence margin asks for information it does not carry.

## The signal that does exist

Rank position is what RRF preserves, so the policy reads rank position:
**dense and sparse independently putting the same chunk first.** Two retrievers
that share no mechanism -- one embedding similarity, one lexical overlap --
agreeing on the top document is a genuine confidence signal, and it is
available on 13 of 25 held-out queries rather than 2.

When both retrievers rank a chunk first, RRF is guaranteed to rank it first
too (`2/(k+1)` beats any other attainable sum), so the fused ordering at the
position that matters most is already settled by agreement rather than by one
retriever's opinion.

## The policy

1. **Exact identifier matched.** The query contains a code such as
   `INC-2025-0042` and the top result contains that exact token. BM25 has
   answered this precisely; reranking can only disturb it.
2. **Retriever agreement.** Dense and sparse both rank the same chunk first.
3. **Decisive margin.** Retained as a secondary check for corpora whose score
   distribution is wider than this one's, with the default threshold
   recalibrated to 0.10 -- just above the observed median and near the observed
   maximum, so it fires on genuinely unusual gaps instead of never.

Everything else is reranked. The policy is deliberately conservative: when in
doubt it reranks, because the cost of skipping is a worse answer while the cost
of reranking is only latency.

`RERANK_POLICY=always` restores the previous behaviour, and `never` disables
reranking entirely.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from ..models.documents import ScoredChunk

log = logging.getLogger(__name__)

# Identifiers such as INC-2025-0042, DOC-PM-2025-0042 or NW-ANALYTICS.
#
# The router uses the same shape but uppercases the query first, which is safe
# there -- a false positive only protects a word from being rewritten. Here it
# is not: uppercasing makes every hyphenated English word look like a code, and
# the first measured run duly skipped reranking on "PRE-EXISTING",
# "FIRST-RESPONSE" and "FIRST-YEAR". Two narrower rules are used instead:
#
#   IDENTIFIER   matched against the query as typed, so NW-ANALYTICS qualifies
#                while "pre-existing" does not, never having been capitalised
#   _HAS_DIGIT   matched without regard to case, but only accepted when the
#                token carries a digit, so "inc-2025-0042" still works
IDENTIFIER = re.compile(r"\b[A-Z]{2,5}(?:-[A-Z0-9]{2,12})+\b")
_HAS_DIGIT = re.compile(r"\b[A-Za-z]{2,5}(?:-[A-Za-z0-9]{2,12})+\b")

# Calibrated against the held-out RRF score distribution (median 0.048,
# max 0.112). See the module docstring for why 0.35 was unreachable.
DEFAULT_MARGIN_THRESHOLD = 0.10


class RerankPolicy(StrEnum):
    ALWAYS = "always"  # previous behaviour
    ADAPTIVE = "adaptive"  # skip when the ranking is already confident
    NEVER = "never"


@dataclass(frozen=True)
class RerankDecision:
    """Whether to rerank, and why. The reason is recorded in the trace."""

    should_rerank: bool
    reason: str

    def __str__(self) -> str:
        return f"{'rerank' if self.should_rerank else 'skip'}: {self.reason}"


def _identifiers_in(query: str) -> list[str]:
    """Codes in the query, excluding ordinary hyphenated words.

    A token qualifies either because it was typed in capitals, or because it
    contains a digit, which no hyphenated English word does.
    """
    found = set(IDENTIFIER.findall(query))
    for token in _HAS_DIGIT.findall(query):
        if any(character.isdigit() for character in token):
            found.add(token.upper())
    return sorted(found)


def _exact_identifier_hit(query: str, results: list[ScoredChunk]) -> str | None:
    """An identifier in the query that appears verbatim in the top result."""
    identifiers = _identifiers_in(query)
    if not identifiers or not results:
        return None
    top_text = results[0].chunk.text.upper()
    for identifier in identifiers:
        if identifier in top_text:
            return identifier
    return None


def _retrievers_agree(
    fused: list[ScoredChunk],
    dense: list[ScoredChunk] | None,
    sparse: list[ScoredChunk] | None,
) -> bool:
    """Do dense and sparse independently rank the same chunk first?

    Both lists must be non-empty -- when BM25 is unavailable its empty result
    is not agreement, it is an absent second opinion, and the query is
    reranked. The fused top is checked too, so a disagreement introduced
    downstream (deduplication, filtering) cannot be mistaken for consensus.
    """
    if not fused or not dense or not sparse:
        return False
    top = fused[0].chunk.chunk_id
    return dense[0].chunk.chunk_id == top and sparse[0].chunk.chunk_id == top


def _margin(results: list[ScoredChunk]) -> float:
    """Relative gap between the top two fused scores."""
    if len(results) < 2:
        return 1.0
    top, second = results[0].score, results[1].score
    if top <= 0:
        return 0.0
    return (top - second) / top


def decide(
    query: str,
    fused: list[ScoredChunk],
    *,
    dense: list[ScoredChunk] | None = None,
    sparse: list[ScoredChunk] | None = None,
    policy: RerankPolicy | str = RerankPolicy.ADAPTIVE,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
) -> RerankDecision:
    """Should this query be reranked?

    `dense` and `sparse` are the pre-fusion result lists. They are optional so
    that callers holding only a fused ranking still get a usable decision, but
    without them the agreement rule cannot fire and the policy degrades to the
    margin check -- which, on RRF scores, means it reranks nearly everything.
    """
    policy = RerankPolicy(policy)

    if policy is RerankPolicy.NEVER:
        return RerankDecision(False, "reranking disabled by policy")
    if policy is RerankPolicy.ALWAYS:
        return RerankDecision(True, "policy=always")
    if not fused:
        return RerankDecision(False, "nothing to rerank")

    identifier = _exact_identifier_hit(query, fused)
    if identifier:
        return RerankDecision(False, f"exact identifier {identifier} already matched at rank 1")

    if _retrievers_agree(fused, dense, sparse):
        return RerankDecision(False, "dense and sparse independently agree on the top result")

    margin = _margin(fused)
    if margin >= margin_threshold:
        return RerankDecision(False, f"top result leads by {margin:.0%}, ranking already decisive")

    return RerankDecision(True, f"retrievers disagree and top margin is only {margin:.0%}")


__all__ = [
    "DEFAULT_MARGIN_THRESHOLD",
    "RerankDecision",
    "RerankPolicy",
    "decide",
]
