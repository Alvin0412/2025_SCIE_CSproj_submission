"""Tokenizer helpers shared across indexing tasks."""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from transformers import AutoTokenizer


class TokenizerProtocol(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool = ...) -> list[int]:
        ...

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool = ...) -> str:
        ...


@lru_cache(maxsize=8)
def get_tokenizer(name: str) -> TokenizerProtocol:
    """Load and cache a tokenizer by name."""

    return AutoTokenizer.from_pretrained(name, use_fast=True)


def count_tokens(text: str, tokenizer: TokenizerProtocol) -> int:
    if not text:
        return 0

    prev_verbose = getattr(tokenizer, "verbose", True)  # disable any warming here
    tokenizer.verbose = False
    try:
        return len(tokenizer.encode(text, add_special_tokens=False))
    finally:
        tokenizer.verbose = prev_verbose
