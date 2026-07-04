"""Resolved configuration — everything is a config value, nothing is an environment variable.

Three layers, deep-merged in order (later wins):

  1. DEFAULTS          — the values baked in here.
  2. global config     — ~/.bean/config.json (per user; embedding model, chunking, OCR…).
  3. workspace settings — the "settings" block of a repo's config.json (per-repo overrides).

`resolve(ws)` returns the merged dict. `get(cfg, "embedding.plugin")` and the `bean config`
CLI walk it by dotted path so any leaf is reachable without special-casing. Secrets never live
here — tokens stay in ~/.bean/credentials/ (mode 0600)."""

from __future__ import annotations

import copy
import json

from .workspace import bean_home

DEFAULTS: dict = {
    "embedding": {
        # How to turn text into vectors. The one built-in embedder is jinaai/jina-embeddings-v5-
        # text-nano, run in-process via sentence-transformers — fully local, no API. There is no
        # backend/model switch and no fallback: if it can't load, bean fails loudly.
        # Changing embedders needs a `bean sync --rebuild`; `bean status` warns when the index was
        # built with a different one.
        "batch_size": 64,
        # To use any other model, point `plugin` at your own code: a .py path (or import path)
        # exposing `embed(texts) -> list[list[float]]` (+ optional `embed_query(text)`). A static
        # config value, never an environment variable. null = the built-in jina embedder.
        "plugin": None,
    },
    # Global chunking defaults. Any source may override the whole block (or any leaf) with a
    # `chunking` sub-block in its own config (see `slack` below) — `chunking_for(cfg, source)`
    # merges the source's overrides over these. Changing chunking needs a `bean sync --rebuild`.
    "chunking": {
        "lines": 40,        # window height, in lines
        "overlap": 8,       # lines shared between adjacent windows
        "max_chars": 2000,  # hard cap on a chunk's characters
        "min_chars": 40,    # windows shorter than this are dropped
        # Prepend the document title to each chunk's *embedded* text (stripped from the stored/
        # displayed text) so short or mid-document chunks still carry what the doc is about — a
        # cheap recall win. Toggling needs a `bean sync --rebuild`.
        "title_prefix": True,
        # Also embed a coarse doc-level "large chunk" per `large_chunk_ratio` base chunks, so broad
        # "which doc is about X" questions match at a section granularity. Vector-only (kept out of
        # the keyword/neighbour mirror). Needs a `bean sync --rebuild`.
        "large_chunks": False,
        "large_chunk_ratio": 4,
    },
    "search": {
        "hybrid": True,      # fuse vector + keyword; False = vector only
        "k": 8,
        "rrf_k": 60,         # reciprocal-rank-fusion constant
        "keyword_pool": 200, # keyword candidates fused with the vector candidates
        "expand": 1,         # neighbouring chunks pulled in around each hit (0 = off)
        # Fusion weights. Each ranking list contributes weight/(rrf_k+rank). Multiple query
        # variants (the assistant can pass paraphrases + extracted identifiers) fuse the same way.
        "vector_weight": 1.0,
        "keyword_weight": 1.0,
        # Query-type routing: nudge the weights per query — identifier/quoted/error-string queries
        # lean keyword, natural-language questions lean vector. False = use the fixed weights above.
        "auto_weight": True,
        # Recency: multiply a hit's fused score by max(1/(1 + recency_decay*age_years),
        # recency_floor), from the document's own modified_at. 0.0 = off (no recency bias).
        "recency_decay": 0.0,
        "recency_floor": 0.75,
        # Coalesce adjacent hits from the same document into one section (union of line ranges,
        # deduped) instead of returning three overlapping chunks as three hits.
        "merge_sections": True,
        # Optional local cross-encoder reranker over the fused top-`pool` before taking top-k. Off
        # by default (downloads a model on first use); no external API — a fastembed cross-encoder.
        "rerank": {
            "enabled": False,
            "model": "Xenova/ms-marco-MiniLM-L-6-v2",
            "pool": 40,
        },
    },
    "graph": {
        # Build a lightweight edge index during sync (authored_by, in-container, links-to) from the
        # metadata connectors already carry — powers `bean related <doc>` and metadata filters.
        # No LLM extraction; purely derived from source-native fields. False = don't build it.
        "enabled": True,
    },
    "ocr": {
        # Backend for PDF text. "auto" tries native text first, OCR only for image-only pages.
        # "unlimited-ocr" forces the baidu/Unlimited-OCR model; "text" never OCRs.
        "backend": "auto",
        "model": "baidu/Unlimited-OCR",
        "dpi": 200,
        # On first OCR, bean installs the heavy toolchain (torch, transformers, pillow) into its own
        # venv — like the embedding model, you never pip it by hand. false = forbid that runtime pip
        # (locked-down/offline venvs); then OCR errors and asks you to pre-install `bean[ocr]`.
        "auto_install": True,
    },
    "sync": {
        # Warn (never auto-run) when the index hasn't been synced in this many days, so an assistant
        # can nudge the user to run `bean sync`. 0 = never warn.
        "stale_days": 7,
    },
    # Lookback = the INITIAL backfill, chosen once at setup: how far back a source reaches on its
    # first sync. After that each source tracks the last message/change it saw (a cursor) and pulls
    # only what's new from there — lookback is not re-applied per sync. `bean sync --rebuild` ignores
    # the cursor and reaches back `--since` days. Set per source with `config set <source>.lookback_days N`.
    "slack": {
        "lookback_days": 14,  # first sync backfills this many days; later syncs continue from the cursor
        # Chat is short and bursty — smaller windows than the global default keep a hit tight to the
        # message that matched instead of dragging in a whole thread. Overrides `chunking` above.
        "chunking": {"lines": 15, "overlap": 3, "max_chars": 1000, "min_chars": 20},
    },
    "discord": {
        "lookback_days": 14,  # same initial backfill as Slack (first sync only, then the cursor)
    },
    "cloud": {
        # Whether the Lance catalog lives on S3 (with a full local replica) instead of purely
        # locally. false = the local-only behaviour from Phase 1; nothing else in this block
        # matters when disabled.
        "enabled": False,
        "role": "writer",   # "writer" | "consumer" — who may push commits to the shared bucket
        "bucket": "",
        "prefix": "",
        "region": "",
    },
    "gdocs": {
        # With no doc/folder explicitly added, bean auto-indexes the Google Docs + PDFs you own.
        # The first sync reaches back this many days; later syncs discover only files changed since
        # the last sync (cursor). Already-indexed files are retained past the window (only trashing
        # or losing access evicts them). 0 = no window (every file you own, every sync).
        "lookback_days": 30,
        # Also index each Drive comment (+ its replies) as its own author-attributed, timestamped
        # doc, so "eric's most recent comment on my doc" is answerable. Costs one extra API call per
        # file per sync (comments live outside the file revision). false = don't fetch comments.
        "comments": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def global_path():
    return bean_home() / "config.json"


def load_global() -> dict:
    try:
        return json.loads(global_path().read_text())
    except (OSError, ValueError):
        return {}


def save_global(cfg: dict) -> None:
    bean_home().mkdir(parents=True, exist_ok=True)
    global_path().write_text(json.dumps(cfg, indent=2) + "\n")


def resolve(ws=None) -> dict:
    """DEFAULTS ← global ← this workspace's `settings` block."""
    cfg = _merge(DEFAULTS, load_global())
    if ws is not None:
        cfg = _merge(cfg, (ws.load_config() or {}).get("settings") or {})
    return cfg


def chunking_for(cfg: dict, source: str) -> dict:
    """The effective chunking config for one source: the global `chunking` defaults with that
    source's own `chunking` sub-block (e.g. `slack.chunking`) merged on top."""
    return _merge(cfg.get("chunking") or {}, (cfg.get(source) or {}).get("chunking") or {})


def get(cfg: dict, path: str, default=None):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_in(cfg: dict, path: str, value) -> dict:
    """Set a dotted leaf, coercing value to the type of the existing default when there is one."""
    parts = path.split(".")
    ref = get(DEFAULTS, path, _MISSING)
    # A per-source chunking override (e.g. `github.chunking.lines`) has no baked-in default, so
    # coerce it against the matching global `chunking` leaf instead.
    if ref is _MISSING and len(parts) >= 2 and parts[-2] == "chunking":
        ref = get(DEFAULTS, f"chunking.{parts[-1]}", _MISSING)
    if ref is not _MISSING and value is not None:
        value = _coerce(value, ref)
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    return cfg


_MISSING = object()


def _coerce(value, like):
    if isinstance(like, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    return value


def known_paths() -> list[str]:
    """Every dotted leaf in DEFAULTS — the surface `bean config` documents."""
    out: list[str] = []

    def walk(d, prefix=""):
        for k, v in d.items():
            p = f"{prefix}{k}"
            if isinstance(v, dict):
                walk(v, p + ".")
            else:
                out.append(p)

    walk(DEFAULTS)
    return out
