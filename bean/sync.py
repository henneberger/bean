"""Sync orchestration: fetch each registered source into the DuckDB catalog, then chunk +
embed every changed document into Lance and mirror its chunk metadata into DuckDB. Sources and
the embedder are injectable, so the whole pipeline runs offline in tests. Only changed
documents are re-embedded; deletions revoke their vectors and chunk rows."""

from __future__ import annotations

from . import config as cfgmod
from .chunks import chunk_text
from .index import delete_doc, reindex_doc
from .sources import SOURCES
from .store import Store
from .workspace import Workspace


def _embed_rows(ws, store, source, doc_id, embed_fn, chunk_cfg) -> int:
    doc = store.get(source, doc_id)
    chunks = chunk_text(doc.body, f"{source}/{doc_id}", chunk_cfg)
    vectors = embed_fn([c.text for c in chunks]) if chunks else []
    rows = [
        {"id": c.id, "title": doc.title, "url": doc.url or "",
         "start": c.start, "end": c.end, "text": c.text}
        for c, v in zip(chunks, vectors) if v
    ]
    reindex_doc(ws, source=source, doc_id=doc_id, title=doc.title, url=doc.url,
                chunks=chunks, vectors=vectors)
    store.replace_chunks(source, doc_id, rows)
    return len(rows)


def run_sync(ws: Workspace, *, only: str | None = None, full: bool = False,
             since_days: int = 90, embed_fn=None, fetch=None, log=lambda m: None) -> dict:
    config = ws.load_config()
    settings = cfgmod.resolve(ws)
    chunk_cfg = settings["chunking"]
    if embed_fn is None:
        from .embed import embedder  # lazy: model load only when embedding
        embed_fn = embedder(settings["embedding"]["model"], settings["embedding"]["batch_size"])

    changed: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    errors: list[str] = []
    with Store(ws) as store:
        for src in SOURCES:
            if only and only != src.key:
                continue
            src_cfg = config.get(src.config_key) or {}
            if not src.is_active(src_cfg):
                continue
            try:
                r = src.sync(store, src_cfg, settings=settings, fetch=fetch, full=full,
                             since_days=since_days, log=log)
            except Exception as err:  # keep the other sources running
                errors.append(f"{src.key}: {err}")
                continue
            changed += [(src.key, d) for d in r.get("changed", [])]
            removed += [(src.key, d) for d in r.get("removed", [])]

        chunks_indexed = 0
        for source, doc_id in changed:
            chunks_indexed += _embed_rows(ws, store, source, doc_id, embed_fn, chunk_cfg)
            log(f"indexed {source}/{doc_id}")
        for source, doc_id in removed:
            delete_doc(ws, source, doc_id)

        store.set_state("embedding.model", settings["embedding"]["model"])

    return {"changed": changed, "removed": removed, "errors": errors, "chunks": chunks_indexed}


def reembed(ws: Workspace, *, embed_fn=None, log=lambda m: None) -> dict:
    """Re-chunk and re-embed every stored document with the current config — used after the
    embedding model or chunking settings change. Reads bodies from DuckDB; fetches nothing."""
    settings = cfgmod.resolve(ws)
    chunk_cfg = settings["chunking"]
    if embed_fn is None:
        from .embed import embedder
        embed_fn = embedder(settings["embedding"]["model"], settings["embedding"]["batch_size"])
    total = 0
    with Store(ws) as store:
        docs = [(s, d) for s in [row.key for row in SOURCES] for d in store.doc_ids(s)]
        for source, doc_id in docs:
            total += _embed_rows(ws, store, source, doc_id, embed_fn, chunk_cfg)
            log(f"re-embedded {source}/{doc_id}")
        store.set_state("embedding.model", settings["embedding"]["model"])
    return {"docs": len(docs), "chunks": total, "model": settings["embedding"]["model"]}
