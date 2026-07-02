"""Readwise source. Indexes Highlights (one doc per book/article, grouping its highlights) and,
best-effort, Reader documents. Auth is a Readwise access token (`Authorization: Token <token>`).
Change detection: a book's revision is the newest highlight timestamp across it; Reader docs use
their `updated_at`. Incremental via a stored `updatedAfter` cursor. Whole-collection source, so it
never prunes. Reader (v3) is tolerated when absent — accounts without Reader 4xx and are skipped."""

from __future__ import annotations

from urllib.parse import urlencode

from ..http import api_json, api_get
from ..store import Store
from ..html import html_to_text
from ..workspace import load_credential, save_credential

EXPORT = "https://readwise.io/api/v2/export/"
READER = "https://readwise.io/api/v3/list/"
CURSOR = "readwise.updated_after"


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    item = item.strip().lower()
    if item == "readwise:highlights":
        return ("tags", "highlights")
    if item == "readwise:reader":
        return ("tags", "reader")
    return None


def connect(*, token=None, fetch=None, log=print, **_ignored) -> dict:
    if not token:
        raise RuntimeError("pass --token … (get one at readwise.io/access_token).")
    res = api_get("https://readwise.io/api/v2/auth/", _headers(token), fetch=fetch)
    if not res.ok:
        raise RuntimeError(f"Readwise token rejected (HTTP {res.status}).")
    save_credential("readwise", {"token": token})
    log("✓ Readwise connected.")
    return {"ok": True}


def connected() -> dict | None:
    return load_credential("readwise")


def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("readwise")
    if not cred:
        raise RuntimeError("not connected — run `bean auth readwise --token …`.")
    headers = _headers(cred["token"])
    want = set(config.get("tags") or ["highlights", "reader"])
    since = None if full else store.get_state(CURSOR)
    changed = []

    if "highlights" in want:
        changed += _sync_highlights(store, headers, fetch, since, full, log)
    if "reader" in want:
        changed += _sync_reader(store, headers, fetch, since, full, log)

    store.set_state(CURSOR, _now_iso())
    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _paged(base_url: str, headers, fetch, cursor_field: str, first_params: dict):
    cursor, params = None, dict(first_params)
    while True:
        if cursor:
            params[cursor_field] = cursor
        resp = api_json(f"{base_url}?{urlencode({k: v for k, v in params.items() if v})}",
                        headers, fetch=fetch)
        yield resp
        cursor = resp.get("nextPageCursor")
        if not cursor:
            return


def _sync_highlights(store, headers, fetch, since, full, log) -> list[str]:
    changed = []
    for page in _paged(EXPORT, headers, fetch, "pageCursor", {"updatedAfter": since}):
        for book in page.get("results", []):
            bid = book.get("user_book_id") or book.get("id")
            if bid is None:
                continue
            doc_id = f"book/{bid}"
            hls = book.get("highlights", []) or []
            rev = book.get("last_highlight_at") or ""
            for h in hls:
                rev = max(rev, h.get("highlighted_at") or "", h.get("updated") or "")
            existing = store.get("readwise", doc_id)
            if not full and existing and rev and existing.revision_id == rev:
                continue
            lines = [f"# {book.get('title') or 'Untitled'}"]
            if book.get("author"):
                lines.append(f"by {book.get('author')}")
            lines.append("")
            for h in hls:
                if h.get("text"):
                    lines.append(f"- {h.get('text')}")
                if h.get("note"):
                    lines.append(f"  note: {h.get('note')}")
            body = "\n".join(lines)
            url = book.get("readwise_url") or book.get("source_url")
            if store.upsert("readwise", doc_id, title=book.get("title") or "Untitled",
                            url=url, revision_id=rev or None, body=body):
                changed.append(doc_id)
                log(f"readwise: updated {doc_id}")
    return changed


def _sync_reader(store, headers, fetch, since, full, log) -> list[str]:
    changed = []
    try:
        pages = list(_paged(READER, headers, fetch, "pageCursor", {"updatedAfter": since}))
    except RuntimeError as err:
        log(f"readwise: reader skipped ({err})")
        return changed
    for page in pages:
        for rec in page.get("results", []):
            rid = rec.get("id")
            if rid is None:
                continue
            doc_id = f"reader/{rid}"
            rev = rec.get("updated_at") or rec.get("last_moved_at")
            existing = store.get("readwise", doc_id)
            if not full and existing and rev and existing.revision_id == rev:
                continue
            title = rec.get("title") or "Untitled"
            parts = [f"# {title}", rec.get("summary") or "",
                     html_to_text(rec.get("content") or "")]
            body = "\n\n".join(x for x in parts if x)
            if store.upsert("readwise", doc_id, title=title, url=rec.get("url") or rec.get("source_url"),
                            revision_id=rev, body=body):
                changed.append(doc_id)
                log(f"readwise: updated {doc_id}")
    return changed


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
