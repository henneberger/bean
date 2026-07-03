"""Embeddings — pluggable, because most models need their own code and all bean wants back is a
vector. Three ways to embed, resolved from `settings["embedding"]`:

  - backend "model2vec" (DEFAULT): a static token-lookup embedder (minishlab/potion-*). No
    transformer forward pass, so it's ~100x faster on CPU than an ONNX transformer — the right
    trade for a speed-first hybrid retriever where keyword fusion covers exact matches.
  - backend "fastembed": an ONNX transformer (e.g. BAAI/bge-small-en-v1.5) for higher accuracy.
  - a PLUGIN: set `embedding.plugin` to a .py path (or import path) exposing
    `embed(texts) -> list[list[float]]` and optionally `embed_query(text) -> list[float]`. This
    overrides backend/model, so any library / API / custom model works as long as it returns vectors.

Everything downstream takes an injectable embed function, so tests run with a deterministic fake and
never load a model. `identity(emb)` names the active embedder so `status` can warn when the index was
built with a different one (a `bean sync --rebuild` re-embeds onto the new vectors)."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

_cache: dict = {}


def identity(emb: dict) -> str:
    """A stable name for the configured embedder, stored with the index so a change is detectable."""
    if emb.get("plugin"):
        return f"plugin:{emb['plugin']}"
    return f"{emb.get('backend', 'model2vec')}:{emb.get('model', '')}"


def embedder(emb: dict):
    """(texts -> list[list[float]]) for the configured embedder — what sync passes around."""
    backend = _resolve(emb)
    batch = int(emb.get("batch_size", 64))
    return lambda texts: backend.embed(list(texts), batch)


def query_embedder(emb: dict):
    """(text -> list[float]) for the query side (uses the model's query prefix where it has one)."""
    return _resolve(emb).query


def _resolve(emb: dict):
    key = (emb.get("plugin"), emb.get("backend"), emb.get("model"))
    if key not in _cache:
        if emb.get("plugin"):
            _cache[key] = _Plugin(emb["plugin"])
        else:
            backend = (emb.get("backend") or "model2vec").lower()
            model = emb.get("model") or _DEFAULT_MODEL.get(backend, "")
            if backend == "model2vec":
                _cache[key] = _Model2Vec(model)
            elif backend == "fastembed":
                _cache[key] = _Fastembed(model)
            else:
                raise RuntimeError(
                    f"unknown embedding backend {backend!r} — use 'model2vec', 'fastembed', "
                    "or point embedding.plugin at a module that returns vectors.")
    return _cache[key]


_DEFAULT_MODEL = {
    "model2vec": "minishlab/potion-retrieval-32M",
    "fastembed": "BAAI/bge-small-en-v1.5",
}


class _Model2Vec:
    def __init__(self, name: str):
        try:
            from model2vec import StaticModel
        except ImportError as err:  # base dependency, but keep the message actionable
            raise RuntimeError("the model2vec embedder needs `pip install model2vec`") from err
        self.model = StaticModel.from_pretrained(name)

    def embed(self, texts, batch):
        return [list(map(float, v)) for v in self.model.encode(texts)]

    def query(self, text):
        return self.embed([text], 1)[0]


class _Fastembed:
    def __init__(self, name: str):
        try:
            from fastembed import TextEmbedding
        except ImportError as err:
            raise RuntimeError("the fastembed backend needs `pip install fastembed`") from err
        self.model = TextEmbedding(model_name=name)

    def embed(self, texts, batch):
        return [list(map(float, v)) for v in self.model.embed(texts, batch_size=batch)]

    def query(self, text):
        if hasattr(self.model, "query_embed"):
            return list(map(float, next(iter(self.model.query_embed([text])))))
        return self.embed([text], 1)[0]


class _Plugin:
    """A user embedder: a .py path or import path exposing embed(texts) [+ optional embed_query]."""
    def __init__(self, ref: str):
        mod = _load_module(ref)
        if not hasattr(mod, "embed"):
            raise RuntimeError(
                f"embedding plugin {ref!r} must define embed(texts) -> list[list[float]]")
        self._embed = mod.embed
        self._query = getattr(mod, "embed_query", None)

    def embed(self, texts, batch):
        return [list(map(float, v)) for v in self._embed(texts)]

    def query(self, text):
        if self._query:
            return list(map(float, self._query(text)))
        return self.embed([text], 1)[0]


def _load_module(ref: str):
    p = Path(ref).expanduser()
    if p.suffix == ".py" or p.exists():
        spec = importlib.util.spec_from_file_location(f"bean_embedder_{p.stem}", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    return importlib.import_module(ref)
