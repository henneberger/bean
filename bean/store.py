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
  fetched_at TIMESTAMP DEFAULT now(),
  PRIMARY KEY (source, doc_id)
);
CREATE TABLE IF NOT EXISTS revisions (
  source TEXT NOT NULL, doc_id TEXT NOT NULL, revision_id TEXT,
  hash TEXT NOT NULL, fetched_at TIMESTAMP DEFAULT now()
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


class Store:
    def __init__(self, ws):
        self.con = duckdb.connect(str(ws.db_path))
        self.con.execute(SCHEMA)

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- documents -------------------------------------------------------------------------------
    def get(self, source: str, doc_id: str) -> Doc | None:
        row = self.con.execute(
            "SELECT source, doc_id, title, url, revision_id, hash, body FROM documents WHERE source=? AND doc_id=?",
            [source, doc_id],
        ).fetchone()
        return Doc(*row) if row else None

    def upsert(self, source: str, doc_id: str, *, title: str, url: str | None,
               revision_id: str | None, body: str) -> bool:
        """Insert or update a snapshot. Returns True when the CONTENT changed (re-embed needed)."""
        h = content_hash(body)
        existing = self.get(source, doc_id)
        if existing and existing.hash == h:
            self.con.execute(
                "UPDATE documents SET title=?, url=?, revision_id=?, fetched_at=now() WHERE source=? AND doc_id=?",
                [title, url, revision_id, source, doc_id],
            )
            return False
        self.con.execute(
            """INSERT INTO documents (source, doc_id, title, url, revision_id, hash, body)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, doc_id) DO UPDATE SET
                 title=excluded.title, url=excluded.url, revision_id=excluded.revision_id,
                 hash=excluded.hash, body=excluded.body, fetched_at=now()""",
            [source, doc_id, title, url, revision_id, h, body],
        )
        self.con.execute(
            "INSERT INTO revisions (source, doc_id, revision_id, hash) VALUES (?, ?, ?, ?)",
            [source, doc_id, revision_id, h],
        )
        return True

    def delete(self, source: str, doc_id: str) -> None:
        self.con.execute("DELETE FROM documents WHERE source=? AND doc_id=?", [source, doc_id])

    def doc_ids(self, source: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT doc_id FROM documents WHERE source=? ORDER BY doc_id", [source]).fetchall()]

    def counts(self) -> dict[str, int]:
        return dict(self.con.execute(
            "SELECT source, count(*) FROM documents GROUP BY source").fetchall())

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
