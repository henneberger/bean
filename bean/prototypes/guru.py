"""Guru source. Indexes every card in your Guru org (a whole-collection source) as one doc,
flattening the card's HTML `content` to text. Auth is HTTP Basic `guru_user:guru_user_token`
(a personal user token from your Guru profile), supplied as --email (the user) and --token.
Change detection is the card `lastModified` as the revision id, so unchanged cards re-embed
nothing. Pagination follows Guru's RFC5988 `Link` header (rel=next). Optionally restrict to
named collections via the `collections` list; never prunes (the org is the collection)."""

from __future__ import annotations

import base64
from urllib.parse import urlencode

from ..http import api_get
from ..store import Store
from ..html import html_to_text
from ..workspace import load_credential, save_credential

CRED = "guru"
API = "https://api.getguru.com/api/v1"
QUERY = f"{API}/search/query"
CARD_URL = "https://app.getguru.com/card/"


# -- refs + auth --------------------------------------------------------------------------------
def _headers(cred: dict) -> dict:
    raw = f"{cred.get('user', '')}:{cred['token']}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("guru:collection:"):
        name = s.split(":", 2)[2]
        return ("collections", name) if name else None
    if s.startswith("guru:"):
        name = s.split(":", 1)[1]
        return ("collections", name) if name else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    user = email or key
    if not user or not token:
        raise RuntimeError(
            "pass --email <guru user> --token <user token> "
            "(User Token: getguru.com → Settings → API Access).")
    cred = {"user": user, "token": token}
    res = api_get(f"{QUERY}?{urlencode({'maxResults': 1})}", _headers(cred), fetch=fetch)
    if res.status not in (200, 204):
        raise RuntimeError(f"Guru rejected the credentials (HTTP {res.status}).")
    save_credential(CRED, cred)
    log(f"✓ Guru connected as {user}.")
    return cred


def connected() -> dict | None:
    return load_credential(CRED)


def _next_link(headers: dict) -> str | None:
    """RFC5988 Link header → the rel=next (Guru calls it `next-page`) URL, else None."""
    link = next((v for k, v in (headers or {}).items() if k.lower() == "link"), None)
    if not link:
        return None
    for part in link.split(","):
        seg = part.split(";")
        if len(seg) < 2:
            continue
        target = seg[0].strip().strip("<>")
        rel = ""
        for p in seg[1:]:
            if "rel" in p:
                rel = p.split("=", 1)[1].strip().strip('"')
        if rel in ("next", "next-page"):
            return target
    return None


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth guru --email … --token …`.")
    headers = _headers(cred)
    want = set(config.get("collections", []))
    changed = []

    url = f"{QUERY}?{urlencode({'maxResults': 50})}"
    while url:
        res = api_get(url, headers, fetch=fetch)
        if res.status == 204 or not res.ok:
            break
        try:
            cards = res.json()
        except Exception:
            break
        for card in cards or []:
            try:
                _ingest(store, card, want, changed, full, log)
            except Exception as err:
                log(f"guru: card {card.get('id')} skipped ({err})")
        url = _next_link(res.headers)

    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _ingest(store, card, want, changed, full, log) -> bool:
    coll = (card.get("collection") or {}).get("name") or ""
    if want and coll not in want:
        return False
    cid = str(card.get("id"))
    rev = str(card.get("lastModified") or "")
    existing = store.get(CRED, cid)
    if not full and existing and rev and existing.revision_id == rev:
        return False
    title = card.get("preferredPhrase") or "Untitled"
    body = f"# {title}\n\n" + html_to_text(card.get("content") or "")
    url = CARD_URL + (card.get("slug") or cid)
    owner = card.get("owner") or {}
    author = " ".join(x for x in (owner.get("firstName"), owner.get("lastName")) if x) or None
    meta = {"modified_at": card.get("lastModified"), "author": author or owner.get("email")}
    if store.upsert(CRED, cid, title=title, url=url, revision_id=rev or None, body=body, meta=meta):
        changed.append(cid)
        log(f"guru: updated \"{title}\"")
        return True
    return False
