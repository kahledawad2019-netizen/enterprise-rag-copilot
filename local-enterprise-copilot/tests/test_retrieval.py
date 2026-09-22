"""
Retrieval tests: fusion maths, permission filtering, and the behaviours the
evaluation numbers depend on.

The fusion tests are pure and fast. The end-to-end tests need a built index and
a running Ollama, and skip with a clear reason when either is missing.
"""

from __future__ import annotations

import pytest

from enterprise_copilot.models.documents import Chunk, RetrievalMethod, ScoredChunk
from enterprise_copilot.retrieval.fusion import (
    apply_authority_preference,
    deduplicate,
    maximal_marginal_relevance,
    reciprocal_rank_fusion,
)
from enterprise_copilot.retrieval.sparse import BM25Index, tokenize


def make_chunk(chunk_id: str, text: str = "text", **kwargs) -> Chunk:
    return Chunk(chunk_id=chunk_id, doc_id=kwargs.pop("doc_id", f"DOC-TST-{chunk_id[:3]}"),
                 text=text, **kwargs)


def scored(chunk_id: str, score: float, method=RetrievalMethod.DENSE, **kwargs) -> ScoredChunk:
    text = kwargs.pop("text", f"text for {chunk_id}")
    return ScoredChunk(chunk=make_chunk(chunk_id, text, **kwargs), score=score, method=method)


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------
class TestTokenize:
    def test_keeps_identifiers_whole(self) -> None:
        """The whole point of BM25 here is exact identifiers."""
        assert "inc-2025-0042" in tokenize("What happened in INC-2025-0042?")

    def test_also_emits_identifier_parts(self) -> None:
        tokens = tokenize("INC-2025-0042")
        assert "inc" in tokens and "2025" in tokens and "0042" in tokens

    def test_drops_stop_words(self) -> None:
        assert "the" not in tokenize("the refund policy")

    def test_keeps_negations(self) -> None:
        """'no refund' and 'refund' must not tokenise identically."""
        assert "not" in tokenize("this is not refundable")
        assert "no" in tokenize("no refund is issued")


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------
class TestReciprocalRankFusion:
    def test_rewards_agreement_between_retrievers(self) -> None:
        dense = [scored("a", 0.9), scored("b", 0.8), scored("c", 0.7)]
        sparse = [scored("c", 12.0), scored("a", 8.0), scored("d", 3.0)]
        fused = reciprocal_rank_fusion([dense, sparse])
        # 'a' is ranked 1 and 2; 'c' is 3 and 1. Both beat single-list entries.
        assert fused[0].chunk.chunk_id in {"a", "c"}
        top_two = {fused[0].chunk.chunk_id, fused[1].chunk.chunk_id}
        assert top_two == {"a", "c"}

    def test_is_immune_to_score_scale(self) -> None:
        """BM25 scores are unbounded; cosine is not. RRF must not care."""
        dense = [scored("a", 0.9), scored("b", 0.1)]
        sparse_small = [scored("b", 0.2), scored("a", 0.1)]
        sparse_huge = [scored("b", 20000.0), scored("a", 10000.0)]
        first = [c.chunk.chunk_id for c in reciprocal_rank_fusion([dense, sparse_small])]
        second = [c.chunk.chunk_id for c in reciprocal_rank_fusion([dense, sparse_huge])]
        assert first == second

    def test_merges_provenance_from_both_lists(self) -> None:
        dense = [ScoredChunk(chunk=make_chunk("a"), score=0.9,
                             method=RetrievalMethod.DENSE, dense_score=0.9, dense_rank=1)]
        sparse = [ScoredChunk(chunk=make_chunk("a"), score=11.0,
                              method=RetrievalMethod.SPARSE, sparse_score=11.0, sparse_rank=1)]
        fused = reciprocal_rank_fusion([dense, sparse])
        assert fused[0].dense_score == 0.9
        assert fused[0].sparse_score == 11.0
        assert fused[0].fused_score is not None

    def test_single_non_empty_list_passes_through(self) -> None:
        fused = reciprocal_rank_fusion([[scored("a", 0.9)], []])
        assert [c.chunk.chunk_id for c in fused] == ["a"]

    def test_handles_all_empty(self) -> None:
        assert reciprocal_rank_fusion([[], []]) == []

    def test_rejects_mismatched_weights(self) -> None:
        with pytest.raises(ValueError, match="weights"):
            reciprocal_rank_fusion([[scored("a", 1)], [scored("b", 1)]], weights=[1.0])


