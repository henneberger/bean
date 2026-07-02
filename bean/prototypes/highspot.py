"""Highspot source. Auth is HTTP Basic with `key:secret` (dev API token pair), stored per user
with the account API host. One doc per ITEM is indexed whenever the source is connected: the item
title + description + any extracted text content, tagged with its parent spot. Spots are listed via
`GET /spots`; each spot's items are paged via `GET /items?spot=…&start=…`, and each item is fetched
in full via `GET /items/{id}`. Change detection is each item's `date_updated` as the revision id; a
single bad item is logged and skipped. An optional `spots` list narrows to named spots. This source
re-observes the whole collection each run and does not prune."""

from __future__ import annotations

import base64

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

BASE = "https://api-su2.highspot.com/v1.0"
PAGE = 100


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`highspot:spot:<name>` restricts indexing to a named spot. Otherwise not ours."""
    s = item.strip()
    if s.lower().startswith("highspot:spot:"):
        return ("spots", s.split(":", 2)[2])
    return None


def connect(*, key=None, secret=None, url=None, token=None, fetch=None, log=print, **_) -> dict:
    base = (url or BASE).rstrip("/")
    if not (key and secret):
        raise RuntimeError(
            "pass --key <api-key> --secret <api-secret> "
            "(Highspot → Admin → Integrations → API → generate a dev key/secret pair).")
    headers = _headers(key, secret)
    who = api_json(f"{base}/spots?limit=1", headers, fetch=fetch)  # cheap authenticated probe
    save_credential("highspot", {"key": key, "secret": secret, "base": base})
    log(f"✓ Highspot connected ({base}).")
    return who


def connected() -> dict | None:
    return load_credential("highspot")


def _headers(key: str, secret: str) -> dict:
    raw = f"{key}:{secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Accept": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("highspot")
    if not cred:
        raise RuntimeError("not connected — run `bean auth highspot --key … --secret …`.")
    base = cred.get("base", BASE)
    headers = _headers(cred["key"], cred["secret"])
    want = {str(n).lower() for n in (config.get("spots") or [])}  # optional filter by spot name

    changed: list[str] = []
    for spot in _spots(base, headers, fetch):
        if want and str(spot.get("title", "")).lower() not in want:
            continue
        changed += _sync_spot(store, base, headers, fetch, full, spot, log)
    return {"changed": changed, "removed": []}


def _spots(base, headers, fetch) -> list:
    out, start = [], 0
    while True:
        resp = api_json(f"{base}/spots?right=view&start={start}&limit={PAGE}", headers, fetch=fetch)
        found = resp.get("collection", [])
        out += found
        if len(found) < PAGE:
            break
        start += PAGE
    return out


def _sync_spot(store, base, headers, fetch, full, spot, log) -> list[str]:
    changed: list[str] = []
    spot_id = spot.get("id")
    spot_name = spot.get("title", "")
    start = 0
    while True:
        resp = api_json(f"{base}/items?spot={spot_id}&start={start}&limit={PAGE}", headers, fetch=fetch)
        items = resp.get("collection", [])
        for it in items:
            try:
                iid = it.get("id")
                if not iid:
                    continue
                detail = api_json(f"{base}/items/{iid}", headers, fetch=fetch)
                doc_id = f"item/{iid}"
                rev = detail.get("date_updated")
                existing = store.get("highspot", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                body = _item_body(detail, spot_name)
                url = detail.get("url") or f"https://www.highspot.com/items/{iid}"
                if store.upsert("highspot", doc_id, title=detail.get("title", doc_id), url=url,
                                revision_id=rev, body=body,
                                meta={"created_at": detail.get("date_added"), "modified_at": rev,
                                      "author": detail.get("author")}):
                    changed.append(doc_id)
                    log(f"highspot: updated {doc_id}")
            except Exception as err:  # one bad item must never abort the sync
                log(f"highspot: item {it.get('id')} skipped ({err})")
        if len(items) < PAGE:
            break
        start += PAGE
    return changed


def _item_body(detail: dict, spot_name: str) -> str:
    lines = [f"# {detail.get('title', '')}", f"spot: {spot_name}", ""]
    desc = detail.get("description") or ""
    if desc.strip():
        lines += [html_to_text(desc), ""]
    content = detail.get("content") or detail.get("content_text") or ""
    if content.strip():
        lines.append(html_to_text(content) if "<" in content else content)
    return "\n".join(lines)
