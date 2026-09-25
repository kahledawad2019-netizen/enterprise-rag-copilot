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
import re
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


class RateLimitedError(ChatClientError):
    """The provider answered 429. Another model may still have quota."""


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

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        *,
        fallback_models: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Tried in order when a model is rate limited. On Groq's free tier
        # every model has its own tokens-per-minute budget (8k at the time of
        # writing), and one grounded SQL answer can use most of it, so a
        # second model roughly doubles how many visitors the demo can serve.
        self.fallback_models = tuple(fallback_models)
        # The model that actually answered the most recent call, which after
        # a 429 is not the one that was asked for. Recorded on the Answer.
        self.last_model: str | None = None
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

        models = [model, *(m for m in self.fallback_models if m != model)]
        if stream:
            return self._stream_with_fallback(payload, models)
        for index, name in enumerate(models):
            try:
                response = self._once({**payload, "model": name})
                self.last_model = name
                return response
            except RateLimitedError:
                if index == len(models) - 1:
                    raise
                log.warning("%s is rate limited; retrying with %s", name, models[index + 1])
        raise AssertionError("unreachable")  # pragma: no cover

    def _stream_with_fallback(
        self, payload: dict[str, Any], models: list[str]
    ) -> Iterator[dict[str, Any]]:
        """Fall back only before the first chunk: switching model mid-answer
        would splice two different answers together."""
        for index, name in enumerate(models):
            parts = self._stream({**payload, "model": name})
            try:
                first = next(parts)
            except StopIteration:
                return
            except RateLimitedError:
                if index == len(models) - 1:
                    raise
                log.warning("%s is rate limited; retrying with %s", name, models[index + 1])
                continue
            self.last_model = name
            yield first
            yield from parts
            return

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
            _raise_http(self.base_url, response)

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
            _raise_http(self.base_url, response)

        visible = ReasoningFilter()
        finished = False
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    finished = True
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue  # keep-alive padding, not a message

                if chunk.get("error"):
                    # An error mid-stream arrives as an ordinary data event on
                    # an HTTP 200. Ignoring it would end the answer silently.
                    message = (chunk["error"] or {}).get("message", "stream error")
                    raise ChatClientError(f"{self.base_url} reported an error mid-stream: {message}")

                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                done = bool(choices and choices[0].get("finish_reason"))
                finished = finished or done
                yield {"message": {"content": visible.feed(delta.get("content") or "")}, "done": done}
        finally:
            response.close()

        if not finished:
            raise ChatClientError(f"{self.base_url} closed the stream before the answer finished.")


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
        "message": {"role": "assistant", "content": strip_reasoning(content)},
        "done": True,
        # Named as Ollama names them, because that is what the call sites read
        # when they record token counts on a trace.
        "prompt_eval_count": usage.get("prompt_tokens"),
        "eval_count": usage.get("completion_tokens"),
        "model": body.get("model", ""),
    }


def _raise_http(base_url: str, response: Any) -> None:
    message = _http_error(base_url, response)
    if response.status_code == 429:
        raise RateLimitedError(message)
    raise ChatClientError(message)


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


class FastEmbedClient:
    """In-process ONNX embeddings via fastembed. No server, no key, CPU only.

    This is the cloud default: a free container has no GPU and no Ollama, and
    a hosted embedding API would add a second credential. The model is
    downloaded once (at image build time in the Dockerfile) and cached.
    """

    def __init__(self, model: str, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding

        self.model = model
        self._model = TextEmbedding(model_name=model, cache_dir=cache_dir)

    # One text per ONNX run. ONNX Runtime's CPU arena keeps its peak
    # allocation for the life of the process, and a padded batch's peak grows
    # with batch size: embedding the 25-table schema catalog measured +258 MB
    # RSS at batch 32 and +17 MB at batch 1 - the difference between fitting
    # a 512 MB free container and not. CPU inference gains little from
    # batching, so throughput barely changes.
    batch_size = 1

    def embed(self, *, model: str, input: list[str], **_: Any) -> dict[str, Any]:
        try:
            vectors = [v.tolist() for v in self._model.embed(input, batch_size=self.batch_size)]
        except Exception as exc:
            raise EmbeddingClientError(
                f"fastembed failed for {self.model}: {type(exc).__name__}: {exc}"
            ) from exc
        return {"embeddings": vectors}


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


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)

