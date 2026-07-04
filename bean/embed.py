"""Embeddings — one built-in model, plus a code-plugin hook. Resolved from `settings["embedding"]`:

  - the BUILT-IN (default): Qwen/Qwen3-Embedding-0.6B, run in-process as a quantized GGUF via
    llama-cpp-python. Fully local, CPU-friendly, no API. There is no backend/model selection and no
    fallback — if the model can't load, bean fails loudly rather than silently degrading.
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
MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
_GGUF_REPO = "Qwen/Qwen3-Embedding-0.6B-GGUF"
_GGUF_FILE = "Qwen3-Embedding-0.6B-Q8_0.gguf"
# Qwen3-Embedding is instruction-tuned on the query side: queries get a task instruction, documents
# are embedded raw. Matching that asymmetry is what the model was trained for.
_QUERY_INSTRUCT = ("Instruct: Given a search query, retrieve relevant passages that answer it\n"
                   "Query: ")

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
            _cache[key] = _Qwen3()
    return _cache[key]


def _reject_legacy(emb: dict) -> None:
    """Fail loudly on stale `backend`/`model` config instead of silently ignoring it. The built-in
    is Qwen3-only now; anything else goes through `embedding.plugin`."""
    stale = [k for k in ("backend", "model") if emb.get(k)]
    if stale:
        raise RuntimeError(
            f"embedding.{'/'.join(stale)} is set but no longer supported — the built-in embedder is "
            f"fixed to {MODEL_ID}. Remove those keys, or point `embedding.plugin` at your own "
            "embedder module (see bean/embed.py). Then run `bean sync --rebuild`.")


class _Qwen3:
    """Qwen3-Embedding-0.6B as a quantized GGUF, run in-process via llama-cpp-python."""
    def __init__(self):
        try:
            from llama_cpp import Llama
        except ImportError as err:
            raise RuntimeError("the built-in embedder needs `pip install llama-cpp-python`") from err
        self.model = Llama.from_pretrained(
            repo_id=_GGUF_REPO, filename=_GGUF_FILE, embedding=True, n_ctx=2048, verbose=False)

    def embed(self, texts, batch):
        return [list(map(float, v)) for v in self.model.embed(list(texts))]

    def query(self, text):
        return self.embed([_QUERY_INSTRUCT + text], 1)[0]


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
