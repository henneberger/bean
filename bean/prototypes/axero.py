"""Axero (Communifire) source. Indexes articles, blog posts and wiki pages over the Axero REST
API (`/api/content/list` + `/api/content/{id}`) authenticated with a `Rest-Api-Key` header. When no
space is configured the whole deployment syncs; `axero:SPACEID` restricts to specific spaces. Each
content item's summary + HTML body is flattened to text; change detection is `DateUpdated` as the
revision id. The list endpoint is sorted newest-first, so a lookback window lets the crawl stop
early. This is a whole-collection crawl bounded by that window, so it never prunes."""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import urlencode

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "axero"
DAY = 86400
# Axero EntityType ids: 3 = Article, 4 = Blog, 9 = Wiki.
ENTITY_TYPES = (3, 4, 9)


# -- refs + auth --------------------------------------------------------------------------------
def _headers(key: str) -> dict:
    return {"Rest-Api-Key": key, "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.lower().startswith("axero:"):
        sid = s.split(":", 1)[1].strip()
        return ("spaces", sid) if sid else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    apikey = key or token
    if not apikey or not url:
        raise RuntimeError(
            "pass --url https://you.axerosolutions.com --key <rest-api-key> "
            "(Axero → Control Panel → REST API key).")
    base = url.rstrip("/")
    api_json(f"{base}/api/content/list?{urlencode({'EntityType': 3, 'StartPage': 1})}",
             _headers(apikey), fetch=fetch)  # cheap reachability check
    save_credential(CRED, {"url": base, "key": apikey})
    log(f"✓ Axero connected ({base}).")
    return {"url": base}


def connected() -> dict | None:
    return load_credential(CRED)


# -- REST helpers -------------------------------------------------------------------------------
def _to_epoch(v) -> float:
    if not v:
        return 0.0
    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _list_entities(base, headers, fetch, entity, space_id, cutoff, full) -> list[dict]:
    out, page, fetched = [], 1, 0
    while True:
        params = {"EntityType": str(entity), "SortColumn": "DateUpdated",
                  "SortOrder": "1", "StartPage": str(page)}
        if space_id is not None:
            params["SpaceID"] = str(space_id)
        data = api_json(f"{base}/api/content/list?{urlencode(params)}", headers, fetch=fetch)
        contents = data.get("ResponseData") or []
        total = data.get("TotalRecords", 0)
        fetched += len(contents)
        stop = False
        for c in contents:
            if not full and _to_epoch(c.get("DateUpdated")) < cutoff:
                stop = True
                break
            out.append(c)
        if stop or not contents or fetched >= total:
            break
        page += 1
    return out


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth axero --url … --key …`.")
    base, headers = cred["url"], _headers(cred["key"])
    cutoff = 0 if full else time.time() - since_days * DAY

    spaces = list(dict.fromkeys(config.get("spaces", []))) or [None]
    changed: list[str] = []
    for sid in spaces:
        for entity in ENTITY_TYPES:
            try:
                contents = _list_entities(base, headers, fetch, entity, sid, cutoff, full)
            except Exception as err:
                log(f"axero: entity {entity} space {sid} skipped ({err})")
                continue
            for c in contents:
                try:
                    _ingest(store, base, c, changed, full, log)
                except Exception as err:
                    log(f"axero: content {c.get('ContentID')} skipped ({err})")
    return {"changed": changed, "removed": []}


def _ingest(store, base, c, changed, full, log) -> None:
    cid = str(c.get("ContentID"))
    rev = str(c.get("DateUpdated") or c.get("ModifiedDate") or "")
    existing = store.get(CRED, cid)
    if not full and existing and existing.revision_id == rev:
        return
    title = c.get("ContentTitle") or cid
    summary = (c.get("ContentSummary") or "").strip()
    body = f"# {title}\n\n" + (summary + "\n\n" if summary else "") + \
        html_to_text(c.get("ContentBody") or "")
    link = c.get("ContentURL") or ""
    if link and not link.startswith("http"):
        link = base + ("" if link.startswith("/") else "/") + link
    meta = {"modified_at": c.get("DateUpdated"), "space": c.get("SpaceName")}
    if store.upsert(CRED, cid, title=title, url=link or None,
                    revision_id=rev, body=body, meta=meta):
        changed.append(cid)
        log(f"axero: updated \"{title}\"")
