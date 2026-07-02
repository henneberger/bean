"""Optional local cross-encoder reranker — a quality pass over the fused top-N before top-k. Uses
fastembed's `TextCrossEncoder` (the same offline, no-API stack as the embedder), so there is no
Cohere/hosted dependency. Off by default (`search.rerank.enabled`); the model downloads on first use
and is cached process-wide. `search()` takes an injectable `rerank_fn` so tests never load a model."""

from __future__ import annotations

_cache: dict = {}


def reranker(model_name: str):
    """Return `rerank(query, texts) -> list[float]` (higher = more relevant), lazily loading the
    cross-encoder once per model."""
    def rerank(query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        ce = _cache.get(model_name)
        if ce is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            ce = _cache[model_name] = TextCrossEncoder(model_name=model_name)
        return list(ce.rerank(query, texts))
    return rerank
