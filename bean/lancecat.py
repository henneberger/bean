"""Lance-backed catalog: the four shared datasets (documents, revisions, edges, chunks) as Lance
tables under one directory, plus a DuckDB read connection that registers them as views so every
existing SQL query and `bean sql` runs unchanged. Writes are immutable Lance ops (merge_insert /
delete-predicate + add / append). This is the storage substrate `Store` sits on."""
from __future__ import annotations
from pathlib import Path
import duckdb
import lancedb
import pyarrow as pa

_TS = pa.timestamp("us")
SCHEMAS = {
    "documents": pa.schema([("source", pa.string()), ("doc_id", pa.string()), ("title", pa.string()),
        ("url", pa.string()), ("revision_id", pa.string()), ("hash", pa.string()), ("body", pa.string()),
        ("created_at", _TS), ("modified_at", _TS), ("author", pa.string()), ("mime", pa.string()),
        ("fetched_at", _TS)]),
    "revisions": pa.schema([("source", pa.string()), ("doc_id", pa.string()),
        ("revision_id", pa.string()), ("hash", pa.string()), ("fetched_at", _TS)]),
    "edges": pa.schema([("source", pa.string()), ("src_doc", pa.string()), ("rel", pa.string()),
        ("dst_kind", pa.string()), ("dst", pa.string())]),
    # chunks vector width is set on first real write (embedding-model dependent); created lazily.
}

def _esc(s: str) -> str:
    return str(s).replace("'", "''")

class Catalog:
    SCHEMAS = SCHEMAS

    def __init__(self, root: Path = None, *, remote_uri: str = None, storage_options: dict = None):
        if (root is None) == (remote_uri is None):
            raise ValueError("Catalog needs exactly one of root or remote_uri")
        if remote_uri is not None:
            self.root = None
            self.db = lancedb.connect(remote_uri, storage_options=storage_options or {})
        else:
            self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
            self.db = lancedb.connect(str(self.root))

    def _table(self, name):
        return self.db.open_table(name) if name in self.db.table_names() else None

    def _ensure(self, name):
        t = self._table(name)
        if t is None and name in SCHEMAS:
            t = self.db.create_table(name, schema=SCHEMAS[name])
        return t

    def upsert_documents(self, rows):
        if not rows: return
        t = self._ensure("documents")
        (t.merge_insert(["source", "doc_id"])
          .when_matched_update_all().when_not_matched_insert_all()
          .execute(pa.Table.from_pylist(rows, schema=SCHEMAS["documents"])))

    def delete_documents(self, source, doc_ids):
        t = self._table("documents")
        if t is None or not doc_ids: return
        ids = ",".join(f"'{_esc(d)}'" for d in doc_ids)
        t.delete(f"source = '{_esc(source)}' AND doc_id IN ({ids})")

    def append_revisions(self, rows):
        if not rows: return
        self._ensure("revisions").add(pa.Table.from_pylist(rows, schema=SCHEMAS["revisions"]))

    def delete_revisions(self, keys):
        """Delete existing revision rows for the given (source, doc_id) pairs. `append_revisions`
        is a raw append with no dedup; calling this first makes a subsequent append idempotent
        under retry (e.g. a migration re-run after a crash)."""
        t = self._table("revisions")
        if t is None or not keys: return
        pred = " OR ".join(f"(source = '{_esc(s)}' AND doc_id = '{_esc(d)}')" for s, d in keys)
        t.delete(pred)

    def replace_edges(self, source, src_doc, rows):
        t = self._ensure("edges")
        t.delete(f"source = '{_esc(source)}' AND src_doc = '{_esc(src_doc)}'")
        if rows:
            t.add(pa.Table.from_pylist([{"source": source, "src_doc": src_doc, **r} for r in rows],
                                       schema=SCHEMAS["edges"]))

    def replace_chunks(self, source, doc_id, rows):
        t = self._table("chunks")
        if t is None and rows:
            t = self.db.create_table("chunks", rows)  # width from the first vector
        elif t is not None:
            t.delete(f"source = '{_esc(source)}' AND doc_id = '{_esc(doc_id)}'")
            if rows: t.add(rows)

    def duck(self):
        con = duckdb.connect()
        for name in ("documents", "revisions", "edges", "chunks"):
            t = self._table(name)
            if t is not None:
                con.register(name, t.to_lance())
            elif name in SCHEMAS:  # empty typed view so queries don't explode pre-first-write
                con.register(name, pa.Table.from_pylist([], schema=SCHEMAS[name]))
        return con