# Installed-model preference for the local chat default, best first. Instruct
# (non-reasoning) models come first: on a CPU a reasoning model spends most of
# its budget thinking, and the router and answerer each pay that cost.
PREFERRED_OLLAMA_CHAT_MODELS: tuple[str, ...] = (
    "qwen3:4b-instruct-2507-q4_K_M",
    "qwen3:4b-instruct",
    "qwen2.5:7b-instruct",
    "llama3.1:8b",
    "qwen2.5:3b-instruct",
    "llama3.2:3b",
    "qwen3:4b",
    "qwen3:8b",
)


def strip_reasoning(text: str) -> str:
    """Remove `<think>...</think>` blocks, including an unterminated one."""
    return _THINK_BLOCK.sub("", text).strip()


class ReasoningFilter:
    """Drops `<think>...</think>` from a token stream whose tags may be split
    across chunks. Feed each chunk; get back the visible text."""

    def __init__(self) -> None:
        self._in_think = False
        self._pending = ""

    def feed(self, chunk: str) -> str:
        text = self._pending + chunk
        self._pending = ""
        out: list[str] = []
        while text:
            tag = "</think>" if self._in_think else "<think>"
            index = text.lower().find(tag)
            if index >= 0:
                if not self._in_think:
                    out.append(text[:index])
                text = text[index + len(tag) :]
                self._in_think = not self._in_think
                continue
            # Hold back a possible partial tag at the end of the chunk.
            keep = next(
                (n for n in range(len(tag) - 1, 0, -1) if text.lower().endswith(tag[:n])), 0
            )
            if not self._in_think:
                out.append(text[: len(text) - keep])
            self._pending = text[len(text) - keep :]
            text = ""
        return "".join(out)


