"""Lance vector store for a workspace. One `chunks` table; rows carry enough metadata
(title, url, text) that a search result needs no second lookup.

lancedb owns table create/add/delete + vector search, and its `table.to_lance()` hands DuckDB the
very same dataset for the keyword / neighbour / merge SQL (see `store.py`) — so there is one copy of
the chunk data. `to_lance()` returns a `lance.LanceDataset`, so `pylance` (the `lance` module) is a
direct dependency in its own right, pinned alongside lancedb in pyproject."""

from __future__ import annotations

import lancedb

TABLE = "chunks"
# Lance trains an ANN (IVF-PQ) index from the data, so a brute-force scan actually wins until the
# table is fairly large — below this floor we skip the vector index (a few thousand chunks scan in
# well under a millisecond and an IVF index over them just wastes clusters). Scalar indexes on the
# filtered columns are always worth it and build at any size.
_VECTOR_INDEX_MIN_ROWS = 4096


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
    rows = [
        {"id": c.id, "source": source, "doc_id": doc_id, "title": title, "url": url or "",
         "start": c.start, "end": c.end, "text": c.text, "vector": v}
        for c, v in zip(chunks, vectors) if v
    ]
    # An embedding-model swap changes the vector width; the existing table's fixed-size vector
    # column can't hold the new vectors (Lance raises on the cast). A dimension change requires a
    # `--rebuild`, which re-embeds every doc — so drop the stale table and let it be recreated at
    # the new width on the first write below.
    if tbl is not None and rows and _vector_dim(tbl) not in (None, len(rows[0]["vector"])):
        db.drop_table(TABLE)
        tbl = None
    if tbl is not None:
        tbl.delete(f"source = '{_esc(source)}' AND doc_id = '{_esc(doc_id)}'")
    if not rows:
        return 0
    if tbl is None:
        db.create_table(TABLE, rows)
    else:
        tbl.add(rows)
    return len(rows)


def _vector_dim(tbl) -> int | None:
    """Width of the stored `vector` column, or None if it can't be read."""
    try:
        return tbl.schema.field("vector").type.list_size
    except Exception:
        return None


def delete_doc(ws, source: str, doc_id: str) -> None:
    tbl = _table(_db(ws))
    if tbl is not None:
        tbl.delete(f"source = '{_esc(source)}' AND doc_id = '{_esc(doc_id)}'")


def ensure_indexes(ws, log=lambda m: None) -> None:
    """Build the indexes retrieval leans on, idempotently (safe to call after every sync):
    scalar indexes on the `source`/`doc_id` columns we filter and delete by, and — once the table
    is large enough to warrant it — a cosine ANN index on `vector`. Cheap to re-run: Lance skips a
    column that is already indexed, and we only (re)train the vector index when it's missing."""
    tbl = _table(_db(ws))
    if tbl is None:
        return
    existing = {i.get("columns", [None])[0] if isinstance(i, dict) else getattr(i, "columns", [None])[0]
                for i in _index_meta(tbl)}
    for col in ("source", "doc_id"):
        if col not in existing:
            try:
                tbl.create_scalar_index(col, replace=True)
            except Exception as err:  # a scalar index is an optimisation, never fatal
                log(f"index: scalar {col} skipped ({err})")
    rows = tbl.count_rows()
    if rows >= _VECTOR_INDEX_MIN_ROWS and "vector" not in existing:
        # ~sqrt(rows) partitions is Lance's rule of thumb; keep clusters populated so training the
        # IVF index doesn't degenerate (the empty-cluster warning) on a modest personal corpus.
        partitions = max(1, min(1024, int(rows ** 0.5)))
        try:
            tbl.create_index(metric="cosine", vector_column_name="vector",
                             num_partitions=partitions, replace=True)
        except Exception as err:
            log(f"index: vector index skipped ({err})")


def _index_meta(tbl) -> list:
    try:
        return list(tbl.list_indices())
    except Exception:
        return []


def chunks_dataset(ws):
    """The chunk table as a Lance dataset (via lancedb's `to_lance`), or None if nothing is indexed
    yet. DuckDB registers this directly and queries it as SQL — the single copy of the chunk data,
    so there is no separate chunk mirror to keep in sync."""
    tbl = _table(_db(ws))
    return tbl.to_lance() if tbl is not None else None


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
