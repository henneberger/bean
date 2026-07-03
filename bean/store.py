"""DuckDB catalog for a workspace: document snapshots, revision history, sync cursors, and the
relationship-edge graph.

Documents are the unit of sync — one row per Google Doc, one per Slack thread or message. The
body lives here; the content hash is the change authority (a revision bump whose text is identical
updates metadata but re-embeds nothing). Chunks live once in Lance (text + vectors); the keyword /
neighbour / merge queries here run as DuckDB SQL directly over that Lance dataset (register it on the
connection, then query) — so DuckDB stays the relational engine without a duplicated chunk mirror.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  source TEXT NOT NULL, doc_id TEXT NOT NULL, title TEXT, url TEXT,
  revision_id TEXT, hash TEXT NOT NULL, body TEXT NOT NULL,
  -- Source-native metadata (nullable; each connector fills what it has). created_at/modified_at
  -- are the document's OWN timestamps at the source, distinct from fetched_at (when bean synced).
  created_at TIMESTAMP, modified_at TIMESTAMP, author TEXT, mime TEXT,
  fetched_at TIMESTAMP DEFAULT now(),
  PRIMARY KEY (source, doc_id)
);
CREATE TABLE IF NOT EXISTS revisions (
  source TEXT NOT NULL, doc_id TEXT NOT NULL, revision_id TEXT,
  hash TEXT NOT NULL, fetched_at TIMESTAMP DEFAULT now()
);
-- No chunk table here: chunks live once in Lance. The keyword / neighbour / merge queries below run
-- as DuckDB SQL directly over the Lance dataset (register + query), so there is a single copy of the
-- chunk data and DuckDB stays the relational engine. Chunk `ord` is derived on the fly from line
-- order (large coarse chunks, id '…-large', are excluded), so no rebuild is needed to adopt this.
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
-- Lightweight relationship index: edges derived at sync time from source-native metadata
-- (authored_by → a person, in-container → a repo/project/channel, links-to → another doc). No LLM;
-- powers `bean related` and graph-expansion. dst_kind ∈ {doc, container, person, link}.
CREATE TABLE IF NOT EXISTS edges (
  source TEXT NOT NULL, src_doc TEXT NOT NULL, rel TEXT NOT NULL,
  dst_kind TEXT NOT NULL, dst TEXT NOT NULL,
  PRIMARY KEY (source, src_doc, rel, dst_kind, dst)
);
"""


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


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
        self.con = duckdb.connect(str(ws.db_path))
        self.con.execute(SCHEMA)
        # Migrate DBs created before the metadata columns existed (CREATE IF NOT EXISTS won't add them).
        # `embedded_hash` is the sync checkpoint: it holds the content hash whose chunks are actually
        # in Lance, so a doc still needs embedding whenever embedded_hash != hash (or is NULL) — which
        # survives an interrupted sync and lets it resume without re-embedding what's already done.
        for col, typ in (("created_at", "TIMESTAMP"), ("modified_at", "TIMESTAMP"),
                         ("author", "TEXT"), ("mime", "TEXT"), ("embedded_hash", "TEXT")):
            self.con.execute(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {col} {typ}")

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- documents -------------------------------------------------------------------------------
    def get(self, source: str, doc_id: str) -> Doc | None:
        row = self.con.execute(
            "SELECT source, doc_id, title, url, revision_id, hash, body, "
            "created_at, modified_at, author, mime FROM documents WHERE source=? AND doc_id=?",
            [source, doc_id],
        ).fetchone()
        return Doc(*row) if row else None

    def upsert(self, source: str, doc_id: str, *, title: str, url: str | None,
               revision_id: str | None, body: str, meta: dict | None = None) -> bool:
        """Insert or update a snapshot. Returns True when the CONTENT changed (re-embed needed).
        `meta` carries source-native fields: created_at, modified_at, author, mime (all optional)."""
        h = content_hash(body)
        m = meta or {}
        md = [m.get("created_at"), m.get("modified_at"), m.get("author"), m.get("mime")]
        existing = self.get(source, doc_id)
        if existing and existing.hash == h:
            self.con.execute(
                "UPDATE documents SET title=?, url=?, revision_id=?, "
                "created_at=?, modified_at=?, author=?, mime=?, fetched_at=now() "
                "WHERE source=? AND doc_id=?",
                [title, url, revision_id, *md, source, doc_id],
            )
            return False
        self.con.execute(
            """INSERT INTO documents (source, doc_id, title, url, revision_id, hash, body,
                                      created_at, modified_at, author, mime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, doc_id) DO UPDATE SET
                 title=excluded.title, url=excluded.url, revision_id=excluded.revision_id,
                 hash=excluded.hash, body=excluded.body, created_at=excluded.created_at,
                 modified_at=excluded.modified_at, author=excluded.author, mime=excluded.mime,
                 fetched_at=now()""",
            [source, doc_id, title, url, revision_id, h, body, *md],
        )
        self.con.execute(
            "INSERT INTO revisions (source, doc_id, revision_id, hash) VALUES (?, ?, ?, ?)",
            [source, doc_id, revision_id, h],
        )
        return True

    def delete(self, source: str, doc_id: str) -> None:
        self.con.execute("DELETE FROM documents WHERE source=? AND doc_id=?", [source, doc_id])
        self.con.execute("DELETE FROM edges WHERE source=? AND src_doc=?", [source, doc_id])
        # Chunk vectors live in Lance; index.delete_doc removes them (called by the sync/scope paths).

    def modified_map(self, pairs) -> dict:
        """{(source, doc_id): modified_at} for a set of hits — the recency signal, one query."""
        rows = list(dict.fromkeys((s, d) for s, d in pairs))
        if not rows:
            return {}
        out: dict = {}
        # DuckDB has no easy tuple-IN; group by source and filter doc_ids per source.
        by_src: dict = {}
        for s, d in rows:
            by_src.setdefault(s, []).append(d)
        for s, ids in by_src.items():
            ph = ",".join("?" * len(ids))
            for did, mod in self.con.execute(
                    f"SELECT doc_id, modified_at FROM documents WHERE source=? AND doc_id IN ({ph})",
                    [s, *ids]).fetchall():
                out[(s, did)] = mod
        return out

    def doc_ids(self, source: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT doc_id FROM documents WHERE source=? ORDER BY doc_id", [source]).fetchall()]

    # -- embed checkpoint (sync resumability) ----------------------------------------------------
    def embed_queue(self, sources=None, *, force: bool = False) -> list[tuple[str, str]]:
        """(source, doc_id) pairs that still need embedding, OLDEST FIRST (by the doc's own
        timestamp) so a checkpoint means 'everything older is done'. `force` (a --rebuild) returns
        every doc; otherwise only docs whose embedded chunks are missing or stale (embedded_hash !=
        hash) — which naturally includes anything an interrupted sync left half-done."""
        where = ["1=1"]
        params: list = []
        if sources is not None:
            marks = ",".join("?" * len(sources)) or "NULL"
            where.append(f"source IN ({marks})")
            params += list(sources)
        if not force:
            where.append("(embedded_hash IS NULL OR embedded_hash <> hash)")
        return [(r[0], r[1]) for r in self.con.execute(
            f"SELECT source, doc_id FROM documents WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(modified_at, fetched_at) ASC, doc_id ASC", params).fetchall()]

    def mark_embedded(self, source: str, doc_id: str) -> None:
        """Checkpoint one doc: its current content is now embedded in Lance. Autocommitted, so an
        interrupted sync resumes from exactly here."""
        self.con.execute(
            "UPDATE documents SET embedded_hash = hash WHERE source=? AND doc_id=?", [source, doc_id])

    def counts(self) -> dict[str, int]:
        return dict(self.con.execute(
            "SELECT source, count(*) FROM documents GROUP BY source").fetchall())

    def _rows(self, sql: str, params: list) -> list[dict]:
        cur = self.con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # -- chunk queries: DuckDB SQL run directly over the single Lance chunk dataset ----------------
    # `ord` (a chunk's position within its document) is derived from line order; the coarse
    # doc-level "…-large" chunks are excluded so numbering matches the base chunks.
    _BASE = ("SELECT id, source, doc_id, title, url, start, \"end\", text, "
             "row_number() OVER (PARTITION BY source, doc_id ORDER BY start) - 1 AS ord "
             "FROM _chunks WHERE id NOT LIKE '%-large'")

    def _chunk_rows(self, select_sql: str, params: list) -> list[dict]:
        """Run `select_sql` (which reads from the `base` CTE) against the Lance chunk dataset."""
        from .index import chunks_dataset
        ds = chunks_dataset(self.ws)
        if ds is None:
            return []
        self.con.register("_chunks", ds)
        try:
            return self._rows(f"WITH base AS ({self._BASE}) {select_sql}", params)
        finally:
            self.con.unregister("_chunks")

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

    def recent(self, *, source: str | None = None, doc_like: str | None = None,
               author: str | None = None, since=None, before=None, limit: int = 20) -> list[dict]:
        """Most-recently-*modified* documents (by the doc's own timestamp, falling back to when
        bean fetched it when a source has none), newest first — 'what changed lately'. Optional
        author / since / before narrow to who and when."""
        where, params = ["1=1"], []
        if source:
            where.append("source = ?"); params.append(source)
        if doc_like:  # match id OR title so "on my <doc name>" reaches comments keyed by an opaque id
            where.append("(doc_id ILIKE ? OR title ILIKE ?)"); params += [f"%{doc_like}%", f"%{doc_like}%"]
        self._meta_filters(where, params, author, since, before)
        return self._rows(
            "SELECT source, doc_id, title, url, body, created_at, modified_at, author, mime, "
            "fetched_at FROM documents "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(modified_at, fetched_at) DESC, doc_id DESC LIMIT ?",
            params + [limit])

    def find_docs(self, *, source: str | None = None, doc_like: str | None = None,
                  author: str | None = None, since=None, before=None, limit: int = 20) -> list[dict]:
        where, params = ["1=1"], []
        if source:
            where.append("source = ?"); params.append(source)
        if doc_like:
            where.append("(doc_id ILIKE ? OR title ILIKE ?)"); params += [f"%{doc_like}%", f"%{doc_like}%"]
        self._meta_filters(where, params, author, since, before)
        return self._rows(
            "SELECT source, doc_id, title, url, body, created_at, modified_at, author, mime, "
            "fetched_at FROM documents "
            f"WHERE {' AND '.join(where)} ORDER BY COALESCE(modified_at, fetched_at) DESC LIMIT ?",
            params + [limit])

    def doc_meta_map(self, pairs) -> dict:
        """{(source, doc_id): {"author", "modified_at"}} for filtering fused search hits."""
        by_src: dict = {}
        for s, d in dict.fromkeys(pairs):
            by_src.setdefault(s, []).append(d)
        out: dict = {}
        for s, ids in by_src.items():
            ph = ",".join("?" * len(ids))
            for did, author, mod in self.con.execute(
                    f"SELECT doc_id, author, modified_at FROM documents "
                    f"WHERE source=? AND doc_id IN ({ph})", [s, *ids]).fetchall():
                out[(s, did)] = {"author": author, "modified_at": mod}
        return out

    def revisions(self, source: str, doc_id: str) -> list[tuple]:
        return self.con.execute(
            "SELECT revision_id, hash, fetched_at FROM revisions WHERE source=? AND doc_id=? ORDER BY fetched_at",
            [source, doc_id],
        ).fetchall()

    # -- edges (lightweight relationship graph) --------------------------------------------------
    def replace_edges(self, source: str, src_doc: str, rows: list[dict]) -> None:
        self.con.execute("DELETE FROM edges WHERE source=? AND src_doc=?", [source, src_doc])
        for r in rows:
            self.con.execute(
                "INSERT INTO edges (source, src_doc, rel, dst_kind, dst) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                [source, src_doc, r["rel"], r["dst_kind"], str(r["dst"])])

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

    # -- state (sync cursors etc.; values are JSON) ----------------------------------------------
    def get_state(self, key: str, default=None):
        row = self.con.execute("SELECT value FROM state WHERE key=?", [key]).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        self.con.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            [key, json.dumps(value)],
        )
