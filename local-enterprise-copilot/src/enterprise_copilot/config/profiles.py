"""
Model profiles.

Hardware varies, so the system ships three profiles instead of one hard-coded
model set. A profile bundles the chat model, the embedding model, the reranker
and the inference settings that suit a class of machine.

Profiles are data, not code: nothing outside this module may hard-code a model
name. Every name is overridable through environment variables, so a profile is
a convenient default, never a constraint.

Sizing notes for the reference machine this was developed on
(AMD Ryzen AI 9 HX 370, 32 GB RAM, RTX 4070 Laptop with 8 GB VRAM):

  lite      ~3 GB VRAM   runs on CPU only if needed; no reranker
  standard  ~6 GB VRAM   fits entirely in 8 GB VRAM alongside the embedder
  high      ~10 GB       exceeds 8 GB VRAM, so it partially offloads to system
                         RAM and runs noticeably slower; choose it for quality
                         comparisons, not for interactive use on this hardware
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ProfileName(StrEnum):
    LITE = "lite"
    STANDARD = "standard"
    HIGH = "high"


class ModelProfile(BaseModel):
    """One coherent set of models and inference settings."""

    model_config = {"frozen": True}

    name: ProfileName
    description: str

    # --- Chat / instruction model (Ollama) ---
    chat_model: str
    chat_context_tokens: int = Field(
        description="num_ctx. Must be large enough for the retrieved evidence "
        "plus the schema subset, or context is silently truncated."
    )
    chat_temperature: float = 0.1
    chat_max_tokens: int = 1024

    # --- Embedding model (Ollama) ---
    # The dimension is deliberately absent: it is detected at runtime by
    # embedding a probe string. Assuming a dimension is a common source of
    # silent index corruption when a model is swapped.
    embedding_model: str
    embedding_batch_size: int = 16

    # --- Reranker (cross-encoder, run locally) ---
    # None means "no reranking": the fused hybrid ranking is used directly.
    reranker_model: str | None = None
    reranker_device: str = "cpu"
    reranker_top_n: int = 30

    # --- Retrieval breadth ---
    # Starting points only. These are tuned against evals/ in Phase 3;
    # the values below are not claimed to be optimal.
    dense_candidates: int = 40
    sparse_candidates: int = 40
    final_evidence_chunks: int = 8

    # --- Approximate resource cost, for the UI profile picker ---
    approx_vram_gb: float
    cpu_only_viable: bool


LITE = ModelProfile(
    name=ProfileName.LITE,
    description=(
        "CPU-friendly. Small chat model, small embedder, no reranker. "
        "Use on machines without a usable GPU or under 16 GB RAM."
    ),
    chat_model="llama3.2:3b",
    chat_context_tokens=8192,
    embedding_model="nomic-embed-text",
    reranker_model=None,
    dense_candidates=25,
    sparse_candidates=25,
    final_evidence_chunks=5,
    approx_vram_gb=3.0,
    cpu_only_viable=True,
)

STANDARD = ModelProfile(
    name=ProfileName.STANDARD,
    description=(
        "Default. 8B chat model with a multilingual embedder and a CPU "
        "cross-encoder reranker. Fits in 8 GB VRAM."
    ),
    chat_model="llama3.1:8b",
    chat_context_tokens=16384,
    embedding_model="qwen3-embedding:0.6b",
    reranker_model="BAAI/bge-reranker-base",
    reranker_device="cpu",
    dense_candidates=40,
    sparse_candidates=40,
    final_evidence_chunks=8,
    approx_vram_gb=6.0,
    cpu_only_viable=False,
)

HIGH = ModelProfile(
    name=ProfileName.HIGH,
    description=(
        "Quality-first. 14B chat model and the full multilingual reranker. "
        "Exceeds 8 GB VRAM and will partially offload to system RAM."
    ),
    chat_model="qwen2.5:14b-instruct",
    chat_context_tokens=24576,
    embedding_model="qwen3-embedding:0.6b",
    reranker_model="BAAI/bge-reranker-v2-m3",
    reranker_device="cpu",
    dense_candidates=60,
    sparse_candidates=60,
    final_evidence_chunks=10,
    approx_vram_gb=10.0,
    cpu_only_viable=False,
)

PROFILES: dict[ProfileName, ModelProfile] = {
    ProfileName.LITE: LITE,
    ProfileName.STANDARD: STANDARD,
    ProfileName.HIGH: HIGH,
}


def get_profile(name: ProfileName | str) -> ModelProfile:
    """Look up a profile by name, raising a clear error for a bad value."""
    try:
        return PROFILES[ProfileName(name)]
    except ValueError as exc:
        valid = ", ".join(p.value for p in ProfileName)
        raise ValueError(f"Unknown model profile {name!r}. Valid profiles: {valid}") from exc


def recommend_profile(total_ram_gb: float, vram_gb: float) -> ProfileName:
    """Suggest a profile from detected hardware.

    Only a suggestion: the operator's explicit COPILOT_PROFILE always wins.
    """
    if vram_gb >= 12:
        return ProfileName.HIGH
    if vram_gb >= 6 and total_ram_gb >= 16:
        return ProfileName.STANDARD
    return ProfileName.LITE
