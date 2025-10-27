"""Chunk generation for bundle content."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from .bundler import BundleSpec
from .tokenization import TokenizerProtocol


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChunkSpec:
    sequence: int
    bundle_sequence: int
    text: str
    token_count: int
    char_start: int
    char_end: int


def split_bundle(
    bundle: BundleSpec,
    *,
    tokenizer: TokenizerProtocol,
    chunk_size: int,
    overlap: int,
    max_tokens: int | None = None,
) -> list[ChunkSpec]:
    """Split bundle text into overlapping token slices."""

    if not bundle.text:
        return []

    tokens = tokenizer.encode(bundle.text, add_special_tokens=False)
    if not tokens:
        return []

    if max_tokens and chunk_size > max_tokens:
        logger.warning(
            "Requested chunk_size %s exceeds encoder window %s for bundle seq=%s; "
            "chunks may be truncated or exceed limits.",
            chunk_size,
            max_tokens,
            bundle.sequence,
        )

    effective_overlap = max(0, min(overlap, chunk_size // 2))
    step = max(1, chunk_size - effective_overlap)

    chunk_specs: list[ChunkSpec] = []
    seq = 0
    pointer = 0

    for start in range(0, len(tokens), step):
        end = min(start + chunk_size, len(tokens))
        token_slice = tokens[start:end]
        text = tokenizer.decode(token_slice, skip_special_tokens=True).strip()
        if not text:
            continue

        char_start = bundle.text.find(text, pointer)
        if char_start == -1:
            char_start = pointer
        char_end = char_start + len(text)
        pointer = char_end

        seq += 1
        if max_tokens and len(token_slice) > max_tokens:
            logger.error(
                "Chunk seq=%s (bundle seq=%s) exceeds encoder window %s "
                "(tokens=%s, chunk_size=%s, overlap=%s)",
                seq,
                bundle.sequence,
                max_tokens,
                len(token_slice),
                chunk_size,
                overlap,
            )

        chunk_specs.append(
            ChunkSpec(
                sequence=seq,
                bundle_sequence=bundle.sequence,
                text=text,
                token_count=len(token_slice),
                char_start=char_start,
                char_end=char_end,
            )
        )

        if end == len(tokens):
            break
    # logger.info(chunk_specs)
    # for spec in chunk_specs:
    #     logger.info(f"Chunk created: {spec.token_count}")
    return chunk_specs
