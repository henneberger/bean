"""Resolved configuration — everything is a config value, nothing is an environment variable.

Three layers, deep-merged in order (later wins):

  1. DEFAULTS          — the values baked in here.
  2. global config     — ~/.bean/config.json (per user; embedding model, chunking, OCR…).
  3. workspace settings — the "settings" block of a repo's config.json (per-repo overrides).

`resolve(ws)` returns the merged dict. `get(cfg, "embedding.model")` and the `bean config`
CLI walk it by dotted path so any leaf is reachable without special-casing. Secrets never live
here — tokens stay in ~/.bean/credentials/ (mode 0600)."""

from __future__ import annotations

import copy
import json

from .workspace import bean_home

DEFAULTS: dict = {
    "embedding": {
        # Any fastembed-supported model. Changing this needs a `bean reembed`; `bean status`
        # warns when the index was built with a different model than this.
        "model": "BAAI/bge-small-en-v1.5",
        "batch_size": 64,
    },
    "chunking": {
        "lines": 40,        # window height, in lines
        "overlap": 8,       # lines shared between adjacent windows
        "max_chars": 2000,  # hard cap on a chunk's characters
        "min_chars": 40,    # windows shorter than this are dropped
    },
    "search": {
        "hybrid": True,      # fuse vector + keyword; False = vector only
        "k": 8,
        "rrf_k": 60,         # reciprocal-rank-fusion constant
        "keyword_pool": 200, # keyword candidates fused with the vector candidates
        "expand": 1,         # neighbouring chunks pulled in around each hit (0 = off)
    },
    "ocr": {
        # Backend for PDF text. "auto" tries native text first, OCR only for image-only pages.
        # "unlimited-ocr" forces the baidu/Unlimited-OCR model; "text" never OCRs.
        "backend": "auto",
        "model": "baidu/Unlimited-OCR",
        "dpi": 200,
    },
    "slack": {
        "lookback_days": 14,  # recent history re-fetched every sync to catch edits/deletes
    },
    "gdocs": {
        # With no doc/folder explicitly added, bean auto-indexes the Google Docs you own that
        # were modified within this window. Already-indexed docs are retained past it (only
        # trashing or losing access evicts them). 0 = no window (every doc you own).
        "lookback_days": 30,
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
