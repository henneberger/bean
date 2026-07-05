"""Sync orchestration: fetch each registered source into the DuckDB catalog, then chunk +
embed every changed document into Lance and mirror its chunk metadata into DuckDB. Sources and
the embedder are injectable, so the whole pipeline runs offline in tests. Only changed
documents are re-embedded; deletions revoke their vectors and chunk rows."""

from __future__ import annotations

from . import config as cfgmod
from .chunks import Chunk, chunk_text
from .index import chunk_rows, delete_doc, ensure_indexes, reindex_doc
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


def _chunk_and_embed(store, source, doc_id, embed_fn, chunk_cfg):
    """Chunk one doc's body (base + optional coarse "large" chunks) and embed the enriched text.
    Returns `(all_chunks, vectors)` — pure w.r.t. Lance, so both the local write path
    (`_embed_rows`) and the cloud-writer orchestration (which hands the result to
    `index.chunk_rows` for a remote commit instead of writing locally) share this exact logic."""
    doc = store.get(source, doc_id)
    key = f"{source}/{doc_id}"
    base = chunk_text(doc.body, key, chunk_cfg)
    large = _large_chunks(base, key, chunk_cfg) if chunk_cfg.get("large_chunks") else []
    all_chunks = base + large
    # Enrich the *embedded* text with the doc title so short/mid-doc chunks carry what the doc is
    # about; the stored/displayed text stays raw. Toggling title_prefix/large_chunks needs a rebuild.
    prefix = f"{doc.title}\n" if chunk_cfg.get("title_prefix") and doc.title else ""
    vectors = embed_fn([prefix + c.text for c in all_chunks]) if all_chunks else []
    return all_chunks, vectors


def _embed_rows(ws, store, source, doc_id, embed_fn, chunk_cfg) -> int:
    all_chunks, vectors = _chunk_and_embed(store, source, doc_id, embed_fn, chunk_cfg)
    doc = store.get(source, doc_id)
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
    # Committed `.bean/config.json` refs merged under the personal ones — a fresh clone of a
    # repo with a `.bean` folder syncs the team's tracked sources with zero setup.
    config = ws.effective_config() if hasattr(ws, "effective_config") else ws.load_config()
    settings = cfgmod.resolve(ws)
    if embed_fn is None:
        from .embed import embedder  # lazy: model load only when embedding
        embed_fn = embedder(settings["embedding"])

    if ws.is_cloud and ws.cloud.get("role") != "writer":
        raise RuntimeError(
            "this is a read-only cloud consumer — nothing to sync; run `bean pull` to fetch the "
            "latest index, or become the writer (`bean cloud role writer`; for s3, `bean cloud init`).")

    if ws.is_cloud and ws.cloud.get("role") == "writer":
        if full:
            raise RuntimeError(
                "cloud `--rebuild` cannot re-embed unchanged docs in v1. To change chunking or the "
                "embedding model for a cloud index, rebuild locally and re-run `bean cloud init` to "
                "re-upload.")
        return _run_sync_cloud(ws, config=config, settings=settings, embed_fn=embed_fn,
                               only=only, keys=keys, full=full, since_days=since_days,
                               fetch=fetch, refetch=refetch, log=log)

    changed: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    errors: list[str] = []
    skipped: list[str] = []
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
            # A source can be tracked without being connected — normal when a committed `.bean`
            # declares team sources this user hasn't authed yet. Skip with a nudge, not an error.
            if src.auth and src.connected and not src.connected():
                log(f"{src.key}: tracked but not connected — skipped (run `bean auth {src.auth}`)")
                skipped.append(src.key)
                continue
            try:
                r = src.sync(store, src_cfg, settings=settings, fetch=fetch, full=full,
                             since_days=since_days, log=log)
            except Exception as err:  # keep the other sources running
                errors.append(f"{src.key}: {err}")
                continue
            changed += [(src.key, d) for d in r.get("changed", [])]
            removed += [(src.key, d) for d in r.get("removed", [])]

        # The embed queue comes from the STORE, not just this run's `changed`: any doc whose
        # embedded chunks are missing or stale (embedded_hash != hash) is (re)embedded — so a sync
        # interrupted mid-embed resumes cleanly instead of silently leaving docs unindexed. `--full`
        # forces the whole set (absorbing the old `reembed`). Oldest first, and each doc is
        # checkpointed the moment its vectors land, so progress is durable every step of the way.
        if only is not None:
            scope = {only} if (keys is None or only in keys) else set()
        elif keys is not None:
            scope = set(keys)
        else:
            scope = None
        embed_targets = store.embed_queue(scope, force=full)

        chunks_indexed = 0
        for source, doc_id in embed_targets:
            chunks_indexed += _embed_rows(ws, store, source, doc_id, embed_fn,
                                          cfgmod.chunking_for(settings, source))
            store.mark_embedded(source, doc_id)  # durable checkpoint — resume-safe if interrupted
            log(f"indexed {source}/{doc_id}")
        for source, doc_id in removed:
            delete_doc(ws, source, doc_id)
        if embed_targets or removed:
            ensure_indexes(ws, log=log)  # keep scalar/vector indexes current after any index change

        # Lightweight relationship graph: derive edges (authored_by / in-container) for the docs we
        # (re)embedded — changed docs on a plain sync, every doc on a --rebuild.
        if settings.get("graph", {}).get("enabled", True):
            from .graph import implied_edges
            for source, doc_id in embed_targets:
                doc = store.get(source, doc_id)
                if doc:
                    store.replace_edges(source, doc_id, implied_edges(doc))

        from .embed import identity
        store.set_state("embedding.model", identity(settings["embedding"]))
        from datetime import datetime, timezone
        store.set_state("last_sync", datetime.now(timezone.utc).isoformat())

    return {"changed": changed, "removed": removed, "errors": errors, "skipped": skipped,
            "chunks": chunks_indexed, "embedded": len(embed_targets)}


