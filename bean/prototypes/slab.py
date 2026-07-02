"""Slab source. Indexes every post in your Slab org (a whole-collection source) as one doc,
flattening the post's content (Quill-delta JSON or HTML) to text. Auth is a Slab API token sent
raw in the `Authorization` header against the GraphQL endpoint. The org's post list (id, title,
updatedAt, version) is fetched cheaply; the full content is pulled only when a post's version
changed. doc_id is the post id (stable across title edits) and the revision id is the post
version (falling back to updatedAt). Never prunes; the org is the collection."""

from __future__ import annotations

import json

from ..http import api_json_post
from ..store import Store
from ..html import html_to_text
from ..workspace import load_credential, save_credential

CRED = "slab"
API = "https://api.slab.com/v1/graphql"

_LIST_Q = "{ organization { posts { id title updatedAt version } } }"
_POST_Q = ("query($id: ID!) { post(id: $id) { title content updatedAt version } }")


# -- refs + auth --------------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("slab:tag:"):
        tag = s.split(":", 2)[2]
        return ("tags", tag) if tag else None
    if s.startswith("slab:"):
        tag = s.split(":", 1)[1]
        return ("tags", tag) if tag else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError("pass --token <api token> (Slab → Settings → API).")
    data = api_json_post(API, _headers(token), {"query": _LIST_Q}, fetch=fetch)
    if data.get("errors"):
        raise RuntimeError(f"Slab rejected the token: {data['errors']}")
    save_credential(CRED, {"token": token, "url": (url or "").rstrip("/")})
    log("✓ Slab connected.")
    return {"ok": True}


def connected() -> dict | None:
    return load_credential(CRED)


def _post_url(base: str, title: str, pid: str) -> str:
    slug = (title or "").lower()
    for ch in "[]:":
        slug = slug.replace(ch, "")
    slug = slug.replace(" ", "-")
    tail = f"posts/{slug}-{pid}" if slug else f"posts/{pid}"
    return f"{base}/{tail}" if base else f"https://slab.com/{tail}"


def _content_text(content: str) -> str:
    """Slab content is a Quill-delta JSON array of {insert: …} ops, or HTML — flatten to text."""
    if not content:
        return ""
    try:
        segs = json.loads(content)
        if isinstance(segs, list):
            return "".join(s.get("insert") for s in segs
                           if isinstance(s.get("insert"), str))
    except (ValueError, TypeError):
        pass
    if "<" in content and ">" in content:
        return html_to_text(content)
    return content


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth slab --token …`.")
    headers = _headers(cred["token"])
    base = (cred.get("url") or "").rstrip("/")

    data = api_json_post(API, headers, {"query": _LIST_Q}, fetch=fetch)
    posts = (((data.get("data") or {}).get("organization")) or {}).get("posts") or []

    changed = []
    for stub in posts:
        try:
            _ingest(store, base, stub, headers, fetch, full, changed, log)
        except Exception as err:
            log(f"slab: post {stub.get('id')} skipped ({err})")
    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _ingest(store, base, stub, headers, fetch, full, changed, log):
    pid = str(stub.get("id"))
    rev = str(stub.get("version") or stub.get("updatedAt") or "")
    existing = store.get(CRED, pid)
    if not full and existing and rev and existing.revision_id == rev:
        return
    data = api_json_post(API, headers, {"query": _POST_Q, "variables": {"id": pid}}, fetch=fetch)
    post = (data.get("data") or {}).get("post") or {}
    title = post.get("title") or stub.get("title") or "Untitled"
    body = f"# {title}\n\n" + _content_text(post.get("content") or "")
    meta = {"modified_at": post.get("updatedAt") or stub.get("updatedAt")}
    if store.upsert(CRED, pid, title=title, url=_post_url(base, title, pid),
                    revision_id=rev or None, body=body, meta=meta):
        changed.append(pid)
        log(f"slab: updated \"{title}\"")