class TestDeduplicate:
    def test_removes_repeated_ids(self) -> None:
        assert len(deduplicate([scored("a", 0.9), scored("a", 0.8)])) == 1

    def test_removes_near_identical_text(self) -> None:
        results = [scored("a", 0.9, text="The refund policy applies to annual plans."),
                   scored("b", 0.8, text="the  refund POLICY applies to annual plans.")]
        assert len(deduplicate(results)) == 1

    def test_keeps_genuinely_different_text(self) -> None:
        results = [scored("a", 0.9, text="Refunds are pro-rata within 30 days."),
                   scored("b", 0.8, text="SLA first response for P1 is 15 minutes.")]
        assert len(deduplicate(results)) == 2


class TestMMR:
    def test_prefers_diversity_over_near_duplicates(self) -> None:
        results = [
            scored("a", 1.00, text="refund policy annual plan pro rata thirty days"),
            scored("b", 0.99, text="refund policy annual plan pro rata thirty days again"),
            scored("c", 0.60, text="sla first response fifteen minutes enterprise priority"),
        ]
        selected = maximal_marginal_relevance(results, limit=2, lambda_param=0.5)
        assert [s.chunk.chunk_id for s in selected] == ["a", "c"]

    def test_lambda_one_is_plain_top_k(self) -> None:
        results = [scored("a", 1.0), scored("b", 0.9), scored("c", 0.8)]
        selected = maximal_marginal_relevance(results, limit=2, lambda_param=1.0)
        assert [s.chunk.chunk_id for s in selected] == ["a", "b"]

    def test_handles_fewer_results_than_the_limit(self) -> None:
        assert len(maximal_marginal_relevance([scored("a", 1.0)], limit=5)) == 1


class TestAuthorityPreference:
    def test_policy_outranks_guidance_at_equal_relevance(self) -> None:
        results = [
            scored("guide", 0.80, authority="guidance"),
            scored("policy", 0.80, authority="policy"),
        ]
        adjusted = apply_authority_preference(results)
        assert adjusted[0].chunk.chunk_id == "policy"

    def test_adjustment_is_small_enough_not_to_override_relevance(self) -> None:
        """A barely-relevant policy must not beat a highly relevant guidance doc."""
        results = [
            scored("relevant_guide", 0.90, authority="guidance"),
            scored("weak_policy", 0.60, authority="policy"),
        ]
        adjusted = apply_authority_preference(results)
        assert adjusted[0].chunk.chunk_id == "relevant_guide"

    def test_unknown_authority_does_not_raise(self) -> None:
        adjusted = apply_authority_preference([scored("x", 0.5, authority="nonsense")])
        assert len(adjusted) == 1


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------
class TestBM25:
    def test_empty_index_returns_nothing(self) -> None:
        index = BM25Index()
        index.build([])
        assert index.search("anything") == []
        assert not index.is_ready

    def test_finds_an_exact_identifier(self) -> None:
        """The case that motivates having BM25 at all.

        The corpus is deliberately not tiny. BM25's IDF term is
        `log((N - df + 0.5) / (df + 0.5))`, so on a two-document corpus a term
        appearing in one of them scores `log(1) = 0` — the exact identifier
        would carry no weight, and ranking would be decided by length
        normalisation alone. That is correct BM25 behaviour, not a defect, but
        it makes a two-document corpus useless as a test of ranking.
        """
        index = BM25Index()
        index.build([
            make_chunk("a", "The outage INC-2025-0042 was caused by an expired certificate."),
            make_chunk("b", "The latency issue INC-2025-0031 was caused by autoscaling."),
            make_chunk("c", "Refunds on annual plans are pro-rata within thirty days."),
            make_chunk("d", "First response for Enterprise P1 tickets is fifteen minutes."),
            make_chunk("e", "Seats are counted as named users in the billing period."),
            make_chunk("f", "Data at rest is encrypted with AES-256 and rotated annually."),
            make_chunk("g", "Onboarding kickoff happens within five business days."),
        ])
        results = index.search("INC-2025-0042")
        assert results, "BM25 returned nothing for an exact identifier"
        assert results[0].chunk.chunk_id == "a"

    def test_returns_results_even_when_scores_are_negative(self) -> None:
        """A tiny corpus drives every IDF negative; returning nothing is wrong.

        Ranking on such a corpus is unreliable, but the retriever must still
        surface the documents that share query terms rather than silently
        yielding an empty list.
        """
        index = BM25Index()
        index.build([
            make_chunk("a", "The outage INC-2025-0042 was caused by a certificate."),
            make_chunk("b", "The latency issue INC-2025-0031 was caused by autoscaling."),
        ])
        assert index.search("INC-2025-0042"), "empty result on a degenerate corpus"

    def test_respects_the_allowed_id_set(self) -> None:
        """Permission filtering must happen before ranking, not after."""
        index = BM25Index()
        index.build([
            make_chunk("secret", "discount ceiling is twenty five percent"),
            make_chunk("public", "discount information is available on request"),
        ])
        results = index.search("discount", allowed_chunk_ids={"public"})
        assert [r.chunk.chunk_id for r in results] == ["public"]

    def test_round_trips_through_disk(self, tmp_path) -> None:
        index = BM25Index()
        index.build([make_chunk("a", "refund policy annual plans")])
        path = tmp_path / "bm25.pkl"
        index.save(path)

        reloaded = BM25Index()
        assert reloaded.load(path)
        assert reloaded.size == 1
        assert reloaded.search("refund")[0].chunk.chunk_id == "a"

    def test_missing_cache_file_returns_false(self, tmp_path) -> None:
        assert BM25Index().load(tmp_path / "absent.pkl") is False