def _active_sources(config: dict, *, only: str | None, keys: set | None, log=lambda m: None):
    """Same source-filter/active logic as the local loop above: `only`/`keys` narrow which
    registered sources run, `is_active` skips a source with nothing tracked (and no
    always-when-connected override), and a tracked-but-not-connected source is skipped with a
    nudge rather than erroring (see the local loop)."""
    for src in SOURCES:
        if only and only != src.key:
            continue
        if keys is not None and src.key not in keys:
            continue
        src_cfg = config.get(src.config_key) or {}
        if not src.is_active(src_cfg):
            continue
        if src.auth and src.connected and not src.connected():
            log(f"{src.key}: tracked but not connected — skipped (run `bean auth {src.auth}`)")
            continue
        yield src, src_cfg


def _run_sync_cloud(ws: Workspace, *, config: dict, settings: dict, embed_fn, only: str | None,
                    keys: set | None, full: bool, since_days: int, fetch, refetch: bool,
                    log) -> dict:
    """Cloud-writer orchestration: writes commit straight to the S3 (or local-dir, in tests) Lance
    catalog; the local replica is pull-only. Unlike the local path, changed docs are chunked +
    embedded from the connector's own `changed` list (not `Store.embed_queue`, which reads the
    replica's `documents` table and so can't see anything only staged in memory) and committed one
    source at a time — a per-source cursor snapshot lets a failed commit roll that source's state
    back so the next sync re-fetches it instead of silently losing it."""
    from . import remote
    from .graph import implied_edges
    from .workspace import credential_context

    remote.pull(ws)  # refresh the replica before anything reads it (change detection, embed_queue)

    changed: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    errors: list[str] = []
    chunks_indexed = 0
    embedded = 0
    graph_enabled = settings.get("graph", {}).get("enabled", True)
    cred_ws = None if getattr(ws, "is_global", False) else ws

    with credential_context(cred_ws), Store(ws) as store:
        for src, src_cfg in (_active_sources(config, only=only, keys=keys, log=log) if refetch else ()):
            snap = store.snapshot_state()
            try:
                r = src.sync(store, src_cfg, settings=settings, fetch=fetch, full=full,
                            since_days=since_days, log=log)
                doc_changed = list(r.get("changed", []))
                doc_removed = list(r.get("removed", []))
                chunks_by_doc: dict = {}
                edges_by_doc: dict = {}
                doc_chunks = 0
                for doc_id in doc_changed:
                    doc = store.get(src.key, doc_id)
                    all_chunks, vectors = _chunk_and_embed(store, src.key, doc_id, embed_fn,
                                                           cfgmod.chunking_for(settings, src.key))
                    chunks_by_doc[(src.key, doc_id)] = chunk_rows(src.key, doc_id, doc.title,
                                                                  doc.url, all_chunks, vectors)
                    edges_by_doc[(src.key, doc_id)] = implied_edges(doc) if graph_enabled else []
                    doc_chunks += sum(1 for v in vectors if v)
                store.commit_source(src.key, doc_changed, chunks_by_doc=chunks_by_doc,
                                    edges_by_doc=edges_by_doc)
                store.commit_deletions([(src.key, d) for d in doc_removed])
                changed += [(src.key, d) for d in doc_changed]
                removed += [(src.key, d) for d in doc_removed]
                chunks_indexed += doc_chunks
                embedded += len(doc_changed)
                log(f"cloud-sync {src.key}: {len(doc_changed)} changed, {len(doc_removed)} removed")
            except Exception as err:  # keep the other sources running; undo this one's cursor advance
                store.restore_state(snap)
                errors.append(f"{src.key}: {err}")

        tbl = store._remote.table("chunks")
        if tbl is not None:  # nothing to index until at least one commit has landed chunks
            ensure_indexes(ws, table=tbl, log=log)

        from .embed import identity
        store.set_state("embedding.model", identity(settings["embedding"]))
        from datetime import datetime, timezone
        store.set_state("last_sync", datetime.now(timezone.utc).isoformat())

    remote.pull(ws)  # pull the just-committed data down so local reads see it immediately

    return {"changed": changed, "removed": removed, "errors": errors, "skipped": [],
            "chunks": chunks_indexed, "embedded": embedded}
