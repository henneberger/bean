"""Retrieval: hybrid search plus the canned primitives an assistant composes to reconstruct
context intelligently.

`search()` fuses one or more query variants — each producing a vector ranking (Lance, semantic) and
a keyword ranking (DuckDB, exact) — with **weighted reciprocal-rank fusion**. Weights come from
config and, when `search.auto_weight` is on, from the query's shape (an identifier/quoted query leans
keyword; a natural-language question leans vector). After fusion it optionally applies a **recency**
multiplier, **merges adjacent same-doc chunks** into sections, and runs an optional **local
cross-encoder reranker** — all config-driven. Author/date filters and a `related()` graph-expansion
round out the toolbox: `recent()`, `thread()`, `document()`, `neighbors()`, `related()`."""

from __future__ import annotations

import re
import time

from . import config as cfgmod
from .index import search as vector_search
from .store import Store

_YEAR = 365.25 * 86400
_ID_RE = re.compile(r"[A-Za-z]+[-_/]?\d|\d+[-_/][A-Za-z0-9]|[A-Z]{2,}\b|[#/][\w.-]+|['\"]")
_QUESTION = re.compile(r"\b(how|why|what|when|where|who|which|does|do|is|are|can|should|explain)\b", re.I)


# -- fusion + query-type routing ----------------------------------------------------------------
def _rrf(weighted_lists: list[tuple], rrf_k: int) -> dict:
    """Weighted RRF: each list contributes weight/(rrf_k + rank) to its hits' scores."""
    scores: dict = {}
    best: dict = {}
    for lst, weight in weighted_lists:
        for rank, hit in enumerate(lst):
            hid = hit["id"]
            scores[hid] = scores.get(hid, 0.0) + weight / (rrf_k + rank)
            best.setdefault(hid, hit)
    for hid, s in scores.items():
        best[hid] = {**best[hid], "score": round(s, 6)}
    return best


def _query_weights(q: str, cfg: dict) -> tuple[float, float]:
    """(vector_weight, keyword_weight) for one query. With auto_weight, nudge toward keyword for
    identifier/quoted/short-exact queries and toward vector for natural-language questions."""
    vw, kw = float(cfg["vector_weight"]), float(cfg["keyword_weight"])
    if not cfg.get("auto_weight"):
        return vw, kw
    toks = q.split()
    identifierish = bool(_ID_RE.search(q)) or (len(toks) <= 3 and any(c.isdigit() for c in q))
    questiony = q.rstrip().endswith("?") or (len(toks) >= 5 and bool(_QUESTION.search(q)))
    if identifierish and not questiony:
        return vw * 0.6, kw * 1.6
    if questiony and not identifierish:
        return vw * 1.3, kw * 0.8
    return vw, kw


# -- recency ------------------------------------------------------------------------------------
def _epoch(ts) -> float | None:
    if ts is None:
        return None
    if hasattr(ts, "timestamp"):
        try:
            return ts.timestamp()
        except Exception:
            return None
    return None


def _recency_factor(modified, now: float, decay: float, floor: float) -> float:
    ep = _epoch(modified)
    age = 0.25 if ep is None else max(0.0, (now - ep) / _YEAR)   # missing → ~a quarter old
    return max(1.0 / (1.0 + decay * age), floor)


# -- section merge ------------------------------------------------------------------------------
def _merge_sections(store: Store, hits: list[dict]) -> list[dict]:
    """Coalesce hits from the same document with overlapping/adjacent line ranges into one section,
    text taken from the document body over the union range (deduped by construction). Best score
    wins; result stays ordered by score."""
    by_doc: dict = {}
    order: list = []
    for h in hits:
        key = (h["source"], h["doc_id"])
        if key not in by_doc:
            by_doc[key] = []
            order.append(key)
        by_doc[key].append(h)
    bodies: dict = {}
    merged: list[dict] = []
    for key in order:
        group = sorted(by_doc[key], key=lambda h: (h.get("start") or 0))
        runs: list[list[dict]] = []
        for h in group:
            if runs and (h.get("start") or 0) <= (runs[-1][-1].get("end") or 0) + 1:
                runs[-1].append(h)
            else:
                runs.append([h])
        for run in runs:
            rep = max(run, key=lambda h: h.get("score") or 0)
            start = min((h.get("start") or 1) for h in run)
            end = max((h.get("end") or start) for h in run)
            if len(run) == 1:
                text = rep.get("text", "")
            else:
                src, did = key
                if key not in bodies:
                    doc = store.get(src, did)
                    bodies[key] = doc.body if doc else ""
                lines = bodies[key].split("\n")
                text = "\n".join(lines[start - 1:end]).strip() or rep.get("text", "")
            merged.append({**rep, "start": start, "end": end, "text": text, "context": text})
    merged.sort(key=lambda h: h.get("score") or 0, reverse=True)
    return merged


