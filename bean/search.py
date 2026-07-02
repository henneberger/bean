"""Retrieval: hybrid search plus the canned primitives an assistant composes to reconstruct
context intelligently.

- `search()` fuses a vector ranking (Lance, semantic) with a keyword ranking (DuckDB, exact) via
  reciprocal rank fusion, then optionally pulls in neighbouring chunks so a hit arrives with its
  surroundings. Deterministic keyword hits mean an identifier or error string is never lost to
  fuzzy nearest-neighbours.
- `recent()`, `thread()`, `document()`, `neighbors()` are the "I had a convo in #product, what's
  the impact on my docs" toolbox: grab the recent conversation, then search the docs for its
  topics. Each returns the same hit shape as `search()`."""

from __future__ import annotations

from . import config as cfgmod
from .index import search as vector_search
from .store import Store


def _rrf(ranked_lists: list[list[dict]], rrf_k: int) -> dict:
    scores: dict = {}
    best: dict = {}
    for lst in ranked_lists:
        for rank, hit in enumerate(lst):
            hid = hit["id"]
            scores[hid] = scores.get(hid, 0.0) + 1.0 / (rrf_k + rank)
            best.setdefault(hid, hit)
    for hid, s in scores.items():
        best[hid] = {**best[hid], "score": round(s, 5)}
    return best


def search(ws, query: str, *, k: int | None = None, source: str | None = None,
           doc_like: str | None = None, expand: int | None = None, hybrid: bool | None = None,
           embed_query_fn=None, log=lambda m: None) -> list[dict]:
    cfg = cfgmod.resolve(ws)["search"]
    k = k or cfg["k"]
    hybrid = cfg["hybrid"] if hybrid is None else hybrid
    expand = cfg["expand"] if expand is None else expand
    pool = max(k * 4, cfg["keyword_pool"])

    if embed_query_fn is None:
        model = cfgmod.resolve(ws)["embedding"]["model"]
        from .embed import embed_query as _eq
        embed_query_fn = lambda q: _eq(q, model)  # noqa: E731

    lists: list[list[dict]] = []
    vec = vector_search(ws, embed_query_fn(query), k=pool, source=source)
    if doc_like:
        vec = [h for h in vec if doc_like.lower() in h["doc_id"].lower()]
    lists.append(vec)

    with Store(ws) as store:
        if hybrid:
            lists.append(store.keyword_search(query, k=pool, source=source, doc_like=doc_like))
        fused = _rrf(lists, cfg["rrf_k"])
        hits = sorted(fused.values(), key=lambda h: h["score"], reverse=True)[:k]
        if expand:
            hits = _expand(store, hits, expand)
    return hits


def _expand(store: Store, hits: list[dict], radius: int) -> list[dict]:
    """Attach `context` — the hit's chunk plus `radius` neighbours on each side, joined."""
    out = []
    for h in hits:
        ordv = h.get("ord")
        if ordv is None:
            row = store.chunk_by_id(h["id"])
            ordv = row.get("ord") if row else None
        ctx = store.neighbors(h["source"], h["doc_id"], ordv, radius) if ordv is not None else []
        h = {**h, "context": "\n".join(c["text"] for c in ctx) if ctx else h["text"]}
        out.append(h)
    return out


# -- canned primitives --------------------------------------------------------------------------
def _as_hits(rows: list[dict]) -> list[dict]:
    hits = []
    for r in rows:
        h = {"id": r.get("id") or f"{r['source']}/{r['doc_id']}", "source": r["source"],
             "doc_id": r["doc_id"], "title": r.get("title"), "url": r.get("url") or None,
             "text": r.get("text") or r.get("body", ""), "score": None}
        for key in ("created_at", "modified_at", "author", "mime"):
            if r.get(key) is not None:
                h[key] = str(r[key]) if "_at" in key else r[key]
        hits.append(h)
    return hits


def recent(ws, *, source: str | None = None, doc_like: str | None = None,
           limit: int = 20) -> list[dict]:
    with Store(ws) as store:
        return _as_hits(store.recent(source=source, doc_like=doc_like, limit=limit))


def document(ws, doc_like: str, *, source: str | None = None) -> list[dict]:
    """Full body of the best-matching document(s) by id/title substring."""
    with Store(ws) as store:
        return _as_hits(store.find_docs(source=source, doc_like=doc_like, limit=5))


def thread(ws, doc_like: str, *, source: str | None = None) -> list[dict]:
    """A whole thread/document as one block — Slack week digests and docs alike."""
    return document(ws, doc_like, source=source)


def neighbors(ws, chunk_id: str, *, radius: int = 3) -> list[dict]:
    with Store(ws) as store:
        row = store.chunk_by_id(chunk_id)
        if not row:
            return []
        return _as_hits(store.neighbors(row["source"], row["doc_id"], row["ord"], radius))