class OllamaChatClient:
    """`ollama.Client` with reasoning models handled in one place.

    Reasoning models (qwen3 thinking builds, deepseek-r1, gpt-oss) behave
    differently from instruct models in three ways that each broke a call site:

    * Some builds cannot stop thinking. `think=False` on qwen3:4b-thinking
      makes the reasoning land *in the answer content* instead of disappearing.
      So `think` is never sent: Ollama's default for such a model already puts
      the reasoning on its own channel, which is discarded. (Ollama reports
      "thinking" for instruct builds too, whose default is not to think, so
      forcing `think=True` would be wrong for them.)
    * The reasoning counts against `num_predict`. With the answer budget of an
      instruct model, the model can finish thinking with no tokens left and
      return an empty answer, which downstream reads as "no evidence".
      The budget is raised by `thinking_budget` for these models.
    * An empty answer is raised as an error rather than returned.

    Everything else is delegated to the wrapped client unchanged.
    """

    def __init__(self, host: str, timeout: float, *, thinking_budget: int = 3072) -> None:
        import ollama

        self._client = ollama.Client(host, timeout=timeout)
        self.host = host
        self.thinking_budget = thinking_budget
        self._capabilities: dict[str, frozenset[str]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def is_thinking_model(self, model: str) -> bool:
        if model not in self._capabilities:
            try:
                caps = self._client.show(model).get("capabilities") or []
            except Exception:  # an unreachable server surfaces on chat()
                caps = []
            self._capabilities[model] = frozenset(caps)
        return "thinking" in self._capabilities[model]

    def chat(self, *, model: str, stream: bool = False, **kwargs: Any) -> Any:
        if self.is_thinking_model(model):
            options = dict(kwargs.get("options") or {})
            if options.get("num_predict"):
                options["num_predict"] = int(options["num_predict"]) + self.thinking_budget
            kwargs["options"] = options

        if stream:
            return self._stream(model, kwargs)

        response = self._client.chat(model=model, **kwargs)
        content = strip_reasoning(response["message"].get("content") or "")
        if not content:
            raise ChatClientError(
                f"{model} returned no answer text (its token budget was spent reasoning). "
                "Use an instruct model or raise the profile's chat_max_tokens."
            )
        response["message"]["content"] = content
        return response

    def _stream(self, model: str, kwargs: dict[str, Any]) -> Iterator[dict[str, Any]]:
        visible = ReasoningFilter()
        produced = False
        for part in self._client.chat(model=model, stream=True, **kwargs):
            content = visible.feed(part["message"].get("content") or "")
            produced = produced or bool(content.strip())
            yield {"message": {"content": content}, "done": bool(part.get("done"))}
        if not produced:
            # Same rule as the batch path: a stream that was all reasoning is
            # a failure, not an empty answer.
            raise ChatClientError(
                f"{model} returned no answer text (its token budget was spent reasoning)."
            )


def list_ollama_models(host: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Installed models with their capabilities. Raises if Ollama is unreachable."""
    import ollama

    client = ollama.Client(host, timeout=timeout)
    models = []
    for entry in client.list().get("models", []):
        name = entry.get("model", "")
        try:
            caps = list(client.show(name).get("capabilities") or [])
        except Exception:
            caps = []
        models.append({"name": name, "capabilities": caps, "size": entry.get("size")})
    return models


def pick_ollama_chat_model(installed: list[dict[str, Any]], configured: str | None) -> str | None:
    """The configured model if installed, else the best installed chat model."""
    names = {m["name"] for m in installed}
    chat_capable = [m["name"] for m in installed if "completion" in m["capabilities"]]

    def present(name: str) -> str | None:
        if name in names:
            return name
        if f"{name}:latest" in names:
            return f"{name}:latest"
        return None

    if configured and (found := present(configured)):
        return found
    for candidate in PREFERRED_OLLAMA_CHAT_MODELS:
        if found := present(candidate):
            return found
    return chat_capable[0] if chat_capable else None


def build_chat_client(settings: Settings | None = None, *, timeout: float | None = None) -> Any:
    """The chat client for the configured provider.

    Returns an `OllamaChatClient` - the Ollama client with reasoning-model
    handling - when the provider is Ollama, so every call site keeps using
    Ollama's method signatures and return shapes.

    `timeout` overrides the provider default. The intent classifier needs it:
    it runs on the hot path with a 12-second budget, and inheriting the
    300-second generation timeout would turn a screening step into a hang.
    """
    settings = settings or get_settings()
    llm = settings.llm

    if llm.provider == "ollama":
        return OllamaChatClient(
            settings.ollama.host,
            timeout=timeout if timeout is not None else settings.ollama.timeout_seconds,
        )

    if llm.api_key is None:
        raise ChatClientError(
            f"LLM_PROVIDER={llm.provider} but no API key is set (GROQ_API_KEY or LLM_API_KEY). "
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
        fallback_models=llm.fallback_model_list,
    )


def build_embedding_client(settings: Settings | None = None) -> Any:
    """The embedding client for the configured provider."""
    settings = settings or get_settings()
    embeddings = settings.embeddings

    if embeddings.provider == "ollama":
        import ollama

        return ollama.Client(settings.ollama.host, timeout=settings.ollama.timeout_seconds)

    if embeddings.provider == "fastembed":
        model = embeddings.model or "nomic-ai/nomic-embed-text-v1.5-Q"
        log.info("Embeddings in-process via fastembed (model %s)", model)
        return FastEmbedClient(model, cache_dir=embeddings.cache_dir or None)

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
