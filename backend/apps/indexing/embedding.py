"""Utility helpers for embedding text batches."""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Sequence

import torch
from transformers import AutoModel, AutoTokenizer


@lru_cache(maxsize=4)
def load_encoder(model_name: str) -> tuple:
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name)
    model.eval()
    return tokenizer, model


def embed_texts(model_name: str, texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []

    tokenizer, model = load_encoder(model_name)
    encoded = tokenizer(
        list(texts),
        return_tensors="pt",
        truncation=True,
        padding=True,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    encoded = {k: v.to(device) for k, v in encoded.items()}

    with torch.no_grad():
        outputs = model(**encoded)
        embeddings = outputs.last_hidden_state.mean(dim=1).cpu().tolist()

    return embeddings

