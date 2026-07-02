"""Discourse source. Indexes forum topics — one document per topic holding its title and every
post's rendered HTML (`cooked`) flattened to text. Auth is a Discourse API key + username sent as
`Api-Key` / `Api-Username` headers (public forums also read anonymously). When no category is
configured the site's latest topics sync; `discourse:category:ID` restricts to specific categories.
Change detection is the topic's `bumped_at` as the revision id, checked against the topic list
before the full topic is fetched. This is a whole-collection crawl bounded by a lookback window,
so it never prunes."""

from __future__ import annotations

import time
from datetime import datetime

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "discourse"
DAY = 86400
MAX_PAGES = 1000  # safety bound; Discourse topic lists return an empty page at the end


# -- refs + auth --------------------------------------------------------------------------------
def _headers(cred: dict) -> dict:
    h = {"Accept": "application/json"}
    if cred.get("token"):
        h["Api-Key"] = cred["token"]
    if cred.get("email"):
        h["Api-Username"] = cred["email"]
    return h


def parse_add(item: str):
    s = item.strip()
    if s.lower().startswith("discourse:category:"):
        cid = s.split(":", 2)[2].strip()
        return ("categories", cid) if cid else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not url:
        raise RuntimeError(
            "pass --url https://forum.example.com [--token <api-key> --email <api-username>] "
            "(Discourse → Admin → API → new key).")
    base = url.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    cred = {"url": base, "token": token or None, "email": email or None}
    api_json(f"{base}/categories.json", _headers(cred), fetch=fetch)  # reachability + auth check
    save_credential(CRED, cred)
    log(f"✓ Discourse connected ({base}).")
    return cred


def connected() -> dict | None:
    return load_credential(CRED)


# -- API helpers --------------------------------------------------------------------------------
def _to_epoch(v) -> float:
    if not v:
        return 0.0
    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _categories(base, headers, fetch) -> dict:
    data = api_json(f"{base}/categories.json?include_subcategories=true", headers, fetch=fetch)
    cats = ((data.get("category_list") or {}).get("categories")) or []
    out = {}
    for c in cats:
        out[c["id"]] = {"slug": c.get("slug"), "name": c.get("name")}
        for sub in c.get("subcategory_list") or []:
            out[sub["id"]] = {"slug": sub.get("slug"), "name": sub.get("name")}
    return out


def _topic_list(base, headers, fetch, path, cutoff, full):
    """Yield topics newest-first across paged lists, stopping once a whole page is older."""
    page = 0
    sep = "&" if "?" in path else "?"
    while page < MAX_PAGES:
        data = api_json(f"{base}/{path}{sep}page={page}", headers, fetch=fetch)
        topics = ((data.get("topic_list") or {}).get("topics")) or []
        if not topics:
            return
        fresh = 0
        for t in topics:
            when = _to_epoch(t.get("bumped_at") or t.get("last_posted_at"))
            if not full and when and when < cutoff:
                continue
            fresh += 1
            yield t
        if not full and fresh == 0:
            return
        page += 1


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth discourse --url … --token … --email …`.")
    base, headers = cred["url"], _headers(cred)
    cutoff = 0 if full else time.time() - since_days * DAY

    cats = list(dict.fromkeys(config.get("categories", [])))
    changed: list[str] = []
    if cats:
        cmap = _categories(base, headers, fetch)
        paths = []
        for c in cats:
            try:
                cid = int(c)
            except (TypeError, ValueError):
                continue
            slug = (cmap.get(cid) or {}).get("slug") or "c"
            paths.append(f"c/{slug}/{cid}.json")
    else:
        paths = ["latest.json"]

    for path in paths:
        try:
            for t in _topic_list(base, headers, fetch, path, cutoff, full):
                try:
                    _ingest(store, base, headers, fetch, t, changed, full, log)
                except Exception as err:
                    log(f"discourse: topic {t.get('id')} skipped ({err})")
        except Exception as err:
            log(f"discourse: {path} skipped ({err})")
    return {"changed": changed, "removed": []}


def _ingest(store, base, headers, fetch, topic, changed, full, log) -> None:
    tid = topic["id"]
    doc_id = f"topic/{tid}"
    rev = str(topic.get("bumped_at") or topic.get("last_posted_at") or "")
    existing = store.get(CRED, doc_id)
    if not full and existing and existing.revision_id == rev:
        return
    full_t = api_json(f"{base}/t/{tid}.json", headers, fetch=fetch)
    title = full_t.get("title") or topic.get("title") or doc_id
    posts = ((full_t.get("post_stream") or {}).get("posts")) or []
    parts = [f"# {title}", ""]
    for p in posts:
        who = p.get("username") or p.get("name") or "?"
        parts += [f"**{who}**: " + html_to_text(p.get("cooked") or ""), ""]
    slug = full_t.get("slug") or topic.get("slug")
    url = f"{base}/t/{slug}/{tid}" if slug else f"{base}/t/{tid}"
    meta = {"modified_at": topic.get("bumped_at") or topic.get("last_posted_at"),
            "author": posts[0].get("username") if posts else None}
    if store.upsert(CRED, doc_id, title=title, url=url, revision_id=rev,
                    body="\n".join(parts), meta=meta):
        changed.append(doc_id)
        log(f"discourse: updated \"{title}\"")
