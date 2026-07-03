"""Sync orchestration: fetch each registered source into the DuckDB catalog, then chunk +
embed every changed document into Lance and mirror its chunk metadata into DuckDB. Sources and
the embedder are injectable, so the whole pipeline runs offline in tests. Only changed
documents are re-embedded; deletions revoke their vectors and chunk rows."""

from __future__ import annotations

from . import config as cfgmod
from .chunks import Chunk, chunk_text
from .index import delete_doc, reindex_doc
from .sources import SOURCES
from .store import Store
from .workspace import Workspace


def _large_chunks(base: list, key: str, cfg: dict) -> list:
    """Coarse doc-level chunks: every `large_chunk_ratio` base chunks joined into one, so broad
    'which doc is about X' questions match at a section granularity. Vector-only (Lance)."""
    ratio = max(2, int(cfg.get("large_chunk_ratio", 4)))
    cap = int(cfg.get("max_chars", 2000)) * ratio
    out = []
    for i in range(0, len(base), ratio):
        grp = base[i:i + ratio]
        if len(grp) < 2:  # a lone chunk adds nothing over its base chunk
            continue
        text = "\n".join(c.text for c in grp)[:cap]
        out.append(Chunk(id=f"{key}#L{grp[0].start}-large", start=grp[0].start,
                         end=grp[-1].end, text=text))
    return out


def _embed_rows(ws, store, source, doc_id, embed_fn, chunk_cfg) -> int:
    doc = store.get(source, doc_id)
    key = f"{source}/{doc_id}"
    base = chunk_text(doc.body, key, chunk_cfg)
    large = _large_chunks(base, key, chunk_cfg) if chunk_cfg.get("large_chunks") else []
    all_chunks = base + large
    # Enrich the *embedded* text with the doc title so short/mid-doc chunks carry what the doc is
    # about; the stored/displayed text stays raw. Toggling title_prefix/large_chunks needs a rebuild.
    prefix = f"{doc.title}\n" if chunk_cfg.get("title_prefix") and doc.title else ""
    vectors = embed_fn([prefix + c.text for c in all_chunks]) if all_chunks else []
    # Lance is the single home for chunks (base + large; vectors from enriched text, stored text
    # raw). Keyword search / neighbours / merge query it via DuckDB — no separate mirror to write.
    reindex_doc(ws, source=source, doc_id=doc_id, title=doc.title, url=doc.url,
                chunks=all_chunks, vectors=vectors)
    return sum(1 for v in vectors if v)


def run_sync(ws: Workspace, *, only: str | None = None, keys: set | None = None,
             full: bool = False, since_days: int = 90, embed_fn=None, fetch=None,
             refetch: bool = True, log=lambda m: None) -> dict:
    """Sync the workspace `ws`. `keys` restricts to a set of source keys (used to route global
    sources into the global workspace and local ones into the repo workspace); None = all.

    `full` (the CLI's `--rebuild`) reaches past every source's cursor to re-fetch back `since_days`
    AND re-embeds every stored document — so a chunking or embedding-model change lands on the whole
    index, not just docs whose bodies happened to change. A plain sync re-embeds only what changed.

    `refetch=False` skips the fetch phase entirely and only re-embeds already-stored docs (used by
    tests to re-embed a hand-populated store without touching any live source)."""
    config = ws.load_config()
    settings = cfgmod.resolve(ws)
    if embed_fn is None:
        from .embed import embedder  # lazy: model load only when embedding
        embed_fn = embedder(settings["embedding"]["model"], settings["embedding"]["batch_size"])

    changed: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    errors: list[str] = []
    from .workspace import credential_context
    # Local sources read this repo's credentials (fallback shared); global sources read the shared.
    cred_ws = None if getattr(ws, "is_global", False) else ws
    with credential_context(cred_ws), Store(ws) as store:
        for src in SOURCES if refetch else ():
            if only and only != src.key:
                continue
            if keys is not None and src.key not in keys:
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

        # A plain sync re-embeds only changed docs; a --rebuild re-embeds every stored doc in the
        # synced sources so a chunking/model change reaches the whole index (this absorbs the old
        # `reembed` command). Deduped, and only for sources actually in scope this run.
        def _in_scope(src_key: str) -> bool:
            return (not only or only == src_key) and (keys is None or src_key in keys)

        if full:
            embed_targets = [(s, d) for s in store.counts() if _in_scope(s)
                             for d in store.doc_ids(s)]
        else:
            embed_targets = list(dict.fromkeys(changed))

        chunks_indexed = 0
        for source, doc_id in embed_targets:
            chunks_indexed += _embed_rows(ws, store, source, doc_id, embed_fn,
                                          cfgmod.chunking_for(settings, source))
            log(f"indexed {source}/{doc_id}")
        for source, doc_id in removed:
            delete_doc(ws, source, doc_id)

        # Lightweight relationship graph: derive edges (authored_by / in-container) for the docs we
        # (re)embedded — changed docs on a plain sync, every doc on a --rebuild.
        if settings.get("graph", {}).get("enabled", True):
            from .graph import implied_edges
            for source, doc_id in embed_targets:
                doc = store.get(source, doc_id)
                if doc:
                    store.replace_edges(source, doc_id, implied_edges(doc))

        store.set_state("embedding.model", settings["embedding"]["model"])
        from datetime import datetime, timezone
        store.set_state("last_sync", datetime.now(timezone.utc).isoformat())

    return {"changed": changed, "removed": removed, "errors": errors,
            "chunks": chunks_indexed, "embedded": len(embed_targets)}