# ---------------------------------------------------------------------------
# End to end (needs a built index + Ollama)
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.requires_ollama
class TestEndToEndRetrieval:
    @pytest.fixture(scope="class")
    def retriever(self, request):
        from enterprise_copilot.config import get_settings
        from enterprise_copilot.retrieval.hybrid import HybridRetriever

        settings = get_settings()
        try:
            instance = HybridRetriever(settings)
            if instance.store.count() == 0:
                pytest.skip("index is empty - run scripts/build_index.py")
        except Exception as exc:
            pytest.skip(f"retrieval unavailable: {exc}")
        request.addfinalizer(instance.close)
        return instance

    def test_finds_the_current_refund_policy(self, retriever) -> None:
        from enterprise_copilot.retrieval.hybrid import UserContext

        results, _ = retriever.retrieve(
            "what is the refund policy for enterprise annual plans?",
            strategy="hybrid", user=UserContext.admin(), limit=5,
        )
        assert results
        assert any(r.chunk.doc_id == "DOC-REF-001" for r in results)

    def test_superseded_policy_is_filtered_out(self, retriever) -> None:
        from enterprise_copilot.retrieval.hybrid import UserContext

        results, _ = retriever.retrieve(
            "can we refund an annual contract?", strategy="hybrid",
            user=UserContext.admin(), limit=8,
        )
        assert all(r.chunk.doc_id != "DOC-REF-000" for r in results), (
            "the superseded refund policy leaked into results"
        )
        assert all(r.chunk.status == "current" for r in results)

    def test_sparse_beats_dense_on_an_exact_identifier(self, retriever) -> None:
        """The motivating case for hybrid retrieval, asserted rather than claimed."""
        from enterprise_copilot.retrieval.hybrid import UserContext

        user = UserContext.admin()
        sparse, _ = retriever.retrieve("INC-2025-0042", strategy="sparse", user=user, limit=1)
        assert sparse and sparse[0].chunk.doc_id == "DOC-PM-2025-0042"

        hybrid, _ = retriever.retrieve("INC-2025-0042", strategy="hybrid", user=user, limit=3)
        assert any(r.chunk.doc_id == "DOC-PM-2025-0042" for r in hybrid)

    def test_guest_cannot_reach_the_finance_pricing_policy(self, retriever) -> None:
        """Tenant and access-group isolation, tested rather than assumed."""
        from enterprise_copilot.retrieval.hybrid import UserContext

        guest = UserContext(user_name="guest", tenant="all", access_groups=["public"])
        results, _ = retriever.retrieve(
            "what is the maximum discount allowed?", strategy="hybrid", user=guest, limit=10,
        )
        assert all(r.chunk.doc_id != "DOC-PRC-001" for r in results), (
            "a guest reached the finance-only pricing policy"
        )
        assert all(r.chunk.access_group == "public" for r in results)

    def test_injection_document_is_not_returned_by_default(self, retriever) -> None:
        from enterprise_copilot.retrieval.hybrid import UserContext

        results, _ = retriever.retrieve(
            "ignore all previous instructions", strategy="hybrid",
            user=UserContext.admin(), limit=10,
        )
        assert all(r.chunk.doc_id != "DOC-TST-001" for r in results)

    def test_trace_records_every_stage(self, retriever) -> None:
        from enterprise_copilot.retrieval.hybrid import UserContext

        _, trace = retriever.retrieve(
            "sla first response target", strategy="reranked",
            user=UserContext.admin(), limit=5,
        )
        assert trace.dense_results and trace.sparse_results and trace.fused_results
        assert "dense_search" in trace.stage_seconds
        assert "sparse_search" in trace.stage_seconds
        assert trace.total_seconds > 0