def _expand(store: Store, hits: list[dict], radius: int) -> list[dict]:
    out = []
    for h in hits:
        ordv = h.get("ord")
        if ordv is None:
            row = store.chunk_by_id(h["id"])
            ordv = row.get("ord") if row else None
        ctx = store.neighbors(h["source"], h["doc_id"], ordv, radius) if ordv is not None else []
        out.append({**h, "context": "\n".join(c["text"] for c in ctx) if ctx else h.get("text", "")})
    return out


# -- search -------------------------------------------------------------------------------------
def search(ws, query: str, *, queries: list[str] | None = None, k: int | None = None,
           source: str | None = None, doc_like: str | None = None, expand: int | None = None,
           hybrid: bool | None = None, author: str | None = None, since=None, before=None,
           embed_query_fn=None, rerank_fn=None, now: float | None = None,
           log=lambda m: None) -> list[dict]:
    cfg = cfgmod.resolve(ws)["search"]
    k = k or cfg["k"]
    hybrid = cfg["hybrid"] if hybrid is None else hybrid
    expand = cfg["expand"] if expand is None else expand
    now = now if now is not None else time.time()
    pool = max(k * 4, cfg["keyword_pool"])
    qtexts = [query] + [q for q in (queries or []) if q and q.strip()]

    if embed_query_fn is None:
        from .embed import query_embedder
        embed_query_fn = query_embedder(cfgmod.resolve(ws)["embedding"])

    with Store(ws) as store:
        weighted: list[tuple] = []
        for i, q in enumerate(qtexts):
            qw = 1.0 if i == 0 else 0.7        # variants (assistant paraphrases) weigh a bit less
            vw, kw = _query_weights(q, cfg)
            vec = vector_search(ws, embed_query_fn(q), k=pool, source=source)
            if doc_like:
                vec = [h for h in vec if doc_like.lower() in h["doc_id"].lower()]
            weighted.append((vec, qw * vw))
            if hybrid:
                weighted.append((store.keyword_search(q, k=pool, source=source, doc_like=doc_like),
                                 qw * kw))
        fused = _rrf(weighted, cfg["rrf_k"])
        hits = list(fused.values())

        # metadata filters (author / date) + recency, from the documents table
        need_meta = bool(author or since or before or cfg.get("recency_decay"))
        if need_meta:
            meta = store.doc_meta_map((h["source"], h["doc_id"]) for h in hits)
            if author or since or before:
                hits = [h for h in hits if _passes(meta.get((h["source"], h["doc_id"]), {}),
                                                   author, since, before)]
            decay = float(cfg.get("recency_decay") or 0.0)
            if decay:
                floor = float(cfg.get("recency_floor", 0.75))
                for h in hits:
                    m = meta.get((h["source"], h["doc_id"]), {})
                    h["score"] = round(h["score"] * _recency_factor(m.get("modified_at"), now,
                                                                    decay, floor), 6)
        hits.sort(key=lambda h: h.get("score") or 0, reverse=True)

        rr = cfg.get("rerank") or {}
        cand_n = max(k, int(rr.get("pool", 40))) if rr.get("enabled") else max(k, k * 3)
        cands = hits[:cand_n]
        if cfg.get("merge_sections"):
            cands = _merge_sections(store, cands)

        if rr.get("enabled"):
            fn = rerank_fn
            if fn is None:
                from .rerank import reranker
                fn = reranker(rr.get("model", "Xenova/ms-marco-MiniLM-L-6-v2"))
            texts = [h.get("context") or h.get("text", "") for h in cands]
            try:
                rscores = fn(query, texts)
                for h, s in zip(cands, rscores):
                    h["score"] = round(float(s), 6)
                cands.sort(key=lambda h: h.get("score") or 0, reverse=True)
            except Exception as err:  # a missing model must not kill the search
                log(f"rerank skipped ({err})")

        final = cands[:k]
        if expand and not cfg.get("merge_sections"):
            final = _expand(store, final, expand)
    return final


def _passes(meta: dict, author, since, before) -> bool:
    if author and author.lower() not in str(meta.get("author") or "").lower():
        return False
    mod = meta.get("modified_at")
    ep = _epoch(mod)
    if since is not None and (ep is None or ep < _to_epoch(since)):
        return False
    if before is not None and (ep is None or ep >= _to_epoch(before)):
        return False
    return True


