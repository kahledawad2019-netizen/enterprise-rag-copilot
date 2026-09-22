"""
The adaptive reranking policy.

## Why this file exists in the shape it does

The first version of this policy skipped reranking when the top fused result
led the runner-up by >= 35 %. It passed its unit tests -- because those tests
constructed scores like 1.0 and 0.4, which no RRF fusion ever produces.

Measured end to end it fired on 2 of 25 held-out queries and changed nothing.
RRF scores a document at `1/(k + rank)`, so with `k = 60` rank 1 and rank 2 sit
0.0164 and 0.0161 apart -- the observed median margin was 0.048 and the largest
margin ever seen was 0.112. A 35 % lead was unreachable by construction.

So `TestMarginArithmeticIsRealistic` below pins the arithmetic itself, using
scores produced by the real fusion function rather than invented ones. A unit
test that cannot fail when the feature is useless is not a test.
"""

from __future__ import annotations

import pytest

from enterprise_copilot.models.documents import Chunk, RetrievalMethod, ScoredChunk
from enterprise_copilot.reranking.policy import (
    DEFAULT_MARGIN_THRESHOLD,
    RerankPolicy,
    decide,
)


def scored(chunk_id: str, score: float, text: str = "some passage text") -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(chunk_id=chunk_id, doc_id="DOC-TEST-0001", text=text),
        score=score,
        method=RetrievalMethod.HYBRID,
    )


def rrf_scores(n: int, k: int = 60) -> list[float]:
    """The scores a single retriever's ranking actually contributes."""
    return [1.0 / (k + rank) for rank in range(1, n + 1)]


class TestPolicyOverrides:
    def test_never_skips_everything(self) -> None:
        decision = decide("any query", [scored("a", 0.0164)], policy=RerankPolicy.NEVER)
        assert not decision.should_rerank
        assert "disabled" in decision.reason

    def test_always_reranks_even_when_confident(self) -> None:
        """The escape hatch must ignore every confidence signal."""
        fused = [scored("a", 0.0164), scored("b", 0.0161)]
        decision = decide(
            "INC-2025-0042 impact",
            fused,
            dense=fused,
            sparse=fused,
            policy=RerankPolicy.ALWAYS,
        )
        assert decision.should_rerank

    def test_empty_candidates_are_not_reranked(self) -> None:
        assert not decide("q", []).should_rerank


class TestExactIdentifier:
    def test_identifier_present_in_top_result_skips(self) -> None:
        fused = [scored("a", 0.0164, "Incident INC-2025-0042 root cause"), scored("b", 0.0161)]
        decision = decide("What caused INC-2025-0042?", fused)
        assert not decision.should_rerank
        assert "INC-2025-0042" in decision.reason

    def test_identifier_absent_from_top_result_does_not_skip(self) -> None:
        """The identifier must be matched, not merely mentioned in the query.

        The scores here are real RRF values. An earlier draft of this test used
        0.9 and 0.8, which is an 11 % margin and tripped the decisiveness rule
        instead -- the test passed for the wrong reason.
        """
        fused = [scored("a", 0.0164, "unrelated passage"), scored("b", 0.0161)]
        decision = decide("What caused INC-2025-0042?", fused)
        assert decision.should_rerank

    def test_lowercase_identifier_in_the_query_still_matches(self) -> None:
        """Accepted because it carries digits, not because of uppercasing."""
        fused = [scored("a", 0.0164, "Incident INC-2025-0042 root cause")]
        assert not decide("what caused inc-2025-0042?", fused).should_rerank

    def test_uppercase_identifier_without_digits_matches(self) -> None:
        fused = [scored("a", 0.0164, "The NW-ANALYTICS service owns this job")]
        assert not decide("who owns NW-ANALYTICS?", fused).should_rerank

    @pytest.mark.parametrize("word", ["pre-existing", "first-response", "first-year"])
    def test_hyphenated_english_words_are_not_identifiers(self, word: str) -> None:
        """Regression: the first measured run skipped reranking on all three.

        The matcher uppercased the query before testing it, so every hyphenated
        word looked like a code and the skip was taken for a bogus reason. The
        words here are the actual ones observed in the held-out set.
        """
        a = scored("a", 0.0164, f"a passage about {word} coverage")
        b = scored("b", 0.0161)
        decision = decide(f"what is the {word} rule?", [a, b], dense=[a, b], sparse=[b, a])
        assert decision.should_rerank, decision.reason


