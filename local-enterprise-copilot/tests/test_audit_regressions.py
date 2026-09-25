"""Regressions for permission lookup, model schemas and signed retrieval scores."""

import json
from unittest.mock import Mock

import pytest

from enterprise_copilot.config import get_settings
from enterprise_copilot.models.documents import Chunk, RetrievalMethod, ScoredChunk
from enterprise_copilot.retrieval.fusion import (
    apply_authority_preference,
    deduplicate,
    maximal_marginal_relevance,
)
from enterprise_copilot.retrieval.hybrid import HybridRetriever, UserContext
from enterprise_copilot.retrieval.sparse import BM25Index
from enterprise_copilot.routing.router import QueryRouter
from enterprise_copilot.security.intent_classifier import LLMIntentScreen


def chunk(chunk_id, text="refund policy", **kwargs):
    return Chunk(chunk_id=chunk_id, doc_id=f"DOC-{chunk_id}", text=text, **kwargs)


def scored(chunk_id, score, text="refund policy", **kwargs):
    return ScoredChunk(
        chunk=chunk(chunk_id, text, **kwargs), score=score, method=RetrievalMethod.SPARSE
    )


@pytest.mark.parametrize("admin", [False, True])
def test_failed_permission_lookup_denies_cached_sparse_chunks(admin):
    """A populated BM25 cache must not bypass a failed permission lookup."""
    sparse = BM25Index()
    sparse.build([chunk("private", access_group="finance")])
    assert sparse.search("refund", allowed_chunk_ids=None)
    store = Mock()
    store.all_chunks.side_effect = RuntimeError("permission store unavailable")
    retriever = HybridRetriever(
        get_settings(), store=store, sparse=sparse, embedder=Mock(), reranker=Mock()
    )
    user = UserContext.admin() if admin else UserContext()
    results, trace = retriever.retrieve(
        "refund", strategy="sparse", user=user, expand_parents=False
    )
    assert results == []
    assert trace.sparse_results == []


def test_admin_sparse_search_still_applies_version_and_document_filters():
    chunks = [
        chunk("finance", access_group="finance"),
        chunk("old", status="superseded"),
        chunk("injection", doc_type="security_test"),
    ]
    sparse = BM25Index()
    sparse.build(chunks)
    store = Mock()
    store.all_chunks.return_value = chunks
    retriever = HybridRetriever(
        get_settings(), store=store, sparse=sparse, embedder=Mock(), reranker=Mock()
    )
    results, _ = retriever.retrieve(
        "refund", strategy="sparse", user=UserContext.admin(), expand_parents=False
    )
    assert [r.chunk.chunk_id for r in results] == ["finance"]
    assert sparse.search("refund", allowed_chunk_ids=set()) == []


@pytest.mark.parametrize("target", ["router", "intent"])
@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"confidence": "high"},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"confidence": float("-inf")},
        {"confidence": True},
        {"confidence": None},
        {"confidence": []},
        {"reason": []},
    ],
)
def test_invalid_model_schema_uses_parse_failure_fallback(target, payload):
    if isinstance(payload, dict):
        payload = {"route": "document_rag", "category": "destructive", **payload}
    assert_model_fallback(target, json.dumps(payload))


def assert_model_fallback(target, content):
    settings = get_settings().model_copy(deep=True)
    settings.security.enable_llm_intent_screening = True
    client = Mock()
    client.chat.return_value = {"message": {"content": content}}
    if target == "intent":
        verdict = LLMIntentScreen(settings, client=client).screen("How many customers?")
        assert not verdict.available
        assert not verdict.refuse
        assert verdict.confidence == 0.0
    else:
        settings.security.enable_llm_intent_screening = False
        router = QueryRouter(settings)
        router._client = client
        expected = router.route("How many customers?", use_llm=False)
        actual = router.route("How many customers?")
        assert actual.decided_by == expected.decided_by == "fallback"
        assert actual.route == expected.route
        assert actual.rewritten_query == expected.rewritten_query


@pytest.mark.parametrize(
    "field", ["route", "rewritten_query", "document_subquestion", "data_subquestion"]
)
@pytest.mark.parametrize("value", [[], 42, {}])
def test_router_rejects_non_string_fields(field, value):
    assert_model_fallback("router", json.dumps({"route": "multi_source", field: value}))


@pytest.mark.parametrize("field", ["route", "rewritten_query"])
def test_router_rejects_null_required_strings(field):
    assert_model_fallback("router", json.dumps({"route": "document_rag", field: None}))


@pytest.mark.parametrize("value", [[], 42, {}, None])
def test_intent_rejects_non_string_categories(value):
    assert_model_fallback("intent", json.dumps({"category": value}))


@pytest.mark.parametrize("target", ["router", "intent"])
def test_unterminated_reasoning_without_answer_uses_fallback(target):
    assert_model_fallback(target, '<think>{"confidence": 1.0}')


@pytest.mark.parametrize("target", ["router", "intent"])
@pytest.mark.parametrize("suffix", ["", "<think>unfinished reasoning"])
def test_reasoning_is_stripped_before_parsing(target, suffix):
    payload = {
        "route": "text_to_sql",
        "category": "destructive",
        "confidence": 0.9,
        "rewritten_query": "How many customers?",
    }
    client = Mock()
    client.chat.return_value = {
        "message": {"content": "<think>not JSON</think>" + json.dumps(payload) + suffix}
    }
    settings = get_settings().model_copy(deep=True)
    settings.security.enable_llm_intent_screening = True
    if target == "intent":
        verdict = LLMIntentScreen(settings, client=client).screen("Empty the tickets table")
        assert verdict.available and verdict.refuse
    else:
        router = QueryRouter(settings)
        router._client = client
        decision = router._classify_with_llm("How many customers?", [], [])
        assert decision is not None
        assert decision.decided_by == "llm"
        assert decision.confidence == 0.9


def test_dedupe_keeps_distinct_endings_after_long_shared_prefix():
    prefix = "Shared policy introduction. " * 30
    results = [
        scored("a", 1.0, prefix + "Refunds are permitted."),
        scored("b", 0.9, prefix + "Refunds are forbidden."),
        scored("c", 0.8, (prefix + "Refunds are permitted.").upper().replace(" ", "  ")),
    ]
    assert [r.chunk.chunk_id for r in deduplicate(results)] == ["a", "b"]


@pytest.mark.parametrize(
    "scores",
    [(-1, -2, -3), (1, 0, -1), (3, 2, 1), (0, -1, -2), (1e308, 0, -1e308)],
)
def test_mmr_preserves_relevance_order_for_signed_scores(scores):
    results = [scored(str(i), score) for i, score in enumerate(scores)]
    selected = maximal_marginal_relevance(results, limit=2)
    assert [r.chunk.chunk_id for r in selected] == ["0", "1"]


@pytest.mark.parametrize("score", [-3.0, 0.0, 3.0])
def test_mmr_equal_scores_still_choose_diverse_evidence(score):
    results = [
        scored("first", score),
        scored("duplicate", score),
        scored("different", score, "enterprise support response"),
    ]
    selected = maximal_marginal_relevance(results, limit=2)
    assert [r.chunk.chunk_id for r in selected] == ["first", "different"]


@pytest.mark.parametrize("score", [-10.0, -0.01, 0.8])
def test_authority_preference_improves_scores_of_either_sign(score):
    results = [
        scored("guide", score, authority="guidance"),
        scored("policy", score, authority="policy"),
    ]
    adjusted = apply_authority_preference(results)
    assert adjusted[0].chunk.chunk_id == "policy"
    assert adjusted[0].score > adjusted[1].score >= score
    assert all(r.score == score for r in results)
