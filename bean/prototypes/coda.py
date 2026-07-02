"""Coda source. Tracks whole docs by id and indexes each of their pages as Markdown. Auth is an
API token (Bearer). Change detection: a page's `contentVersion` is the revision id, so unchanged
pages re-embed nothing.

Page content only comes out of Coda through an async export: POST an export request, poll its
status url until `complete`, then GET the one-shot download link for the Markdown. The poll sleep
is injectable (`sleep=`) and attempts are capped so tests run offline and instantly. Removing a
doc from config prunes every page under it."""

from __future__ import annotations

import re
import time
from urllib.parse import urlencode

from ..http import api_json, api_json_post, api_get
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://coda.io/apis/v1"
# coda.io/d/Some-Title_dABC123 → the id is the token right after "_d".
URL_RE = re.compile(r"coda\.io/d/[^/\s]*_d([A-Za-z0-9_-]+)", re.I)
POLL_ATTEMPTS = 30


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    item = item.strip()
    if item.startswith("coda:"):
        did = item[len("coda:"):].strip()
        return ("docs", did) if did else None
    m = URL_RE.search(item)
    if m:
        return ("docs", m.group(1))
    return None


def connect(*, token=None, fetch=None, log=print, **_ignored) -> dict:
    if not token:
        raise RuntimeError("pass --token … (create one at coda.io/account under API Settings).")
    who = api_json(f"{API}/whoami", _headers(token), fetch=fetch)
    save_credential("coda", {"token": token, "name": who.get("name") or who.get("loginId")})
    log(f"✓ Coda connected as {who.get('name') or who.get('loginId')}.")
    return who


def connected() -> dict | None:
    return load_credential("coda")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# -- export (async poll) ------------------------------------------------------------------------
def _export_markdown(doc_id: str, page_id: str, headers: dict, fetch, sleep) -> str:
    req = api_json_post(f"{API}/docs/{doc_id}/pages/{page_id}/export",
                        headers, {"outputFormat": "markdown"}, fetch=fetch)
    status_url = req.get("href")
    if not status_url:
        return ""
    for _ in range(POLL_ATTEMPTS):
        st = api_json(status_url, headers, fetch=fetch)
        status = st.get("status")
        if status == "complete":
            link = st.get("downloadLink")
            if not link:
                return ""
            res = api_get(link, {}, fetch=fetch)  # signed one-shot URL, no auth header
            return res.text if res.ok else ""
        if status == "failed":
            return ""
        sleep(1.0)
    return ""


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None, sleep=time.sleep) -> dict:
    cred = load_credential("coda")
    if not cred:
        raise RuntimeError("not connected — run `bean auth coda --token …`.")
    headers = _headers(cred["token"])
    docs = list(dict.fromkeys(config.get("docs", [])))
    changed, seen = [], []

    for doc in docs:
        cursor = None
        while True:
            q = urlencode({"limit": 100, **({"pageToken": cursor} if cursor else {})})
            try:
                resp = api_json(f"{API}/docs/{doc}/pages?{q}", headers, fetch=fetch)
            except RuntimeError as err:
                log(f"coda: {doc} skipped ({err})")
                break
            for page in resp.get("items", []):
                pid = page.get("id")
                if not pid:
                    continue
                doc_id = f"{doc}/{pid}"
                seen.append(doc_id)
                rev = page.get("contentVersion")
                existing = store.get("coda", doc_id)
                if not full and existing and rev is not None and existing.revision_id == rev:
                    continue
                body = _export_markdown(doc, pid, headers, fetch, sleep)
                meta = {"modified_at": page.get("updatedAt")} if page.get("updatedAt") else None
                if store.upsert("coda", doc_id, title=page.get("name") or "Untitled",
                                url=page.get("browserLink"), revision_id=rev, body=body, meta=meta):
                    changed.append(doc_id)
                    log(f"coda: updated \"{page.get('name')}\"")
            cursor = resp.get("nextPageToken")
            if not cursor:
                break

    removed = [d for d in store.doc_ids("coda") if d not in seen and _doc_of(d) in set(docs)]
    for d in removed:
        store.delete("coda", d)
    # Also drop pages whose parent doc was untracked entirely.
    stale = [d for d in store.doc_ids("coda") if _doc_of(d) not in set(docs)]
    for d in stale:
        store.delete("coda", d)
    return {"changed": changed, "removed": removed + stale}


def _doc_of(doc_id: str) -> str:
    return doc_id.split("/", 1)[0]
