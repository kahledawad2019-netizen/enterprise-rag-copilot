"""
Tests for the chat and embedding client layer.

The point of this layer is that swapping provider changes the transport and
nothing else, so what is worth testing is the translation: Ollama's option
names to OpenAI's parameter names, OpenAI's response envelope back to the
shape six call sites already read, and the Workers AI envelope to a list of
vectors.

Nothing here touches the network. Every test either exercises a pure function
or substitutes a fake `requests` module.
"""

from __future__ import annotations

import sys
import types

import pytest

from enterprise_copilot.config import get_settings, reset_settings_cache
from enterprise_copilot.llm.clients import (
    MIN_TEMPERATURE,
    ChatClientError,
    CloudflareEmbeddingClient,
    EmbeddingClientError,
    OpenAICompatChatClient,
    _extract_cf_vectors,
    _to_ollama_shape,
    _translate_options,
    build_chat_client,
    build_embedding_client,
    describe_providers,
)


class TestOptionTranslation:
    def test_ollama_names_become_openai_names(self):
        translated = _translate_options({"temperature": 0.7, "num_predict": 512})
        assert translated["temperature"] == 0.7
        assert translated["max_tokens"] == 512

    def test_num_ctx_is_dropped(self):
        """Context length is a property of a hosted deployment, not a request."""
        assert "num_ctx" not in _translate_options({"num_ctx": 8192})
        assert _translate_options({"num_ctx": 8192}) == {}

    def test_zero_temperature_is_raised_to_the_minimum(self):
        """Groq rejects temperature == 0 and silently substitutes 1e-8.

        The project asks for 0.0 everywhere it wants determinism. Sending the
        substitution ourselves means the behaviour does not depend on one
        vendor's undocumented coercion - and that a provider which does NOT
        coerce returns an error we can see rather than silently sampling.
        """
        assert _translate_options({"temperature": 0.0})["temperature"] == MIN_TEMPERATURE

    def test_none_and_empty_are_safe(self):
        assert _translate_options(None) == {}
        assert _translate_options({}) == {}


