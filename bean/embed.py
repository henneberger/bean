"""Embeddings via fastembed (ONNX, CPU-only, no torch). The model downloads once (~100 MB)
and is cached by fastembed. Everything downstream takes an injectable embed function, so
tests run with a deterministic fake and never touch the model."""

from __future__ import annotations

import os

DEFAULT_MODEL = os.environ.get("BEAN_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

_model = None


def _load():
    global _model
    if _model is None:
        from fastembed import TextEmbedding  # lazy: import cost + download only when embedding
        _model = TextEmbedding(model_name=DEFAULT_MODEL)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Document-side embeddings."""
    return [list(map(float, v)) for v in _load().embed(texts)]


def embed_query(text: str) -> list[float]:
    """Query-side embedding (uses the model's query prefix when it has one)."""
    model = _load()
    if hasattr(model, "query_embed"):
        return list(map(float, next(iter(model.query_embed([text])))))
    return embed_texts([text])[0]
