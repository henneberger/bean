"""Confluence source. Tracks spaces (by key) and indexes every page's storage-format HTML as
text. Auth supports both Atlassian Cloud (email + API token → HTTP Basic `email:token`) and
Server/Data Center (personal access token → `Authorization: Bearer <token>`); connect() picks
Cloud when an `email` is supplied, else DC. Change detection is the page `version.number` as the
revision id, so unchanged pages re-embed nothing. Removing a space from config prunes its pages."""

from __future__ import annotations

import base64
import re
from urllib.parse import urlencode

from .http import api_json
from .store import Store
from .workspace import load_credential, save_credential
from .html import html_to_text

CRED = "confluence"


# -- refs + auth --------------------------------------------------------------------------------
def _headers(cred: dict) -> dict:
    if cred.get("method") == "cloud":
        raw = f"{cred.get('email', '')}:{cred['token']}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
                "Accept": "application/json"}
    return {"Authorization": f"Bearer {cred['token']}", "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("confluence:page:"):
        pid = s.split(":", 2)[2]
        return ("pages", pid) if pid else None
    if s.startswith("confluence:"):
        key = s.split(":", 1)[1]
        return ("spaces", key) if key else None
    if "atlassian.net/wiki" in s or "/pages/" in s or "/spaces/" in s:
        m = re.search(r"/pages/(\d+)", s)
        if m:
            return ("pages", m.group(1))
        m = re.search(r"/spaces/([^/?#]+)", s)
        if m:
            return ("spaces", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token or not url:
        raise RuntimeError(
            "pass --url https://your.atlassian.net/wiki --token <token> (Cloud also needs "
            "--email; get a Cloud token at id.atlassian.com/manage-profile/security/api-tokens).")
    url = url.rstrip("/")
    method = "cloud" if email else "dc"
    cred = {"method": method, "url": url, "email": email, "token": token, "name": None}
    who = {}
    try:
        if method == "cloud":
            who = api_json(f"{url}/rest/api/user/current", _headers(cred), fetch=fetch)
        else:
            who = api_json(f"{url}/rest/api/user", _headers(cred), fetch=fetch)
    except RuntimeError:
        who = {}  # DC identity endpoint may 404 — just save the credential
    cred["name"] = who.get("displayName") or who.get("username") or email
    save_credential(CRED, cred)
    log(f"✓ Confluence connected ({cred['name'] or url}).")
    return cred


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth confluence --url … --token …`.")
    headers = _headers(cred)
    base = cred["url"].rstrip("/")
    spaces = list(dict.fromkeys(config.get("spaces", [])))
    single = list(dict.fromkeys(config.get("pages", [])))

    changed, seen = [], []

    def ingest(page: dict):
        pid = str(page.get("id"))
        seen.append(pid)
        rev = str(((page.get("version") or {}).get("number", "")))
        existing = store.get(CRED, pid)
        if not full and existing and existing.revision_id == rev:
            return
        html = (((page.get("body") or {}).get("storage") or {}).get("value")) or ""
        title = page.get("title") or "Untitled"
        body = f"# {title}\n\n" + html_to_text(html)
        webui = ((page.get("_links") or {}).get("webui")) or ""
        url = base + webui if webui else f"{base}/pages/{pid}"
        version = page.get("version") or {}
        last = (((page.get("history") or {}).get("lastUpdated")) or {})
        meta = {"modified_at": last.get("when") or version.get("when"),
                "author": (version.get("by") or {}).get("displayName")}
        if store.upsert(CRED, pid, title=title, url=url, revision_id=rev, body=body, meta=meta):
            changed.append(pid)
            log(f"confluence: updated \"{title}\"")

    expand = "body.storage,version,history.lastUpdated"
    for key in spaces:
        start = 0
        while True:
            q = urlencode({"spaceKey": key, "type": "page", "expand": expand,
                           "limit": 100, "start": start})
            resp = api_json(f"{base}/rest/api/content?{q}", headers, fetch=fetch)
            results = resp.get("results", [])
            for page in results:
                ingest(page)
            size = resp.get("size", len(results))
            if len(results) < 100 or not results:
                break
            start += size or 100

    for pid in single:
        try:
            page = api_json(f"{base}/rest/api/content/{pid}?expand={expand}", headers, fetch=fetch)
        except RuntimeError as err:
            log(f"confluence: {pid} skipped ({err})")
            continue
        ingest(page)

    removed = [d for d in store.doc_ids(CRED) if d not in seen]
    for pid in removed:
        store.delete(CRED, pid)
    return {"changed": changed, "removed": removed}
