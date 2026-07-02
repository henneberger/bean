"""GitBook source. Tracks spaces (by id) and indexes each page's Markdown export as one doc.
Auth is a personal API token (Bearer). The space content tree is listed via
`/spaces/{id}/content`; each page's text comes from its Markdown export endpoint
(`/spaces/{id}/content/page/{pageId}?format=markdown`). doc_id is `{spaceId}/{pageId}` and the
revision id is the page `updatedAt`, so unchanged pages re-embed nothing. Removing a space from
config prunes its pages."""

from __future__ import annotations

import re

from ..http import api_json
from ..store import Store
from ..html import html_to_text
from ..workspace import load_credential, save_credential

CRED = "gitbook"
API = "https://api.gitbook.com/v1"


# -- refs + auth --------------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("gitbook:"):
        sid = s.split(":", 1)[1]
        return ("spaces", sid) if sid else None
    if "gitbook.com" in s or "gitbook.io" in s:
        m = re.search(r"/s/([A-Za-z0-9_-]+)", s) or re.search(r"/spaces/([A-Za-z0-9_-]+)", s)
        if m:
            return ("spaces", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError(
            "pass --token <api token> (create one at gitbook.com → Account → Developer settings).")
    who = api_json(f"{API}/user", _headers(token), fetch=fetch)
    save_credential(CRED, {"token": token, "name": who.get("displayName") or who.get("email")})
    log(f"✓ GitBook connected as {who.get('displayName') or who.get('email') or 'user'}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


# -- content ------------------------------------------------------------------------------------
def _walk(pages: list) -> list:
    """Flatten the nested content tree into a flat list of page nodes."""
    out = []
    for p in pages or []:
        out.append(p)
        out += _walk(p.get("pages") or [])
    return out


def _doc_text(doc) -> str:
    """A page's Markdown export is a string; tolerate the older node-tree shape defensively."""
    if isinstance(doc, str):
        return doc
    if isinstance(doc, dict):
        # Node-tree fallback: pull every leaf's text.
        parts = []

        def leaves(node):
            if isinstance(node, dict):
                if isinstance(node.get("text"), str):
                    parts.append(node["text"])
                for v in node.values():
                    leaves(v)
            elif isinstance(node, list):
                for v in node:
                    leaves(v)
        leaves(doc)
        return " ".join(parts)
    return ""


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth gitbook --token …`.")
    headers = _headers(cred["token"])
    spaces = list(dict.fromkeys(config.get("spaces", [])))

    changed, seen = [], []
    for space in spaces:
        try:
            content = api_json(f"{API}/spaces/{space}/content", headers, fetch=fetch)
        except RuntimeError as err:
            log(f"gitbook: space {space} skipped ({err})")
            continue
        for page in _walk(content.get("pages")):
            try:
                _ingest(store, space, page, headers, fetch, full, changed, seen, log)
            except Exception as err:
                log(f"gitbook: page {page.get('id')} skipped ({err})")

    removed = [d for d in store.doc_ids(CRED)
               if d.split("/", 1)[0] not in set(spaces)]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _ingest(store, space, page, headers, fetch, full, changed, seen, log):
    pid = page.get("id")
    updated = page.get("updatedAt")
    if not pid or not updated:
        return  # groups/links / never-edited pages carry no content
    doc_id = f"{space}/{pid}"
    seen.append(doc_id)
    existing = store.get(CRED, doc_id)
    if not full and existing and existing.revision_id == updated:
        return
    content = api_json(f"{API}/spaces/{space}/content/page/{pid}?format=markdown",
                       headers, fetch=fetch)
    text = _doc_text(content.get("document") or content.get("markdown") or "")
    if "<" in text and ">" in text:
        text = html_to_text(text)
    title = page.get("title") or "Untitled"
    body = f"# {title}\n\n{text}"
    url = (page.get("urls") or {}).get("app") or ""
    meta = {"modified_at": updated}
    if store.upsert(CRED, doc_id, title=title, url=url, revision_id=updated, body=body, meta=meta):
        changed.append(doc_id)
        log(f"gitbook: updated \"{title}\"")
