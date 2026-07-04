"""Store: the document/revision/edge catalog for a workspace, backed by Lance (via `Catalog`)
for the shared relational data and a small private DuckDB for sync cursors (`state`).

Documents are the unit of sync — one row per Google Doc, one per Slack thread or message. The
body lives here; the content hash is the change authority (a revision bump whose text is identical
updates metadata but re-embeds nothing). Chunks live once in Lance (text + vectors); the keyword /
neighbour / merge queries here run as DuckDB SQL directly over that Lance dataset via `Catalog.duck()`
— so DuckDB stays the query engine without a duplicated chunk mirror. Writes to documents/revisions/
edges go through `Catalog`'s Lance ops (a DuckDB-registered Lance dataset is a read-only view).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import duckdb
import pyarrow as pa

from .lancecat import Catalog

STATE_SCHEMA = "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)"


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_ts(v):
    """Parse a source-native ISO8601 timestamp (as connectors hand it in `meta`) to a naive
    datetime for the Lance `documents` schema; already-parsed values pass through unchanged."""
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)


@dataclass
class Doc:
    source: str
    doc_id: str
    title: str
    url: str | None
    revision_id: str | None
    hash: str
    body: str
    created_at: object = None
    modified_at: object = None
    author: str | None = None
    mime: str | None = None


class Store:
    def __init__(self, ws):
        self.ws = ws  # needed to reach the Lance chunk dataset for the keyword/neighbour queries
        self.cat = Catalog(ws.catalog_dir)
        # `state` (sync cursors etc.) is the only thing that stays in a private local DuckDB;
        # documents/revisions/edges now live on the Catalog's Lance datasets.
        self._state = duckdb.connect(str(ws.db_path))
        self._state.execute(STATE_SCHEMA)
        # Private embed checkpoint (out of the shared catalog — see Task 1.4): holds the content
        # hash whose chunks are actually in Lance, per (source, doc_id), so a doc still needs
        # embedding whenever embedded_hash != hash (or is NULL) — survives an interrupted sync
        # and lets it resume without re-embedding what's already done.
        self._state.execute("CREATE TABLE IF NOT EXISTS embedded ("
                            "source TEXT, doc_id TEXT, embedded_hash TEXT, PRIMARY KEY (source, doc_id))")

    def close(self):
        self._state.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- documents -------------------------------------------------------------------------------
    def get(self, source: str, doc_id: str) -> Doc | None:
        con = self.cat.duck()
        try:
            row = con.execute(
                "SELECT source, doc_id, title, url, revision_id, hash, body, "
                "created_at, modified_at, author, mime FROM documents WHERE source=? AND doc_id=?",
                [source, doc_id],
            ).fetchone()
        finally:
            con.close()
        return Doc(*row) if row else None

    def upsert(self, source: str, doc_id: str, *, title: str, url: str | None,
               revision_id: str | None, body: str, meta: dict | None = None) -> bool:
        """Insert or update a snapshot. Returns True when the CONTENT changed (re-embed needed).
        `meta` carries source-native fields: created_at, modified_at, author, mime (all optional)."""
        h = content_hash(body)
        m = meta or {}
        created_at, modified_at = _to_ts(m.get("created_at")), _to_ts(m.get("modified_at"))
        author, mime = m.get("author"), m.get("mime")
        now = _now()
        con = self.cat.duck()
        try:
            existing = con.execute(
                "SELECT hash FROM documents WHERE source=? AND doc_id=?",
                [source, doc_id]).fetchone()
        finally:
            con.close()
        row = {"source": source, "doc_id": doc_id, "title": title, "url": url,
               "revision_id": revision_id, "hash": h, "body": body,
               "created_at": created_at, "modified_at": modified_at, "author": author,
               "mime": mime, "fetched_at": now}
        if existing and existing[0] == h:
            self.cat.upsert_documents([row])  # metadata-only change: embed checkpoint untouched
            return False
        self.cat.upsert_documents([row])
        self.cat.append_revisions([{"source": source, "doc_id": doc_id,
                                    "revision_id": revision_id, "hash": h, "fetched_at": now}])
        return True

    def delete(self, source: str, doc_id: str) -> None:
        self.cat.delete_documents(source, [doc_id])
        self.cat.replace_edges(source, doc_id, [])
        # Chunk vectors live in Lance; index.delete_doc removes them (called by the sync/scope paths).

    def doc_ids(self, source: str) -> list[str]:
        con = self.cat.duck()
        try:
            return [r[0] for r in con.execute(
                "SELECT doc_id FROM documents WHERE source=? ORDER BY doc_id", [source]).fetchall()]
        finally:
            con.close()

    # -- embed checkpoint (sync resumability) ----------------------------------------------------
    def embed_queue(self, sources=None, *, force: bool = False) -> list[tuple[str, str]]:
        """(source, doc_id) pairs that still need embedding, OLDEST FIRST (by the doc's own
        timestamp) so a checkpoint means 'everything older is done'. `force` (a --rebuild) returns
        every doc; otherwise only docs whose embedded chunks are missing or stale (private
        embedded_hash != hash) — which naturally includes anything an interrupted sync left
        half-done. The checkpoint lives in the private local DuckDB (`embedded`, on `self._state`)
        joined here against the shared catalog's `documents`. Note: ATTACH-ing `ws.db_path` (the
        private DuckDB file) onto a *second* connection doesn't work here — `self._state` already
        holds that same file open read-write in this process, and DuckDB refuses a second handle
        onto one database file ("Unique file handle conflict"). So instead we register the Lance
        `documents` dataset as a view directly on `self._state` (registering an external dataset is
        unrelated to ATTACH-ing a database file) and join against the local `embedded` table there."""
        where = ["1=1"]
        params: list = []
        if sources is not None:
            marks = ",".join("?" * len(sources)) or "NULL"
            where.append(f"d.source IN ({marks})")
            params += list(sources)
        join_pred = "" if force else \
            "AND (e.embedded_hash IS NULL OR e.embedded_hash <> d.hash)"
        t = self.cat._table("documents")
        lance_view = t.to_lance() if t is not None else \
            pa.Table.from_pylist([], schema=Catalog.SCHEMAS["documents"])
        self._state.register("_cat_documents", lance_view)
        try:
            rows = self._state.execute(
                f"SELECT d.source, d.doc_id FROM _cat_documents d "
                f"LEFT JOIN embedded e ON d.source=e.source AND d.doc_id=e.doc_id "
                f"WHERE {' AND '.join(where)} {join_pred} "
                "ORDER BY COALESCE(d.modified_at, d.fetched_at) ASC, d.doc_id ASC", params).fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            self._state.unregister("_cat_documents")

    def mark_embedded(self, source: str, doc_id: str) -> None:
        """Checkpoint one doc: its current content is now embedded in Lance. Reads the doc's current
        hash and upserts it into the private `embedded` table — durable immediately, so an
        interrupted sync resumes from exactly here."""
        d = self.get(source, doc_id)
        if d is None:
            return
        self._state.execute(
            "INSERT INTO embedded (source, doc_id, embedded_hash) VALUES (?, ?, ?) "
            "ON CONFLICT (source, doc_id) DO UPDATE SET embedded_hash = excluded.embedded_hash",
            [source, doc_id, d.hash])

    def counts(self) -> dict[str, int]:
        con = self.cat.duck()
        try:
            return dict(con.execute(
                "SELECT source, count(*) FROM documents GROUP BY source").fetchall())
        finally:
            con.close()

    def _rows(self, sql: str, params: list) -> list[dict]:
        con = self.cat.duck()
        try:
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            con.close()

    # -- chunk queries: DuckDB SQL run directly over the single Lance chunk dataset ----------------
    # `ord` (a chunk's 0-based position within its document, among base chunks) is stored at embed
    # time by `reindex_doc`; the coarse doc-level "…-large" chunks carry `ord = NULL` and are
    # excluded here so numbering matches the base chunks.
    _BASE = ("SELECT id, source, doc_id, title, url, start, \"end\", text, ord "
             "FROM _chunks WHERE id NOT LIKE '%-large'")

    def _chunk_rows(self, select_sql: str, params: list) -> list[dict]:
        """Run `select_sql` (which reads from the `base` CTE) against the Lance chunk dataset."""
        from .index import chunks_dataset
        ds = chunks_dataset(self.ws)
        if ds is None:
            return []
        con = self.cat.duck()
        try:
            con.register("_chunks", ds)
            cur = con.execute(f"WITH base AS ({self._BASE}) {select_sql}", params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            con.close()

    def keyword_search(self, query: str, *, k: int = 200, source: str | None = None,
                       doc_like: str | None = None) -> list[dict]:
        """Deterministic keyword ranking over the Lance chunks: score = distinct query terms present,
        + a phrase bonus. No fuzzy vectors — an exact identifier or error string lands every time."""
        import re
        terms = list(dict.fromkeys(re.findall(r"[\w#/.-]{2,}", query.lower())))
        if not terms:
            return []
        where = ["1=1"]
        params: list = []
        if source:
            where.append("source = ?"); params.append(source)
        if doc_like:
            where.append("doc_id ILIKE ?"); params.append(f"%{doc_like}%")
        score = " + ".join(["(CASE WHEN lower(text) LIKE ? THEN 1 ELSE 0 END)"] * len(terms))
        score += " + (CASE WHEN lower(text) LIKE ? THEN 2 ELSE 0 END)"  # whole-phrase bonus
        score_params = [f"%{t}%" for t in terms] + [f"%{query.lower()}%"]
        sql = (f'SELECT id, source, doc_id, title, url, ord, start, "end", text, ({score}) AS kw_score '
               f"FROM base WHERE {' AND '.join(where)} AND ({score}) > 0 "
               f"ORDER BY kw_score DESC, doc_id, ord LIMIT ?")
        return self._chunk_rows(sql, score_params + params + score_params + [k])

    def chunk_by_id(self, chunk_id: str) -> dict | None:
        rows = self._chunk_rows(
            'SELECT id, source, doc_id, title, url, ord, start, "end", text FROM base WHERE id=?',
            [chunk_id])
        return rows[0] if rows else None

    def neighbors(self, source: str, doc_id: str, ord: int, radius: int = 1) -> list[dict]:
        return self._chunk_rows(
            'SELECT id, source, doc_id, title, url, ord, start, "end", text FROM base '
            "WHERE source=? AND doc_id=? AND ord BETWEEN ? AND ? ORDER BY ord",
            [source, doc_id, ord - radius, ord + radius])

    def _meta_filters(self, where: list, params: list, author, since, before) -> None:
        if author:
            where.append("author ILIKE ?"); params.append(f"%{author}%")
        if since:
            where.append("COALESCE(modified_at, fetched_at) >= ?"); params.append(since)
        if before:
            where.append("COALESCE(modified_at, fetched_at) < ?"); params.append(before)

    def _find_docs(self, *, source, doc_like, author, since, before, limit, tiebreak: str):
        """Documents ordered newest-first by their own timestamp (fetch time when a source has none),
        with the given filters. `tiebreak` is appended to the ORDER BY for a stable secondary sort."""
        where, params = ["1=1"], []
        if source:
            where.append("source = ?"); params.append(source)
        if doc_like:  # match id OR title so "on my <doc name>" reaches comments keyed by an opaque id
            where.append("(doc_id ILIKE ? OR title ILIKE ?)"); params += [f"%{doc_like}%", f"%{doc_like}%"]
        self._meta_filters(where, params, author, since, before)
        return self._rows(
            "SELECT source, doc_id, title, url, body, created_at, modified_at, author, mime, "
            "fetched_at FROM documents "
            f"WHERE {' AND '.join(where)} ORDER BY COALESCE(modified_at, fetched_at) DESC{tiebreak} "
            "LIMIT ?", params + [limit])

    def recent(self, *, source: str | None = None, doc_like: str | None = None,
               author: str | None = None, since=None, before=None, limit: int = 20) -> list[dict]:
        """Most-recently-*modified* documents (by the doc's own timestamp, falling back to when
        bean fetched it when a source has none), newest first — 'what changed lately'. Optional
        author / since / before narrow to who and when."""
        return self._find_docs(source=source, doc_like=doc_like, author=author, since=since,
                               before=before, limit=limit, tiebreak=", doc_id DESC")

    def find_docs(self, *, source: str | None = None, doc_like: str | None = None,
                  author: str | None = None, since=None, before=None, limit: int = 20) -> list[dict]:
        return self._find_docs(source=source, doc_like=doc_like, author=author, since=since,
                               before=before, limit=limit, tiebreak="")

    def doc_meta_map(self, pairs) -> dict:
        """{(source, doc_id): {"author", "modified_at"}} for filtering fused search hits."""
        by_src: dict = {}
        for s, d in dict.fromkeys(pairs):
            by_src.setdefault(s, []).append(d)
        out: dict = {}
        con = self.cat.duck()
        try:
            for s, ids in by_src.items():
                ph = ",".join("?" * len(ids))
                for did, author, mod in con.execute(
                        f"SELECT doc_id, author, modified_at FROM documents "
                        f"WHERE source=? AND doc_id IN ({ph})", [s, *ids]).fetchall():
                    out[(s, did)] = {"author": author, "modified_at": mod}
        finally:
            con.close()
        return out

    def revisions(self, source: str, doc_id: str) -> list[tuple]:
        con = self.cat.duck()
        try:
            return con.execute(
                "SELECT revision_id, hash, fetched_at FROM revisions WHERE source=? AND doc_id=? ORDER BY fetched_at",
                [source, doc_id],
            ).fetchall()
        finally:
            con.close()

    # -- edges (lightweight relationship graph) --------------------------------------------------
    def replace_edges(self, source: str, src_doc: str, rows: list[dict]) -> None:
        self.cat.replace_edges(source, src_doc, rows)

    def edges_of(self, source: str, doc_id: str) -> list[dict]:
        return self._rows("SELECT rel, dst_kind, dst FROM edges WHERE source=? AND src_doc=?",
                          [source, doc_id])

    def related(self, source: str, doc_id: str, *, limit: int = 20) -> list[dict]:
        """Documents one hop away in the edge graph, each with the connecting `reason`:
        docs sharing a container/person target with this doc, docs it links to, docs linking to it."""
        mine = self.edges_of(source, doc_id)
        cand: dict = {}  # (source, doc_id) -> reason string

        def add(s, d, reason):
            if (s, d) != (source, doc_id):
                cand.setdefault((s, d), reason)

        for e in mine:
            kind, dst, rel = e["dst_kind"], e["dst"], e["rel"]
            if kind == "link":
                add(source, dst, f"linked from this ({rel})")
            else:  # container / person: other docs pointing at the same target
                for r in self._rows(
                        "SELECT source, src_doc FROM edges WHERE dst_kind=? AND dst=? AND rel=?",
                        [kind, dst, rel]):
                    add(r["source"], r["src_doc"], f"same {kind}: {dst}")
        # docs that link TO this one
        for r in self._rows(
                "SELECT source, src_doc, rel FROM edges WHERE dst_kind='link' AND dst=? AND source=?",
                [doc_id, source]):
            add(r["source"], r["src_doc"], f"links to this ({r['rel']})")

        if not cand:
            return []
        out: list[dict] = []
        for (s, d), reason in cand.items():
            doc = self.get(s, d)
            if doc is None:
                continue
            out.append({"source": s, "doc_id": d, "title": doc.title, "url": doc.url,
                        "body": doc.body, "modified_at": doc.modified_at, "author": doc.author,
                        "reason": reason})
        out.sort(key=lambda r: (r.get("modified_at") is None, r.get("modified_at")), reverse=True)
        return out[:limit]

    # -- state (sync cursors etc.; values are JSON; small private local DuckDB) -------------------
    def get_state(self, key: str, default=None):
        row = self._state.execute("SELECT value FROM state WHERE key=?", [key]).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        self._state.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            [key, json.dumps(value)],
        )
