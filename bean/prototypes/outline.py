"""Outline source. Indexes every document in a self-hosted (or cloud) Outline knowledge base as
one doc, storing its Markdown `text`. Auth is a personal API token (Bearer) against a base --url.
Outline's API is POST-only, so listing pages through POST `/api/documents.list` with an
`offset`/`limit` pager. doc_id is the document id and the revision id is `updatedAt`, so unchanged
docs re-embed nothing. Optionally restrict to `collections` (by id); with none set every document
syncs, so it never prunes by default — removed docs still fall out via the seen set."""

from __future__ import annotations

from ..http import api_json_post, api_post
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "outline"


# -- refs + auth --------------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("outline:"):
        cid = s.split(":", 1)[1]
        return ("collections", cid) if cid else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token or not url:
        raise RuntimeError(
            "pass --url https://your.outline.host --token <api token> "
            "(create one at Outline → Settings → API Tokens).")
    base = url.rstrip("/")
    res = api_post(f"{base}/api/auth.info", _headers(token), {}, fetch=fetch)
    if not res.ok:
        raise RuntimeError(f"Outline rejected the credentials (HTTP {res.status}).")
    who = (res.json().get("data") or {}).get("user") or {}
    save_credential(CRED, {"token": token, "url": base, "name": who.get("name")})
    log(f"✓ Outline connected ({who.get('name') or base}).")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth outline --url … --token …`.")
    headers = _headers(cred["token"])
    base = cred["url"].rstrip("/")
    want = set(config.get("collections", []))

    changed, seen, offset = [], [], 0
    while True:
        page = api_json_post(f"{base}/api/documents.list", headers,
                             {"limit": 100, "offset": offset}, fetch=fetch)
        batch = page.get("data") or []
        for doc in batch:
            try:
                _ingest(store, base, doc, want, changed, seen, full, log)
            except Exception as err:
                log(f"outline: document {doc.get('id')} skipped ({err})")
        offset += len(batch)
        if len(batch) < 100:
            break

    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _ingest(store, base, doc, want, changed, seen, full, log):
    if want and doc.get("collectionId") not in want:
        return
    did = str(doc.get("id"))
    seen.append(did)
    rev = doc.get("updatedAt")
    existing = store.get(CRED, did)
    if not full and existing and rev and existing.revision_id == rev:
        return
    title = doc.get("title") or "Untitled"
    body = f"# {title}\n\n{doc.get('text') or ''}"
    path = doc.get("url") or f"/doc/{did}"
    url = base + path if path.startswith("/") else path
    meta = {"modified_at": rev, "created_at": doc.get("createdAt"),
            "author": (doc.get("createdBy") or {}).get("name")}
    if store.upsert(CRED, did, title=title, url=url, revision_id=rev, body=body, meta=meta):
        changed.append(did)
        log(f"outline: updated \"{title}\"")
