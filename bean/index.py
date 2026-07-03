"""Lance vector store for a workspace. One `chunks` table; rows carry enough metadata
(title, url, text) that a search result needs no second lookup."""

from __future__ import annotations

import lancedb

TABLE = "chunks"


def _db(ws):
    ws.lance_dir.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(ws.lance_dir))


def _table(db):
    return db.open_table(TABLE) if TABLE in db.table_names() else None


def reindex_doc(ws, *, source: str, doc_id: str, title: str, url: str | None,
                chunks, vectors) -> int:
    """Replace every chunk row for one document (delete + add). Idempotent per snapshot."""
    db = _db(ws)
    tbl = _table(db)
    if tbl is not None:
        tbl.delete(f"source = '{_esc(source)}' AND doc_id = '{_esc(doc_id)}'")
    rows = [
        {"id": c.id, "source": source, "doc_id": doc_id, "title": title, "url": url or "",
         "start": c.start, "end": c.end, "text": c.text, "vector": v}
        for c, v in zip(chunks, vectors) if v
    ]
    if not rows:
        return 0
    if tbl is None:
        db.create_table(TABLE, rows)
    else:
        tbl.add(rows)
    return len(rows)


def delete_doc(ws, source: str, doc_id: str) -> None:
    tbl = _table(_db(ws))
    if tbl is not None:
        tbl.delete(f"source = '{_esc(source)}' AND doc_id = '{_esc(doc_id)}'")


def chunks_dataset(ws):
    """The chunk table as a pylance `LanceDataset`, or None if nothing is indexed yet. This is the
    single copy of the chunk data — DuckDB queries it directly (register + SQL) for the keyword /
    neighbour / merge work, so there is no separate chunk mirror to keep in sync."""
    import lance
    path = ws.lance_dir / f"{TABLE}.lance"
    return lance.dataset(str(path)) if path.exists() else None


def search(ws, vector, k: int = 8, source: str | None = None) -> list[dict]:
    tbl = _table(_db(ws))
    if tbl is None:
        return []
    q = tbl.search(vector).distance_type("cosine").limit(k)
    if source:
        q = q.where(f"source = '{_esc(source)}'")
    out = []
    for r in q.to_list():
        out.append({
            "id": r["id"], "source": r["source"], "doc_id": r["doc_id"], "title": r["title"],
            "url": r["url"] or None, "start": r["start"], "end": r["end"], "text": r["text"],
            "score": round(1 - r.get("_distance", 1.0), 3),
        })
    return out


def _esc(s: str) -> str:
    return str(s).replace("'", "''")
