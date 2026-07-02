"""Sync orchestration: fetch each source into the DuckDB catalog, then chunk + embed every
changed document into the Lance store. Sources and the embedder are injectable, so the whole
pipeline runs offline in tests."""

from __future__ import annotations

from . import gdocs, slack
from .chunks import chunk_text
from .index import delete_doc, reindex_doc
from .store import Store
from .workspace import Workspace, load_credential


def run_sync(ws: Workspace, *, only: str | None = None, full: bool = False,
             since_days: int = 90, embed_fn=None, fetch=None, log=lambda m: None) -> dict:
    config = ws.load_config()
    if embed_fn is None:
        from .embed import embed_texts as embed_fn  # lazy: model load only when embedding

    changed: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    errors: list[str] = []
    with Store(ws) as store:
        google_cfg = config.get("google") or {}
        if only in (None, "google") and (google_cfg.get("docs") or google_cfg.get("folders")):
            try:
                r = gdocs.sync(store, google_cfg, fetch=fetch, full=full, log=log)
                changed += [("gdocs", d) for d in r["changed"]]
                removed += [("gdocs", d) for d in r["removed"]]
            except Exception as err:  # keep the other source running
                errors.append(f"gdocs: {err}")

        slack_cfg = config.get("slack") or {}
        if only in (None, "slack") and slack_cfg.get("channels"):
            cred = load_credential("slack")
            if not cred:
                errors.append("slack: not connected — run `bean auth slack --token …`.")
            else:
                try:
                    r = slack.sync(store, slack_cfg, token=cred["token"], team_url=cred.get("url"),
                                   fetch=fetch, full=full, since_days=since_days, log=log)
                    changed += [("slack", d) for d in r["changed"]]
                except Exception as err:
                    errors.append(f"slack: {err}")

        # Chunk + embed only what actually changed; deletions revoke their vectors.
        chunks_indexed = 0
        for source, doc_id in changed:
            doc = store.get(source, doc_id)
            chunks = chunk_text(doc.body, f"{source}/{doc_id}")
            vectors = embed_fn([c.text for c in chunks]) if chunks else []
            chunks_indexed += reindex_doc(ws, source=source, doc_id=doc_id, title=doc.title,
                                          url=doc.url, chunks=chunks, vectors=vectors)
            log(f"indexed {source}/{doc_id} ({len(chunks)} chunks)")
        for source, doc_id in removed:
            delete_doc(ws, source, doc_id)

    return {"changed": changed, "removed": removed, "errors": errors, "chunks": chunks_indexed}
