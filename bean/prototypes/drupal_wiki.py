"""Drupal Wiki source. Indexes wiki pages over the Drupal Wiki REST API (`/api/rest/scope/api/*`)
behind a Bearer token. When no space is configured the whole collection syncs (every space →
every page); `drupalwiki:SPACEID` restricts to specific spaces. Each page's HTML body is flattened
to text; change detection is the page's `lastModified` epoch as the revision id, so the page list
is walked cheaply and only changed pages have their body refetched. This is a whole-collection
crawl bounded by a lookback window, so it never prunes."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "drupalwiki"
DAY = 86400
PAGE_SIZE = 500


# -- refs + auth --------------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.lower().startswith("drupalwiki:"):
        sid = s.split(":", 1)[1].strip()
        return ("spaces", sid) if sid else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token or not url:
        raise RuntimeError(
            "pass --url https://your.drupal-wiki.com --token <bearer> "
            "(Drupal Wiki → user settings → API token).")
    base = url.rstrip("/")
    api_json(f"{base}/api/rest/scope/api/space?{urlencode({'size': 1, 'page': 0})}",
             _headers(token), fetch=fetch)  # cheap reachability check
    save_credential(CRED, {"url": base, "token": token})
    log(f"✓ Drupal Wiki connected ({base}).")
    return {"url": base}


def connected() -> dict | None:
    return load_credential(CRED)


# -- REST helpers -------------------------------------------------------------------------------
def _iso(epoch) -> str | None:
    try:
        sec = float(epoch)
    except (TypeError, ValueError):
        return None
    if sec > 1e12:  # milliseconds
        sec /= 1000.0
    return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()


def _paged(base: str, path: str, headers: dict, fetch, **extra) -> list[dict]:
    out, page = [], 0
    while True:
        params = {"size": PAGE_SIZE, "page": page, **extra}
        data = api_json(f"{base}{path}?{urlencode(params)}", headers, fetch=fetch)
        content = data.get("content") or []
        out += content
        if data.get("last") or len(content) < PAGE_SIZE:
            break
        page += 1
    return out


def _all_space_ids(base, headers, fetch) -> list[str]:
    spaces = _paged(base, "/api/rest/scope/api/space", headers, fetch)
    return [str(s["id"]) for s in spaces if s.get("id") is not None]


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth drupalwiki --url … --token …`.")
    base, headers = cred["url"], _headers(cred["token"])
    cutoff = 0 if full else int(time.time() - since_days * DAY)

    spaces = list(dict.fromkeys(config.get("spaces", [])))
    if not spaces:
        spaces = _all_space_ids(base, headers, fetch)

    changed: list[str] = []
    for sid in spaces:
        try:
            pages = _paged(base, "/api/rest/scope/api/page", headers, fetch,
                           space=str(sid), modifiedAfter=cutoff)
        except Exception as err:
            log(f"drupalwiki: space {sid} skipped ({err})")
            continue
        for p in pages:
            try:
                _ingest(store, base, headers, fetch, p, changed, full, log)
            except Exception as err:
                log(f"drupalwiki: page {p.get('id')} skipped ({err})")
    return {"changed": changed, "removed": []}


def _ingest(store, base, headers, fetch, p, changed, full, log) -> None:
    pid = str(p["id"])
    rev = str(p.get("lastModified") or "")
    existing = store.get(CRED, pid)
    if not full and existing and existing.revision_id == rev:
        return
    full_page = api_json(f"{base}/api/rest/scope/api/page/{pid}", headers, fetch=fetch)
    title = full_page.get("title") or p.get("title") or pid
    body = f"# {title}\n\n" + html_to_text(full_page.get("body") or "")
    meta = {"modified_at": _iso(p.get("lastModified")), "space_id": str(p.get("homeSpace", ""))}
    if store.upsert(CRED, pid, title=title, url=f"{base}/node/{pid}",
                    revision_id=rev, body=body, meta=meta):
        changed.append(pid)
        log(f"drupalwiki: updated \"{title}\"")