class TestResponseShape:
    def test_openai_envelope_becomes_the_shape_call_sites_read(self):
        """Six call sites read response["message"]["content"] directly."""
        mapped = _to_ollama_shape(
            {
                "model": "llama-3.3-70b-versatile",
                "choices": [{"message": {"role": "assistant", "content": "SELECT 1"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8},
            }
        )
        assert mapped["message"]["content"] == "SELECT 1"
        assert mapped["prompt_eval_count"] == 120
        assert mapped["eval_count"] == 8

    def test_missing_choices_yields_empty_content_not_a_crash(self):
        """A refusal or a filtered response can come back with no choices."""
        assert _to_ollama_shape({"choices": []})["message"]["content"] == ""

    def test_missing_usage_is_tolerated(self):
        mapped = _to_ollama_shape({"choices": [{"message": {"content": "x"}}]})
        assert mapped["prompt_eval_count"] is None


class TestCloudflareVectorExtraction:
    def test_data_shape(self):
        body = {"result": {"shape": [2, 3], "data": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}}
        assert _extract_cf_vectors(body, "@cf/baai/bge-m3") == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    def test_response_list_of_lists_shape(self):
        body = {"result": {"response": [[0.1, 0.2]]}}
        assert _extract_cf_vectors(body, "@cf/baai/bge-m3") == [[0.1, 0.2]]

    def test_response_list_of_objects_shape(self):
        body = {"result": {"response": [{"embedding": [0.1, 0.2]}]}}
        assert _extract_cf_vectors(body, "@cf/baai/bge-m3") == [[0.1, 0.2]]

    def test_unknown_shape_names_what_it_actually_got(self):
        """Cloudflare documents the request schema for bge-m3 but not a stable
        response schema, and the bge family has not been uniform. If the shape
        changes, the error must say what arrived - otherwise this is an
        afternoon of guessing."""
        with pytest.raises(EmbeddingClientError) as excinfo:
            _extract_cf_vectors({"result": {"vectors": [[0.1]]}}, "@cf/baai/bge-m3")
        assert "vectors" in str(excinfo.value)


class TestFactories:
    def test_hosted_chat_without_a_key_is_refused_with_the_remedy(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        reset_settings_cache()

        with pytest.raises(ChatClientError) as excinfo:
            build_chat_client(get_settings())
        assert "LLM_API_KEY" in str(excinfo.value)

    def test_hosted_chat_without_a_model_is_refused(self, monkeypatch):
        """The profile's model name is an Ollama tag. Sending 'llama3.1:8b' to
        Groq is a 404 that reads like a network fault."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "gsk_fake")
        monkeypatch.setenv("LLM_MODEL", "")
        reset_settings_cache()

        with pytest.raises(ChatClientError) as excinfo:
            build_chat_client(get_settings())
        assert "LLM_MODEL" in str(excinfo.value)

    def test_hosted_chat_builds_with_a_key_and_model(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "gsk_fake")
        monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
        reset_settings_cache()

        client = build_chat_client(get_settings())
        assert isinstance(client, OpenAICompatChatClient)

    def test_cloudflare_embeddings_need_account_and_token(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "cloudflare")
        monkeypatch.setenv("EMBEDDING_MODEL", "@cf/baai/bge-m3")
        monkeypatch.delenv("EMBEDDING_CLOUDFLARE_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("EMBEDDING_CLOUDFLARE_API_TOKEN", raising=False)
        reset_settings_cache()

        with pytest.raises(EmbeddingClientError) as excinfo:
            build_embedding_client(get_settings())
        assert "ACCOUNT_ID" in str(excinfo.value)

    def test_cloudflare_embeddings_build(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "cloudflare")
        monkeypatch.setenv("EMBEDDING_MODEL", "@cf/baai/bge-m3")
        monkeypatch.setenv("EMBEDDING_CLOUDFLARE_ACCOUNT_ID", "acct123")
        monkeypatch.setenv("EMBEDDING_CLOUDFLARE_API_TOKEN", "cf_fake")
        reset_settings_cache()

        client = build_embedding_client(get_settings())
        assert isinstance(client, CloudflareEmbeddingClient)

    def test_blank_api_key_means_absent_not_empty(self, monkeypatch):
        """The same mistake with MSSQL_PASSWORD produced a real defect: an
        empty SecretStr passed an `is not None` check and reached the driver."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "   ")
        monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
        reset_settings_cache()

        assert get_settings().llm.api_key is None


class TestDisclosure:
    def test_describe_never_includes_the_key(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "gsk_super_secret_value")
        monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
        reset_settings_cache()

        described = describe_providers(get_settings())
        assert "gsk_super_secret_value" not in str(described)
        assert described["chat_key"] == "set"

    def test_hosted_provider_is_reported_as_leaving_the_machine(self, monkeypatch):
        """README.md and docs/security.md claim the system is fully local.
        Once a hosted provider is configured that claim is false, and the
        system has to be able to say so."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "gsk_fake")
        monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
        reset_settings_cache()
        assert describe_providers(get_settings())["leaves_machine"] == "yes"

    def test_all_local_is_reported_as_staying_put(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
        reset_settings_cache()
        assert describe_providers(get_settings())["leaves_machine"] == "no"


class TestChatOverHttp:
    """The request/response round trip, with `requests` replaced by a fake."""

    @staticmethod
    def _install_fake_requests(monkeypatch, *, status=200, body=None, capture=None):
        class FakeResponse:
            status_code = status
            text = "" if body is not None else "upstream said no"

            @staticmethod
            def json():
                return body

        def fake_post(url, headers=None, json=None, timeout=None, stream=False):
            if capture is not None:
                capture.update({"url": url, "headers": headers, "json": json})
            return FakeResponse()

        module = types.ModuleType("requests")
        module.post = fake_post
        monkeypatch.setitem(sys.modules, "requests", module)

    def test_json_mode_sets_response_format(self, monkeypatch):
        """The router depends on this. Without it the model wraps the JSON in
        prose, the parse fails, and the router falls back to its heuristic
        while appearing to still be using the model."""
        capture: dict = {}
        self._install_fake_requests(
            monkeypatch,
            body={"choices": [{"message": {"content": "{}"}}]},
            capture=capture,
        )
        client = OpenAICompatChatClient("https://api.groq.com/openai/v1", "gsk_fake", 30)
        client.chat(model="m", messages=[{"role": "user", "content": "hi"}], format="json")

        assert capture["json"]["response_format"] == {"type": "json_object"}

    def test_keep_alive_is_accepted_and_ignored(self, monkeypatch):
        """Call sites pass Ollama's keep_alive. A hosted API has no such
        concept, and the parameter must not become a TypeError."""
        capture: dict = {}
        self._install_fake_requests(
            monkeypatch,
            body={"choices": [{"message": {"content": "ok"}}]},
            capture=capture,
        )
        client = OpenAICompatChatClient("https://api.groq.com/openai/v1", "gsk_fake", 30)
        result = client.chat(
            model="m", messages=[{"role": "user", "content": "hi"}], keep_alive="5m"
        )

        assert result["message"]["content"] == "ok"
        assert "keep_alive" not in capture["json"]

    def test_the_key_is_sent_as_a_bearer_token(self, monkeypatch):
        capture: dict = {}
        self._install_fake_requests(
            monkeypatch,
            body={"choices": [{"message": {"content": "ok"}}]},
            capture=capture,
        )
        client = OpenAICompatChatClient("https://api.groq.com/openai/v1", "gsk_fake", 30)
        client.chat(model="m", messages=[])
        assert capture["headers"]["Authorization"] == "Bearer gsk_fake"

    @pytest.mark.parametrize(
        ("status", "expected_hint"),
        [(401, "API key"), (404, "model name"), (429, "Rate limited")],
    )
    def test_http_errors_say_what_to_fix(self, monkeypatch, status, expected_hint):
        self._install_fake_requests(monkeypatch, status=status)
        client = OpenAICompatChatClient("https://api.groq.com/openai/v1", "gsk_fake", 30)

        with pytest.raises(ChatClientError) as excinfo:
            client.chat(model="m", messages=[])
        assert expected_hint in str(excinfo.value)


@pytest.fixture(autouse=True)
def _restore_settings():
    """Each test here mutates the environment; the cache must not leak."""
    yield
    reset_settings_cache()
