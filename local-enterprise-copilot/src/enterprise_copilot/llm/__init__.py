"""Chat and embedding clients, behind one Ollama-shaped interface."""

from .clients import (
    ChatClientError,
    EmbeddingClientError,
    RateLimitedError,
    build_chat_client,
    build_embedding_client,
    describe_providers,
)

__all__ = [
    "ChatClientError",
    "EmbeddingClientError",
    "RateLimitedError",
    "build_chat_client",
    "build_embedding_client",
    "describe_providers",
]
