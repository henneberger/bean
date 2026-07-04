"""Embeddings — one built-in model, plus a code-plugin hook. Resolved from `settings["embedding"]`:

  - the BUILT-IN (default): jinaai/jina-embeddings-v5-text-nano, run in-process via
    sentence-transformers. It's a task-aware retrieval model, so queries and documents are encoded
    with different prompts (query vs document) — matching what it was trained for. There is no
    backend/model selection and no fallback: if it can't load, bean fails loudly rather than silently
    degrading.
  - a PLUGIN: set `embedding.plugin` to a .py path (or import path) exposing
    `embed(texts) -> list[list[float]]` and optionally `embed_query(text) -> list[float]`. This is
    how you swap in any other model — a static config value, never an environment variable. Most
    models need their own code; the plugin just has to return vectors.

Everything downstream takes an injectable embed function, so tests run with a deterministic fake and
never load a model. `identity(emb)` names the active embedder so `status` can warn when the index was
built with a different one (a `bean sync --rebuild` re-embeds onto the new vectors)."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

# The single built-in embedder. Other models go through `embedding.plugin`, not a config switch.
MODEL_ID = "jinaai/jina-embeddings-v5-text-nano"

_cache: dict = {}


def identity(emb: dict) -> str:
    """A stable name for the configured embedder, stored with the index so a change is detectable."""
    if emb.get("plugin"):
        return f"plugin:{emb['plugin']}"
    return MODEL_ID


def embedder(emb: dict):
    """(texts -> list[list[float]]) for the configured embedder — what sync passes around."""
    backend = _resolve(emb)
    batch = int(emb.get("batch_size", 64))
    return lambda texts: backend.embed(list(texts), batch)


def query_embedder(emb: dict):
    """(text -> list[float]) for the query side (uses the model's query prefix where it has one)."""
    return _resolve(emb).query


def _resolve(emb: dict):
    plugin = emb.get("plugin")
    key = ("plugin", plugin) if plugin else ("builtin",)
    if key not in _cache:
        if plugin:
            _cache[key] = _Plugin(plugin)
        else:
            _reject_legacy(emb)
            _cache[key] = _Jina()
    return _cache[key]


def _reject_legacy(emb: dict) -> None:
    """Fail loudly on stale `backend`/`model` config instead of silently ignoring it. There is one
    built-in model now; anything else goes through `embedding.plugin`."""
    stale = [k for k in ("backend", "model") if emb.get(k)]
    if stale:
        raise RuntimeError(
            f"embedding.{'/'.join(stale)} is set but no longer supported — the built-in embedder is "
            f"fixed to {MODEL_ID}. Remove those keys, or point `embedding.plugin` at your own "
            "embedder module (see bean/embed.py). Then run `bean sync --rebuild`.")


class _Jina:
    """jina-embeddings-v5-text-nano via sentence-transformers. Task-aware: documents and queries are
    encoded with the model's `retrieval` document/query prompts respectively."""
    def __init__(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as err:
            raise RuntimeError(
                "the built-in embedder needs `pip install sentence-transformers`") from err
        # CPU-friendly defaults: no flash-attention / bf16 (those are the model card's GPU tips).
        self.model = SentenceTransformer(MODEL_ID, trust_remote_code=True)

    def _encode(self, texts, prompt_name, batch):
        vecs = self.model.encode(sentences=list(texts), task="retrieval", prompt_name=prompt_name,
                                 batch_size=batch, convert_to_numpy=True, normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]

    def embed(self, texts, batch):
        return self._encode(texts, "document", batch)

    def query(self, text):
        return self._encode([text], "query", 1)[0]


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
