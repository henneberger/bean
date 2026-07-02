"""Embeddings via fastembed (ONNX, CPU-only, no torch).

The model is chosen by config (`embedding.model`), never an environment variable, and its
weights download automatically the first time an embedding is actually computed — on the first
`sync`/`search`/`reembed`, not at setup. Models are cached per name, so `bean reembed` can move
the index onto a different model without a process restart. Everything downstream takes an
injectable embed function, so tests run with a deterministic fake and never touch a model."""

from __future__ import annotations

_models: dict = {}


def _load(model_name: str):
    if model_name not in _models:
        from fastembed import TextEmbedding  # lazy: import + weight download only when embedding
        _models[model_name] = TextEmbedding(model_name=model_name)
    return _models[model_name]


def embedder(model_name: str, batch_size: int = 64):
    """A (texts -> vectors) callable bound to one model — what sync/reembed pass around."""
    def embed(texts: list[str]) -> list[list[float]]:
        model = _load(model_name)
        return [list(map(float, v)) for v in model.embed(texts, batch_size=batch_size)]
    return embed


def embed_query(text: str, model_name: str) -> list[float]:
    """Query-side embedding (uses the model's query prefix when it has one)."""
    model = _load(model_name)
    if hasattr(model, "query_embed"):
        return list(map(float, next(iter(model.query_embed([text])))))
    return [list(map(float, v)) for v in model.embed([text])][0]
