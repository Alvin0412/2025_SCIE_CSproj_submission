"""Utilities for deriving IndexProfile defaults from HuggingFace models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from transformers import AutoConfig


@dataclass(frozen=True)
class HFProfileDefaults:
    """Default indexing parameters inferred from a HuggingFace model config."""

    dimension: int
    max_input_tokens: int
    chunk_size: int
    chunk_overlap: int
    target_bundle_tokens: int
    encoder_id: str
    tokenizer_id: str


def build_profile_defaults(model_id: str) -> HFProfileDefaults:
    """Load the HuggingFace config and derive indexing profile defaults."""

    config = AutoConfig.from_pretrained(model_id, trust_remote_code=False)

    dimension = _extract_dimension(config)
    if dimension is None or dimension <= 0:
        raise ValueError("Could not determine embedding dimension from model config.")

    max_input_tokens = _extract_max_length(config) or 2048
    if max_input_tokens <= 0:
        max_input_tokens = 2048

    chunk_size = min(512, max_input_tokens)
    if chunk_size <= 0:
        chunk_size = max(32, max_input_tokens)

    chunk_overlap = min(64, max(0, chunk_size // 4))
    target_bundle_tokens = min(max_input_tokens, max(600, chunk_size * 2))

    return HFProfileDefaults(
        dimension=dimension,
        max_input_tokens=max_input_tokens,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        target_bundle_tokens=target_bundle_tokens,
        encoder_id=model_id,
        tokenizer_id=model_id,
    )


def _extract_dimension(config: Any) -> int | None:
    """Try to determine embedding dimension from several common config fields."""

    dimension = _first_int_attr(
        config,
        [
            "hidden_size",
            "d_model",
            "projection_dim",
            "text_embed_dim",
            "word_embed_proj_dim",
            "embed_dim",
        ],
    )
    if dimension:
        return dimension

    text_config = getattr(config, "text_config", None)
    if text_config:
        return _extract_dimension(text_config)

    return None


def _extract_max_length(config: Any) -> int | None:
    """Return the maximum supported sequence length, if available."""

    max_length = _first_int_attr(
        config,
        [
            "max_position_embeddings",
            "max_sequence_length",
            "max_seq_len",
            "n_positions",
            "context_length",
        ],
    )
    if max_length:
        return max_length

    text_config = getattr(config, "text_config", None)
    if text_config:
        return _extract_max_length(text_config)

    return None


def _first_int_attr(config: Any, attributes: list[str]) -> int | None:
    """Return the first positive integer attribute found on the config."""

    for attr in attributes:
        value = getattr(config, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    return None
