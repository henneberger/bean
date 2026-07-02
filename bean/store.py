"""DuckDB catalog for a workspace: document snapshots, revision history, sync cursors.

Documents are the unit of sync — one row per Google Doc, one per Slack channel-week digest.
The body lives here (there is no file mirror); Lance holds the chunk vectors. The content
hash is the change authority: a revision bump whose text is identical updates metadata but
re-embeds nothing.
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
-- Mirror of the chunk metadata (also in Lance): powers the keyword half of hybrid search plus
-- the recent / thread / neighbours retrieval primitives, deterministically and offline.
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY, source TEXT NOT NULL, doc_id TEXT NOT NULL,
  title TEXT, url TEXT, ord INTEGER, start INTEGER, "end" INTEGER, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
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
        self.con = duckdb.connect(str(ws.db_path))
        self.con.execute(SCHEMA)
        # Migrate DBs created before the metadata columns existed (CREATE IF NOT EXISTS won't add them).
        for col, typ in (("created_at", "TIMESTAMP"), ("modified_at", "TIMESTAMP"),
                         ("author", "TEXT"), ("mime", "TEXT")):
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
        self.con.execute("DELETE FROM chunks WHERE source=? AND doc_id=?", [source, doc_id])

    def doc_ids(self, source: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT doc_id FROM documents WHERE source=? ORDER BY doc_id", [source]).fetchall()]

    def counts(self) -> dict[str, int]:
        return dict(self.con.execute(
            "SELECT source, count(*) FROM documents GROUP BY source").fetchall())

    # -- chunk mirror (keyword search + recent/thread/neighbours) --------------------------------
    def replace_chunks(self, source: str, doc_id: str, rows: list[dict]) -> None:
        self.con.execute("DELETE FROM chunks WHERE source=? AND doc_id=?", [source, doc_id])
        for i, r in enumerate(rows):
            self.con.execute(
                'INSERT INTO chunks (chunk_id, source, doc_id, title, url, ord, start, "end", text)'
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [r["id"], source, doc_id, r.get("title"), r.get("url"), i,
                 r.get("start"), r.get("end"), r["text"]],
            )

    def delete_chunks(self, source: str, doc_id: str) -> None:
        self.con.execute("DELETE FROM chunks WHERE source=? AND doc_id=?", [source, doc_id])

    def _rows(self, sql: str, params: list) -> list[dict]:
        cur = self.con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def keyword_search(self, query: str, *, k: int = 200, source: str | None = None,
                       doc_like: str | None = None) -> list[dict]:
        """Deterministic keyword ranking: score = distinct query terms present, + a phrase bonus.
        No fuzzy vectors — an exact identifier or error string lands its chunk every time."""
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
        sql = (f'SELECT chunk_id AS id, source, doc_id, title, url, ord, start, "end", text, '
               f"({score}) AS kw_score FROM chunks WHERE {' AND '.join(where)} "
               f"AND ({score}) > 0 ORDER BY kw_score DESC, doc_id, ord LIMIT ?")
        return self._rows(sql, score_params + params + score_params + [k])

    def chunk_by_id(self, chunk_id: str) -> dict | None:
        rows = self._rows('SELECT chunk_id AS id, source, doc_id, title, url, ord, start, "end", text '
                          "FROM chunks WHERE chunk_id=?", [chunk_id])
        return rows[0] if rows else None

    def neighbors(self, source: str, doc_id: str, ord: int, radius: int = 1) -> list[dict]:
        return self._rows(
            'SELECT chunk_id AS id, source, doc_id, title, url, ord, start, "end", text '
            "FROM chunks WHERE source=? AND doc_id=? AND ord BETWEEN ? AND ? ORDER BY ord",
            [source, doc_id, ord - radius, ord + radius])

    def recent(self, *, source: str | None = None, doc_like: str | None = None,
               limit: int = 20) -> list[dict]:
        """Most-recently-*modified* documents (by the doc's own timestamp, falling back to when
        bean fetched it when a source has none), newest first — 'what changed lately'."""
        where, params = ["1=1"], []
        if source:
            where.append("source = ?"); params.append(source)
        if doc_like:
            where.append("doc_id ILIKE ?"); params.append(f"%{doc_like}%")
        return self._rows(
            "SELECT source, doc_id, title, url, body, created_at, modified_at, author, mime, "
            "fetched_at FROM documents "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(modified_at, fetched_at) DESC, doc_id DESC LIMIT ?",
            params + [limit])

    def find_docs(self, *, source: str | None = None, doc_like: str | None = None,
                  limit: int = 20) -> list[dict]:
        where, params = ["1=1"], []
        if source:
            where.append("source = ?"); params.append(source)
        if doc_like:
            where.append("(doc_id ILIKE ? OR title ILIKE ?)"); params += [f"%{doc_like}%", f"%{doc_like}%"]
        return self._rows(
            "SELECT source, doc_id, title, url, body, created_at, modified_at, author, mime, "
            "fetched_at FROM documents "
            f"WHERE {' AND '.join(where)} ORDER BY COALESCE(modified_at, fetched_at) DESC LIMIT ?",
            params + [limit])

    def revisions(self, source: str, doc_id: str) -> list[tuple]:
        return self.con.execute(
            "SELECT revision_id, hash, fetched_at FROM revisions WHERE source=? AND doc_id=? ORDER BY fetched_at",
            [source, doc_id],
        ).fetchall()

    # -- state (sync cursors etc.; values are JSON) ----------------------------------------------
    def get_state(self, key: str, default=None):
        row = self.con.execute("SELECT value FROM state WHERE key=?", [key]).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        self.con.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            [key, json.dumps(value)],
        )