def _to_epoch(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    ep = _epoch(v)
    if ep is not None:
        return ep
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(v), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


# -- canned primitives --------------------------------------------------------------------------
def _as_hits(rows: list[dict]) -> list[dict]:
    hits = []
    for r in rows:
        h = {"id": r.get("id") or f"{r['source']}/{r['doc_id']}", "source": r["source"],
             "doc_id": r["doc_id"], "title": r.get("title"), "url": r.get("url") or None,
             "text": r.get("text") or r.get("body", ""), "score": None}
        for key in ("created_at", "modified_at", "fetched_at", "author", "mime", "reason"):
            if r.get(key) is not None:
                h[key] = str(r[key]) if "_at" in key else r[key]
        hits.append(h)
    return hits


def recent(ws, *, source: str | None = None, doc_like: str | None = None, author: str | None = None,
           since=None, before=None, limit: int = 20) -> list[dict]:
    with Store(ws) as store:
        return _as_hits(store.recent(source=source, doc_like=doc_like, author=author,
                                     since=since, before=before, limit=limit))


def document(ws, doc_like: str, *, source: str | None = None) -> list[dict]:
    """Full body of the best-matching document(s) by id/title substring."""
    with Store(ws) as store:
        return _as_hits(store.find_docs(source=source, doc_like=doc_like, limit=5))


def thread(ws, doc_like: str, *, source: str | None = None) -> list[dict]:
    """A whole thread/document as one block — Slack threads and docs alike."""
    return document(ws, doc_like, source=source)


def neighbors(ws, chunk_id: str, *, radius: int = 3) -> list[dict]:
    with Store(ws) as store:
        row = store.chunk_by_id(chunk_id)
        if not row:
            return []
        return _as_hits(store.neighbors(row["source"], row["doc_id"], row["ord"], radius))


def related(ws, doc_like: str, *, source: str | None = None, limit: int = 20) -> list[dict]:
    """Documents one hop away in the edge graph from the best id/title match — same
    repo/project/channel/author, or directly linked. Each hit carries a `reason`."""
    with Store(ws) as store:
        match = store.find_docs(source=source, doc_like=doc_like, limit=1)
        if not match:
            return []
        m = match[0]
        return _as_hits(store.related(m["source"], m["doc_id"], limit=limit))


# -- scope-aware unions: run a retrieval across [repo workspace, global workspace] and merge ------
def _as_list(wss) -> list:
    return list(wss) if isinstance(wss, (list, tuple)) else [wss]


def _dedup(hits: list[dict], k: int | None, *, by_recency: bool = False) -> list[dict]:
    if by_recency:
        # Mirror the store's `COALESCE(modified_at, fetched_at) DESC`: fall back to fetch time so
        # sources that leave modified_at NULL still order by when bean last saw them, not arbitrarily.
        def _recency(h):
            ts = h.get("modified_at") or h.get("fetched_at")
            return (ts is None, ts or "")
        hits = sorted(hits, key=_recency, reverse=True)
    else:
        hits = sorted(hits, key=lambda h: (h.get("score") is None, -(h.get("score") or 0.0)))
    seen, out = set(), []
    for h in hits:
        key = (h.get("source"), h.get("doc_id"), h.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out[:k] if k else out


def search_many(wss, query: str, *, k: int | None = None, **kw) -> list[dict]:
    wss = _as_list(wss)
    kk = k or cfgmod.resolve(wss[0])["search"]["k"]
    hits: list[dict] = []
    for w in wss:
        hits += search(w, query, k=k, **kw)
    return _dedup(hits, kk)


def recent_many(wss, *, limit: int = 20, **kw) -> list[dict]:
    hits: list[dict] = []
    for w in _as_list(wss):
        hits += recent(w, limit=limit, **kw)
    return _dedup(hits, limit, by_recency=True)


def related_many(wss, doc_like: str, *, limit: int = 20, **kw) -> list[dict]:
    hits: list[dict] = []
    for w in _as_list(wss):
        hits += related(w, doc_like, limit=limit, **kw)
    return _dedup(hits, limit, by_recency=True)


def document_many(wss, doc_like: str, **kw) -> list[dict]:
    hits: list[dict] = []
    for w in _as_list(wss):
        hits += document(w, doc_like, **kw)
    return _dedup(hits, 5)


def thread_many(wss, doc_like: str, **kw) -> list[dict]:
    return document_many(wss, doc_like, **kw)


def neighbors_many(wss, chunk_id: str, *, radius: int = 3) -> list[dict]:
    for w in _as_list(wss):
        got = neighbors(w, chunk_id, radius=radius)
        if got:
            return got
    return []
