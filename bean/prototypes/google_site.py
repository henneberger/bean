"""Google Sites source. Onyx ingests a published-site zip export; bean instead crawls the live
published site with the injectable fetch. Given a site's base URL, it fetches that page, follows
same-host links one level deep (bounded, cap 200), and runs each page through the shared readable
extractor. No auth — published sites are public. Change detection uses the HTTP validators the
server hands back: the `ETag` (preferred) or `Last-Modified` header becomes the revision id, so an
unchanged page re-embeds nothing. doc_id is the page URL; pages that drop out of the crawl are
pruned."""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse

from ..html import extract_readable
from ..http import api_get

_CAP = 200  # keep a single site from ballooning the index


# -- refs ---------------------------------------------------------------------------------------
def parse_add(item: str):
    """`gsite:URL` → sites, or a bare `sites.google.com/...` URL. Anything else → None so other
    sources (web/localfiles) still see it. Must be routed BEFORE `web`, which would otherwise claim
    a bare sites.google.com URL as a generic page."""
    s = item.strip()
    if s.lower().startswith("gsite:") and _is_url(s[6:]):
        return ("sites", s[6:])
    if _is_url(s) and urlparse(s).netloc.lower().endswith("sites.google.com"):
        return ("sites", s)
    return None


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _revision(headers: dict) -> str | None:
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    return lower.get("etag") or lower.get("last-modified")


def _links(html: str, base: str) -> list[str]:
    """Same-host page links from an HTML page, absolutised and de-fragmented."""
    host = urlparse(base).netloc.lower()
    out, seen = [], set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', html or ""):
        if href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        absolute = urldefrag(urljoin(base, href))[0]
        if not _is_url(absolute) or urlparse(absolute).netloc.lower() != host:
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


# -- crawl --------------------------------------------------------------------------------------
def _crawl_pages(base: str, fetch, log) -> list[str]:
    """The base URL plus its same-host links (one level), capped."""
    try:
        res = api_get(base, {}, fetch=fetch)
    except Exception as err:
        log(f"gsite: {base} skipped ({err})")
        return []
    if not res.ok:
        log(f"gsite: {base} HTTP {res.status}")
        return [base]
    pages = [base] + [u for u in _links(res.text, base) if u != base]
    if len(pages) > _CAP:
        log(f"gsite: {base} truncated to {_CAP} of {len(pages)} pages")
        pages = pages[:_CAP]
    return pages


# -- sync ---------------------------------------------------------------------------------------
def sync(store, config: dict, *, settings: dict | None = None, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    sites = [s for s in config.get("sites", []) if _is_url(s)]
    urls: list[str] = []
    for site in sites:
        for u in _crawl_pages(site, fetch, log):
            if u not in urls:
                urls.append(u)

    changed, seen = [], []
    for url in urls:
        try:
            res = api_get(url, {}, fetch=fetch)
        except Exception as err:
            log(f"gsite: {url} skipped ({err})")
            continue
        if not res.ok:
            log(f"gsite: {url} HTTP {res.status}")
            continue
        seen.append(url)
        rev = _revision(res.headers)
        existing = store.get("google_site", url)
        if not full and existing and rev and existing.revision_id == rev:
            continue  # server validator says unchanged
        title, text = extract_readable(res.text)
        title = title or url
        body = f"# {title}\n\n{text}"
        if store.upsert("google_site", url, title=title, url=url, revision_id=rev, body=body):
            changed.append(url)
            log(f"gsite: updated {url}")
    removed = [d for d in store.doc_ids("google_site") if d not in seen]
    for doc_id in removed:
        store.delete("google_site", doc_id)
    return {"changed": changed, "removed": removed}
