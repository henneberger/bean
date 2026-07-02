"""RSS/Atom source. Tracks feed URLs and indexes each entry as its own document. No auth — it
fetches public feeds. Change detection is the entry's `pubDate`/`updated` as the revision id, so
an unchanged entry re-embeds nothing. doc_id is the entry guid/id (falling back to its link).
Unlike file/page sources this NEVER prunes: entries that scroll off the feed are retained, since a
feed is a moving window and the old items are still worth searching."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..html import html_to_text

_ATOM = "{http://www.w3.org/2005/Atom}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
_DC = "{http://purl.org/dc/elements/1.1/}"


def parse_add(item: str):
    """`rss:URL` or `feed:URL` → feeds. Anything else → None."""
    s = item.strip()
    for prefix in ("rss:", "feed:"):
        if s.startswith(prefix) and s[len(prefix):].startswith(("http://", "https://")):
            return ("feeds", s[len(prefix):])
    return None


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _find(parent, *localnames):
    """First direct child whose tag localname matches any given name (namespace-agnostic)."""
    for child in parent:
        if child.tag.rsplit("}", 1)[-1] in localnames:
            return child
    return None


def _entries(root):
    """Yield (id, title, link, body_html, revision, author) tuples for RSS items or Atom entries."""
    tag = root.tag.rsplit("}", 1)[-1]
    if tag == "feed":  # Atom
        for e in root.findall(f"{_ATOM}entry"):
            link = ""
            for ln in e.findall(f"{_ATOM}link"):
                rel = ln.get("rel", "alternate")
                if rel == "alternate" or not link:
                    link = ln.get("href", link)
            body = _text(e.find(f"{_ATOM}content")) or _text(e.find(f"{_ATOM}summary"))
            author = ""
            a = e.find(f"{_ATOM}author")
            if a is not None:
                author = _text(a.find(f"{_ATOM}name"))
            gid = _text(e.find(f"{_ATOM}id")) or link
            yield (gid, _text(e.find(f"{_ATOM}title")), link, body,
                   _text(e.find(f"{_ATOM}updated")) or _text(e.find(f"{_ATOM}published")), author)
        return
    # RSS 2.0: <rss><channel><item>...
    channel = _find(root, "channel") or root
    for it in channel:
        if it.tag.rsplit("}", 1)[-1] != "item":
            continue
        link = _text(_find(it, "link"))
        gid = _text(_find(it, "guid")) or link
        body = _text(it.find(f"{_CONTENT}encoded")) or _text(_find(it, "description"))
        author = _text(_find(it, "author")) or _text(it.find(f"{_DC}creator"))
        yield (gid, _text(_find(it, "title")), link, body,
               _text(_find(it, "pubDate")), author)


def sync(store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    from ..http import api_get

    changed = []
    for feed_url in list(dict.fromkeys(config.get("feeds", []))):
        try:
            res = api_get(feed_url, {}, fetch=fetch)
            if not res.ok:
                log(f"rss: {feed_url} HTTP {res.status}")
                continue
            root = ET.fromstring(res.text)
        except Exception as err:
            log(f"rss: {feed_url} skipped ({err})")
            continue
        for gid, title, link, body_html, rev, author in _entries(root):
            doc_id = gid or link
            if not doc_id:
                continue
            existing = store.get("rss", doc_id)
            if not full and existing and rev and existing.revision_id == rev:
                continue
            title = title or link or doc_id
            body = title + "\n\n" + html_to_text(body_html)
            meta = {"modified_at": rev or None, "author": author or None}
            if store.upsert("rss", doc_id, title=title, url=link or None,
                            revision_id=rev or None, body=body, meta=meta):
                changed.append(doc_id)
                log(f"rss: updated \"{title}\"")
    return {"changed": changed, "removed": []}  # feeds are a moving window; never prune
