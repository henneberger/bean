"""SQL database source. Indexes the rows returned by a user-supplied query as documents — one
document per row. The connection string (DSN) is the credential and lives in the workspace config,
not in ~/.bean/credentials, so there is no `bean auth` step. Supports sqlite (stdlib) and postgres
(via psycopg or psycopg2 when installed). No network fetch is used, so it is fully offline-testable.

Each tracked query is a dict {name, dsn, sql, id, title, url, body} where id/title/url/body name the
result columns to map (with fallbacks). doc_id is `{name}#{row_id}`; revision_id is None so the
content hash gates re-embedding. Rows that the query no longer returns are pruned, scoped to that
query's `{name}#` doc-id prefix."""

from __future__ import annotations

import hashlib
import re

_FROM_RE = re.compile(r"\bfrom\s+([\w.\"`]+)", re.I)


def parse_add(item: str):
    """`sql:{dsn}|{sql}` with an optional trailing `#name=...` → a query dict. Else → None.
    Example: `sql:sqlite:///abs/path.db|SELECT id,title,body,url FROM notes`."""
    s = item.strip()
    if not s.startswith("sql:"):
        return None
    rest = s[len("sql:"):]
    name = None
    m = re.search(r"#name=([\w.-]+)\s*$", rest)
    if m:
        name = m.group(1)
        rest = rest[:m.start()]
    if "|" not in rest:
        return None
    dsn, sql = rest.split("|", 1)
    dsn, sql = dsn.strip(), sql.strip()
    if not dsn or not sql:
        return None
    if not name:
        fm = _FROM_RE.search(sql)
        name = fm.group(1).strip('"`').split(".")[-1] if fm else \
            "q" + hashlib.sha1(sql.encode()).hexdigest()[:8]
    return ("queries", {"name": name, "dsn": dsn, "sql": sql,
                        "id": None, "title": None, "url": None, "body": None})


# -- drivers ------------------------------------------------------------------------------------
def _rows(dsn: str, sql: str) -> tuple[list[str], list[tuple]]:
    """Run `sql` against `dsn`; return (column_names, rows)."""
    if dsn.startswith("sqlite:"):
        import sqlite3
        path = dsn[len("sqlite://"):] if dsn.startswith("sqlite://") else dsn[len("sqlite:"):]
        con = sqlite3.connect(path)
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            return cols, cur.fetchall()
        finally:
            con.close()
    if dsn.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg  # psycopg 3
            connect = psycopg.connect
        except ImportError:
            try:
                import psycopg2
                connect = psycopg2.connect
            except ImportError:
                raise RuntimeError(
                    "postgres DSN needs the 'psycopg' (or 'psycopg2') driver — pip install psycopg")
        con = connect(dsn)
        try:
            cur = con.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return cols, cur.fetchall()
        finally:
            con.close()
    raise RuntimeError(f"unsupported DSN (expected sqlite:/postgres:): {dsn.split(':', 1)[0]}:")


def _pick(cols: list[str], configured: str | None, *defaults) -> str | None:
    """Resolve a mapped column: the configured name if valid, else the first default present."""
    if configured and configured in cols:
        return configured
    for d in defaults:
        if d and d in cols:
            return d
    return None


def sync(store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    changed, seen = [], []
    for q in config.get("queries", []):
        name = q.get("name")
        try:
            cols, rows = _rows(q["dsn"], q["sql"])
        except Exception as err:
            log(f"sqldb: query {name} skipped ({err})")
            continue
        id_col = _pick(cols, q.get("id"), "id") or (cols[0] if cols else None)
        title_col = _pick(cols, q.get("title"), "title")
        url_col = _pick(cols, q.get("url"), "url")
        body_col = _pick(cols, q.get("body"), "body")
        for row in rows:
            rec = dict(zip(cols, row))
            row_id = rec.get(id_col) if id_col else None
            if row_id is None:
                continue
            doc_id = f"{name}#{row_id}"
            seen.append(doc_id)
            title = str(rec[title_col]) if title_col and rec.get(title_col) is not None \
                else (name or str(row_id))
            url = str(rec[url_col]) if url_col and rec.get(url_col) is not None else None
            if body_col:
                body = str(rec.get(body_col) or "")
            else:  # render every column as "col: value" lines
                body = "\n".join(f"{c}: {rec.get(c)}" for c in cols)
            if store.upsert("sqldb", doc_id, title=title, url=url, revision_id=None, body=body):
                changed.append(doc_id)
                log(f"sqldb: updated {doc_id}")

    # Prune only within the query prefixes we own this run (leave other queries' docs alone).
    prefixes = tuple(f"{q.get('name')}#" for q in config.get("queries", []))
    removed = [d for d in store.doc_ids("sqldb")
               if d.startswith(prefixes) and d not in seen]
    for doc_id in removed:
        store.delete("sqldb", doc_id)
    return {"changed": changed, "removed": removed}