class TestRetrieverAgreement:
    """The signal that replaced the unreachable margin rule."""

    def test_agreement_on_top_result_skips(self) -> None:
        top, other = scored("a", 0.033), scored("b", 0.032)
        decision = decide("q", [top, other], dense=[top, other], sparse=[top, other])
        assert not decision.should_rerank
        assert "agree" in decision.reason

    def test_disagreement_reranks(self) -> None:
        a, b = scored("a", 0.033), scored("b", 0.032)
        decision = decide("q", [a, b], dense=[a, b], sparse=[b, a])
        assert decision.should_rerank

    def test_missing_sparse_is_not_agreement(self) -> None:
        """An unavailable BM25 index is an absent opinion, not a concurring one."""
        a, b = scored("a", 0.033), scored("b", 0.032)
        assert decide("q", [a, b], dense=[a, b], sparse=[]).should_rerank
        assert decide("q", [a, b], dense=[a, b], sparse=None).should_rerank

    def test_agreement_on_a_chunk_that_is_not_the_fused_top_is_not_agreement(self) -> None:
        """Deduplication or filtering downstream can move the fused top."""
        a, b = scored("a", 0.033), scored("b", 0.032)
        decision = decide("q", [b, a], dense=[a, b], sparse=[a, b])
        assert decision.should_rerank

    def test_caller_without_prefusion_lists_still_gets_a_decision(self) -> None:
        a, b = scored("a", 0.033), scored("b", 0.032)
        decision = decide("q", [a, b])
        assert decision.should_rerank  # degrades to the margin check


class TestMarginArithmeticIsRealistic:
    """Pins the failure the first implementation shipped with."""

    def test_rrf_margins_never_reach_the_old_threshold(self) -> None:
        """The regression that made the original policy a no-op.

        Two retrievers, worst case for the runner-up: the top document is
        ranked 1 by both and the second is ranked 2 by both. Even this, the
        largest gap two-retriever RRF can produce among adjacent ranks, falls
        far short of 0.35.
        """
        k = 60
        top = 2 * (1.0 / (k + 1))
        second = 2 * (1.0 / (k + 2))
        margin = (top - second) / top
        assert margin < 0.02, f"expected a tiny RRF margin, got {margin:.3f}"
        assert margin < 0.35, "the original 0.35 threshold was unreachable"

    def test_recalibrated_threshold_is_above_the_observed_median(self) -> None:
        """Measured over the held-out set: median 0.048, max 0.112."""
        assert 0.048 < DEFAULT_MARGIN_THRESHOLD <= 0.112

    def test_single_result_is_treated_as_decisive(self) -> None:
        assert not decide("q", [scored("a", 0.016)]).should_rerank

    def test_zero_top_score_does_not_divide_by_zero(self) -> None:
        decision = decide("q", [scored("a", 0.0), scored("b", 0.0)])
        assert decision.should_rerank

    @pytest.mark.parametrize("n", [2, 5, 10, 25])
    def test_real_rrf_score_shape_reranks(self, n: int) -> None:
        """A realistic single-retriever ranking is never 'decisive' by margin."""
        fused = [scored(f"c{i}", s) for i, s in enumerate(rrf_scores(n))]
        assert decide("q", fused).should_rerank


class TestDecisionIsExplainable:
    """Every decision is written into the retrieval trace, so it must read well."""

    def test_str_names_the_action_and_the_reason(self) -> None:
        a, b = scored("a", 0.033), scored("b", 0.032)
        skipped = str(decide("q", [a, b], dense=[a, b], sparse=[a, b]))
        assert skipped.startswith("skip: ")
        reranked = str(decide("q", [a, b], dense=[a, b], sparse=[b, a]))
        assert reranked.startswith("rerank: ")
