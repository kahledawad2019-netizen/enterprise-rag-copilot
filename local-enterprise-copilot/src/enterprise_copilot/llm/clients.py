"""
Chat and embedding clients for providers other than Ollama.

## Why these imitate Ollama's interface rather than replacing it

The project called `ollama.Client` directly from six places, each with its own
error handling, its own streaming loop, and its own option dictionary. The
tempting move is a clean neutral abstraction that every call site is rewritten
against. The cheaper and far safer move - with 323 passing tests riding on
those call sites - is to keep Ollama's method signatures and return shapes and
implement them over a different transport. Each call site then changes by one
line: which client it is handed.

So `chat()` takes `model`, `messages`, `options`, `format`, `keep_alive` and
`stream`, and returns `{"message": {"content": ...}, "prompt_eval_count": ...,
"eval_count": ...}` - because that is what the existing code already reads.

## What this does NOT change

Nothing about safety. A hosted model is exactly as untrusted as a local one:
generated SQL still goes through `SQLGuard` and `ReadOnlyRunner`, retrieved
text is still never treated as instructions, and citations are still validated
against the evidence package. The only thing that changes is which machine
runs the matrix multiplications.

## What it does change

Data leaves the machine. With `LLM_PROVIDER=openai`, every prompt - including
retrieved document passages and the database schema - is sent to the endpoint
at `LLM_BASE_URL`. Query *results* reach the prompt only when a caller passes
them, which the orchestrator does not do by default. `README.md` and
`docs/security.md` claim the system is fully local; that claim is false once
this is configured, and the documents say so.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

# Groq rejects temperature == 0 and silently substitutes 1e-8. Sending the
# substitution ourselves keeps the request honest and means the behaviour does
# not depend on one vendor's undocumented coercion.
MIN_TEMPERATURE = 1e-8


class ChatClientError(RuntimeError):
    """Chat generation failed, with enough context to act on."""


class EmbeddingClientError(RuntimeError):
    """Embedding failed, with enough context to act on."""


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class OpenAICompatChatClient:
    """Any OpenAI-compatible chat endpoint: Groq, OpenRouter, vLLM, OpenAI.

    Uses `requests` rather than the `openai` SDK. The surface needed here is
    one POST and an SSE loop; the SDK would add a dependency, a version
    constraint and a second retry policy for no gain.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
        format: str | None = None,
        keep_alive: str | None = None,
        stream: bool = False,
        **_: Any,
    ) -> dict[str, Any] | Iterator[dict[str, Any]]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        payload.update(_translate_options(options))

        if format == "json":
            # The router relies on this: without it a model returns prose
            # around the JSON and the parse fails, and the router then falls
            # back to its heuristic without anyone noticing it stopped using
            # the model at all.
            payload["response_format"] = {"type": "json_object"}

        if stream:
            return self._stream(payload)
        return self._once(payload)

    def _once(self, payload: dict[str, Any]) -> dict[str, Any]:
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers,
                json=payload,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ChatClientError(
                f"Could not reach {self.base_url}: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ChatClientError(_http_error(self.base_url, response))

        body = response.json()
        return _to_ollama_shape(body)

    def _stream(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers,
                json=payload,
                timeout=self.timeout,
                stream=True,
            )
        except Exception as exc:
            raise ChatClientError(
                f"Could not reach {self.base_url}: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ChatClientError(_http_error(self.base_url, response))

        for raw in response.iter_lines(decode_unicode=True):
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue  # keep-alive padding, not a message

            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            yield {
                "message": {"content": delta.get("content") or ""},
                "done": bool(choices and choices[0].get("finish_reason")),
            }


def _translate_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Ollama option names to OpenAI parameter names.

    `num_ctx` has no equivalent and is dropped: context length is a property
    of the hosted deployment, not a per-request knob.
    """
    if not options:
        return {}

    translated: dict[str, Any] = {}
    if "temperature" in options:
        translated["temperature"] = max(float(options["temperature"]), MIN_TEMPERATURE)
    if options.get("num_predict"):
        translated["max_tokens"] = int(options["num_predict"])
    if "top_p" in options:
        translated["top_p"] = options["top_p"]
    if "stop" in options:
        translated["stop"] = options["stop"]
    return translated


def _to_ollama_shape(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices") or []
    content = ""
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""

    usage = body.get("usage") or {}
    return {
        "message": {"role": "assistant", "content": content},
        "done": True,
        # Named as Ollama names them, because that is what the call sites read
        # when they record token counts on a trace.
        "prompt_eval_count": usage.get("prompt_tokens"),
        "eval_count": usage.get("completion_tokens"),
        "model": body.get("model", ""),
    }


def _http_error(base_url: str, response: Any) -> str:
    """A message that says what to fix, not just that something broke."""
    detail = response.text[:300] if response.text else "(no body)"
    hint = {
        401: " Check the API key.",
        403: " The key is valid but not permitted to use this model.",
        404: " Check the model name and the base URL.",
        413: " The prompt is too large for this model's context window.",
        429: " Rate limited or out of quota.",
    }.get(response.status_code, "")
    return f"{base_url} returned {response.status_code}.{hint} {detail}"


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class OpenAICompatEmbeddingClient:
    """OpenAI-compatible `/embeddings`."""

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def embed(self, *, model: str, input: list[str], **_: Any) -> dict[str, Any]:
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/embeddings",
                headers=self._headers,
                json={"model": model, "input": input},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise EmbeddingClientError(
                f"Could not reach {self.base_url}: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise EmbeddingClientError(_http_error(self.base_url, response))

        body = response.json()
        # The API does not guarantee order, and the index depends on it.
        rows = sorted(body.get("data") or [], key=lambda d: d.get("index", 0))
        return {"embeddings": [row["embedding"] for row in rows]}


class CloudflareEmbeddingClient:
    """Cloudflare Workers AI embeddings.

    `@cf/baai/bge-m3` is the model this project is set up for: 1024
    dimensions - the same as the local `qwen3-embedding:0.6b` the index was
    originally built with, so the Qdrant collection's vector size is
    unchanged - and multilingual across 100+ languages, which matters because
    the evaluation's `multilingual` category is where sparse retrieval
    collapses to 0.333.

    The vectors are still different vectors. The index must be rebuilt.
    """

    def __init__(self, account_id: str, api_token: str, timeout: float) -> None:
        self.account_id = account_id
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def embed(self, *, model: str, input: list[str], **_: Any) -> dict[str, Any]:
        import requests

        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/{model}"
        try:
            response = requests.post(
                url, headers=self._headers, json={"text": input}, timeout=self.timeout
            )
        except Exception as exc:
            raise EmbeddingClientError(
                f"Could not reach Workers AI: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise EmbeddingClientError(_http_error("Workers AI", response))

        body = response.json()
        if not body.get("success", True):
            errors = "; ".join(str(e) for e in body.get("errors") or [])
            raise EmbeddingClientError(f"Workers AI reported failure: {errors}")

        return {"embeddings": _extract_cf_vectors(body, model)}


def _extract_cf_vectors(body: dict[str, Any], model: str) -> list[list[float]]:
    """Pull the vectors out, tolerating more than one documented shape.

    Cloudflare publishes the request schema for bge-m3 but not a stable,
    legible response schema, and the bge family has not been uniform: the
    older models return `result.data`, and the newer unified ones have been
    seen returning `result.response` as a list of objects. Rather than guess
    one and fail obscurely, this accepts the shapes that exist and, when it
    recognises none of them, says exactly what it received - which is the
    difference between a five-minute fix and an afternoon.
    """
    result = body.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list) and data and isinstance(data[0], list):
            return data

        response = result.get("response")
        if isinstance(response, list) and response:
            if isinstance(response[0], list):
                return response
            if isinstance(response[0], dict) and "embedding" in response[0]:
                return [row["embedding"] for row in response]

    raise EmbeddingClientError(
        f"Workers AI returned an unrecognised response shape for {model!r}. "
        f"Expected vectors under result.data or result.response; got keys "
        f"{sorted(result) if isinstance(result, dict) else type(result).__name__}."
    )


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def build_chat_client(settings: Settings | None = None, *, timeout: float | None = None) -> Any:
    """The chat client for the configured provider.

    Returns an `ollama.Client` unchanged when the provider is Ollama, so the
    default path is not merely compatible with the old behaviour - it is the
    old behaviour.

    `timeout` overrides the provider default. The intent classifier needs it:
    it runs on the hot path with a 12-second budget, and inheriting the
    300-second generation timeout would turn a screening step into a hang.
    """
    settings = settings or get_settings()
    llm = settings.llm

    if llm.provider == "ollama":
        import ollama

        return ollama.Client(
            settings.ollama.host,
            timeout=timeout if timeout is not None else settings.ollama.timeout_seconds,
        )

    if llm.api_key is None:
        raise ChatClientError(
            f"LLM_PROVIDER={llm.provider} but LLM_API_KEY is not set. "
            "Set it as an environment variable or a container secret, never in a file."
        )
    if not llm.model:
        # The profile's model name is an Ollama tag. Sending "llama3.1:8b" to
        # Groq produces a 404 that reads like a network problem.
        raise ChatClientError(
            "LLM_MODEL must be set for a hosted provider. The model profile's name is an "
            "Ollama tag and is not meaningful to a hosted API."
        )

    log.info("Chat via %s at %s (model %s)", llm.provider, llm.base_url, llm.model)
    return OpenAICompatChatClient(
        base_url=llm.base_url,
        api_key=llm.api_key.get_secret_value(),
        timeout=timeout if timeout is not None else llm.timeout_seconds,
    )


def build_embedding_client(settings: Settings | None = None) -> Any:
    """The embedding client for the configured provider."""
    settings = settings or get_settings()
    embeddings = settings.embeddings

    if embeddings.provider == "ollama":
        import ollama

        return ollama.Client(settings.ollama.host, timeout=settings.ollama.timeout_seconds)

    if not embeddings.model:
        raise EmbeddingClientError(
            "EMBEDDING_MODEL must be set for a hosted provider "
            "(for example @cf/baai/bge-m3 on Cloudflare)."
        )

    if embeddings.provider == "cloudflare":
        if not embeddings.cloudflare_account_id or embeddings.cloudflare_api_token is None:
            raise EmbeddingClientError(
                "EMBEDDING_PROVIDER=cloudflare needs EMBEDDING_CLOUDFLARE_ACCOUNT_ID and "
                "EMBEDDING_CLOUDFLARE_API_TOKEN. The token needs the Workers AI Read "
                "permission."
            )
        log.info("Embeddings via Cloudflare Workers AI (model %s)", embeddings.model)
        return CloudflareEmbeddingClient(
            account_id=embeddings.cloudflare_account_id,
            api_token=embeddings.cloudflare_api_token.get_secret_value(),
            timeout=embeddings.timeout_seconds,
        )

    if embeddings.api_key is None:
        raise EmbeddingClientError(
            f"EMBEDDING_PROVIDER={embeddings.provider} but EMBEDDING_API_KEY is not set."
        )

    log.info(
        "Embeddings via %s at %s (model %s)",
        embeddings.provider,
        embeddings.base_url,
        embeddings.model,
    )
    return OpenAICompatEmbeddingClient(
        base_url=embeddings.base_url,
        api_key=embeddings.api_key.get_secret_value(),
        timeout=embeddings.timeout_seconds,
    )


def describe_providers(settings: Settings | None = None) -> dict[str, str]:
    """A redacted summary for the config page, health check and traces.

    No key is included, and there is no branch that could put one here.
    """
    settings = settings or get_settings()
    return {
        "chat_provider": settings.llm.provider,
        "chat_endpoint": settings.llm.base_url if settings.llm.is_hosted else settings.ollama.host,
        "chat_model": settings.chat_model,
        "chat_key": "set" if settings.llm.api_key else "not set",
        "embedding_provider": settings.embeddings.provider,
        "embedding_model": settings.embedding_model,
        "leaves_machine": "yes"
        if (settings.llm.is_hosted or settings.embeddings.is_hosted)
        else "no",
    }
