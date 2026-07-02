"""Web source. Indexes arbitrary web pages and whole sitemaps. No auth — it just fetches URLs.
Change detection uses the HTTP validators the server hands back: the `ETag` (preferred) or
`Last-Modified` header becomes the revision id, so an unchanged page re-embeds nothing; when a
server sends neither, revision_id is None and the content hash is the sole authority. doc_id is
the page URL. Sitemaps are expanded to their member page URLs (one level of sitemap-index nesting
is followed); pages that drop out of the configured/derived set are pruned."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from ..html import extract_readable
from ..http import api_get

# Hosts other connectors own natively — a bare URL to one of these is NOT ours to claim.
_NATIVE_HOSTS = ("github.com", "atlassian.net", "notion.so", "notion.site", "linear.app",
                 "trello.com", "figma.com", "asana.com", "slack.com", "docs.google.com",
                 "drive.google.com")
_SITEMAP_CAP = 200  # keep a single sitemap from ballooning the index


def parse_add(item: str):
    """`web:URL` → pages, `sitemap:URL` → sitemaps, a bare http(s) URL on a non-native host →
    pages. Anything else (paths, native-host URLs, bare words) → None so other sources see it."""
    s = item.strip()
    if s.startswith("web:") and _is_url(s[4:]):
        return ("pages", s[4:])
    if s.startswith("sitemap:") and _is_url(s[8:]):
        return ("sitemaps", s[8:])
    if _is_url(s) and not any(h in s.lower() for h in _NATIVE_HOSTS):
        return ("pages", s)
    return None


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _revision(headers: dict) -> str | None:
    """ETag (strong validator) else Last-Modified, case-insensitively."""
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    return lower.get("etag") or lower.get("last-modified")


def _locs(xml: str) -> list[str]:
    """<loc> values from a sitemap or sitemap-index, namespace-agnostic."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [e.text.strip() for e in root.iter()
            if e.tag.rsplit("}", 1)[-1] == "loc" and (e.text or "").strip()]


def _is_index(xml: str) -> bool:
    try:
        return ET.fromstring(xml).tag.rsplit("}", 1)[-1] == "sitemapindex"
    except ET.ParseError:
        return False


def _sitemap_pages(url: str, fetch, log) -> list[str]:
    """Expand a sitemap URL to member page URLs, following one level of index nesting."""
    try:
        res = api_get(url, {}, fetch=fetch)
    except Exception as err:
        log(f"web: sitemap {url} skipped ({err})")
        return []
    if not res.ok:
        log(f"web: sitemap {url} HTTP {res.status}")
        return []
    pages: list[str] = []
    if _is_index(res.text):
        for child in _locs(res.text):
            try:
                sub = api_get(child, {}, fetch=fetch)
            except Exception as err:
                log(f"web: sitemap {child} skipped ({err})")
                continue
            if sub.ok:
                pages += _locs(sub.text)
    else:
        pages = _locs(res.text)
    if len(pages) > _SITEMAP_CAP:
        log(f"web: sitemap {url} truncated to {_SITEMAP_CAP} of {len(pages)} urls")
        pages = pages[:_SITEMAP_CAP]
    return pages


def sync(store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    urls: list[str] = list(config.get("pages", []))
    for sm in config.get("sitemaps", []):
        urls += _sitemap_pages(sm, fetch, log)
    urls = list(dict.fromkeys(u for u in urls if _is_url(u)))

    changed, seen = [], []
    for url in urls:
        try:
            res = api_get(url, {}, fetch=fetch)
        except Exception as err:
            log(f"web: {url} skipped ({err})")
            continue
        if not res.ok:
            log(f"web: {url} HTTP {res.status}")
            continue
        seen.append(url)
        rev = _revision(res.headers)
        existing = store.get("web", url)
        if not full and existing and rev and existing.revision_id == rev:
            continue  # server validator says unchanged — skip the re-embed
        title, text = extract_readable(res.text)
        title = title or url
        body = f"# {title}\n\n{text}"
        if store.upsert("web", url, title=title, url=url, revision_id=rev, body=body):
            changed.append(url)
            log(f"web: updated {url}")
    removed = [d for d in store.doc_ids("web") if d not in seen]
    for doc_id in removed:
        store.delete("web", doc_id)
    return {"changed": changed, "removed": removed}
