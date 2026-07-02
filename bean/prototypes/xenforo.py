"""XenForo source. Indexes forum threads over the XenForo 2.x REST API (`/api/threads`,
`/api/posts`) behind an `XF-Api-Key` header. One document per thread holds every post's message
(rendered HTML → text). When nothing is configured the whole board syncs (every forum → every
thread); `xenforo:forum:ID` restricts to forums and a thread URL (or `xenforo:thread:ID`) tracks a
single thread. Change detection is the thread's `last_post_date` as the revision id, checked
against the thread list before posts are fetched. This is a whole-collection crawl bounded by a
lookback window, so it never prunes."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "xenforo"
DAY = 86400
_THREAD_RE = re.compile(r"/threads/(?:[^/]*?\.)?(\d+)")


# -- refs + auth --------------------------------------------------------------------------------
def _headers(key: str) -> dict:
    return {"XF-Api-Key": key, "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    low = s.lower()
    if low.startswith("xenforo:forum:"):
        fid = s.split(":", 2)[2].strip()
        return ("forums", fid) if fid else None
    if low.startswith("xenforo:thread:"):
        tid = s.split(":", 2)[2].strip()
        return ("threads", tid) if tid else None
    if s.startswith("http") and "/threads/" in low:
        m = _THREAD_RE.search(s)
        if m:
            return ("threads", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    apikey = key or token
    if not apikey or not url:
        raise RuntimeError(
            "pass --url https://forum.example.com --key <XF-Api-Key> "
            "(XenForo → Admin → Setup → API keys).")
    base = url.rstrip("/")
    api_json(f"{base}/api/", _headers(apikey), fetch=fetch)  # API index — reachability + auth check
    save_credential(CRED, {"url": base, "key": apikey})
    log(f"✓ XenForo connected ({base}).")
    return {"url": base}


def connected() -> dict | None:
    return load_credential(CRED)


# -- API helpers --------------------------------------------------------------------------------
def _iso(v) -> str | None:
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _all_forums(base, headers, fetch) -> list[str]:
    data = api_json(f"{base}/api/forums/", headers, fetch=fetch)
    nodes = data.get("forums") or data.get("nodes") or []
    ids = []
    for n in nodes:
        nid = n.get("node_id") or n.get("forum_id")
        if nid is not None:
            ids.append(str(nid))
    return ids


def _threads_in_forum(base, headers, fetch, forum_id, cutoff, full):
    page = 1
    while True:
        data = api_json(f"{base}/api/threads/?forum_id={forum_id}&page={page}",
                        headers, fetch=fetch)
        threads = data.get("threads") or []
        if not threads:
            return
        for t in threads:
            when = t.get("last_post_date") or 0
            if not full and when and when < cutoff:
                continue
            yield t
        last_page = (data.get("pagination") or {}).get("last_page") or page
        if page >= last_page:
            return
        page += 1


def _thread_posts(base, headers, fetch, tid) -> list[dict]:
    posts, page = [], 1
    while True:
        data = api_json(f"{base}/api/threads/{tid}/?with_posts=1&page={page}",
                        headers, fetch=fetch)
        batch = data.get("posts") or []
        posts += batch
        last_page = (data.get("pagination") or {}).get("last_page") or page
        if page >= last_page or not batch:
            break
        page += 1
    return posts


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth xenforo --url … --key …`.")
    base, headers = cred["url"], _headers(cred["key"])
    cutoff = 0 if full else time.time() - since_days * DAY

    forums = list(dict.fromkeys(config.get("forums", [])))
    threads = list(dict.fromkeys(config.get("threads", [])))
    if not forums and not threads:
        try:
            forums = _all_forums(base, headers, fetch)
        except Exception as err:
            log(f"xenforo: forum listing skipped ({err})")

    changed: list[str] = []
    for fid in forums:
        try:
            for t in _threads_in_forum(base, headers, fetch, fid, cutoff, full):
                try:
                    _ingest(store, base, headers, fetch, t, changed, full, log)
                except Exception as err:
                    log(f"xenforo: thread {t.get('thread_id')} skipped ({err})")
        except Exception as err:
            log(f"xenforo: forum {fid} skipped ({err})")

    for tid in threads:
        try:
            data = api_json(f"{base}/api/threads/{tid}/", headers, fetch=fetch)
            th = data.get("thread") or {"thread_id": tid}
            _ingest(store, base, headers, fetch, th, changed, True, log)
        except Exception as err:
            log(f"xenforo: thread {tid} skipped ({err})")
    return {"changed": changed, "removed": []}


def _ingest(store, base, headers, fetch, th, changed, full, log) -> None:
    tid = str(th.get("thread_id"))
    doc_id = f"thread/{tid}"
    rev = str(th.get("last_post_date") or "")
    existing = store.get(CRED, doc_id)
    if not full and existing and existing.revision_id == rev:
        return
    title = th.get("title") or doc_id
    posts = _thread_posts(base, headers, fetch, tid)
    parts = [f"# {title}", ""]
    for p in posts:
        who = p.get("username") or "?"
        text = html_to_text(p.get("message_parsed") or "") or (p.get("message") or "")
        parts += [f"**{who}**: {text.strip()}", ""]
    url = th.get("view_url") or f"{base}/threads/{tid}/"
    meta = {"modified_at": _iso(th.get("last_post_date")),
            "author": posts[0].get("username") if posts else None}
    if store.upsert(CRED, doc_id, title=title, url=url, revision_id=rev,
                    body="\n".join(parts), meta=meta):
        changed.append(doc_id)
        log(f"xenforo: updated \"{title}\"")
