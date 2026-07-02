"""BookStack source. Indexes every page in a self-hosted BookStack (a whole-collection source)
as one doc, flattening the page HTML to text. Auth is a personal API token pair (id + secret)
sent as `Authorization: Token {id}:{secret}` against a base --url. Pages are listed via
`/api/pages` (count/offset paging); each page's HTML comes from `/api/pages/{id}`. doc_id is
`page/{id}` and the revision id is `updated_at`, so unchanged pages re-embed nothing. Optionally
restrict to `books` (by id); never prunes (the instance is the collection)."""

from __future__ import annotations

from urllib.parse import urlencode

from ..http import api_json, api_get
from ..store import Store
from ..html import html_to_text
from ..workspace import load_credential, save_credential

CRED = "bookstack"


# -- refs + auth --------------------------------------------------------------------------------
def _headers(cred: dict) -> dict:
    return {"Authorization": f"Token {cred['id']}:{cred['secret']}",
            "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("bookstack:book:"):
        bid = s.split(":", 2)[2]
        return ("books", bid) if bid else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not url or not key or not secret:
        raise RuntimeError(
            "pass --url https://your.bookstack --key <token id> --secret <token secret> "
            "(BookStack → Edit Profile → API Tokens).")
    base = url.rstrip("/")
    cred = {"url": base, "id": key, "secret": secret}
    res = api_get(f"{base}/api/books?{urlencode({'count': 1})}", _headers(cred), fetch=fetch)
    if not res.ok:
        raise RuntimeError(f"BookStack rejected the credentials (HTTP {res.status}).")
    save_credential(CRED, cred)
    log(f"✓ BookStack connected ({base}).")
    return cred


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth bookstack --url … --key … --secret …`.")
    headers = _headers(cred)
    base = cred["url"].rstrip("/")
    want = {str(b) for b in config.get("books", [])}

    changed, offset = [], 0
    while True:
        q = urlencode({"count": 100, "offset": offset, "sort": "+id"})
        page = api_json(f"{base}/api/pages?{q}", headers, fetch=fetch)
        batch = page.get("data") or []
        for row in batch:
            try:
                _ingest(store, base, row, headers, fetch, want, changed, full, log)
            except Exception as err:
                log(f"bookstack: page {row.get('id')} skipped ({err})")
        offset += len(batch)
        if len(batch) < 100:
            break

    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _ingest(store, base, row, headers, fetch, want, changed, full, log):
    if want and str(row.get("book_id")) not in want:
        return
    pid = str(row.get("id"))
    doc_id = f"page/{pid}"
    rev = str(row.get("updated_at") or "")
    existing = store.get(CRED, doc_id)
    if not full and existing and rev and existing.revision_id == rev:
        return
    detail = api_json(f"{base}/api/pages/{pid}", headers, fetch=fetch)
    title = detail.get("name") or row.get("name") or "Untitled"
    body = f"# {title}\n\n" + html_to_text(detail.get("html") or "")
    url = f"{base}/books/{row.get('book_slug') or row.get('book_id')}/page/{detail.get('slug') or pid}"
    meta = {"modified_at": row.get("updated_at"), "created_at": row.get("created_at")}
    if store.upsert(CRED, doc_id, title=title, url=url, revision_id=rev or None,
                    body=body, meta=meta):
        changed.append(doc_id)
        log(f"bookstack: updated \"{title}\"")
