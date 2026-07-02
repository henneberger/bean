"""Wikipedia source. A thin specialization of the MediaWiki connector against a language
edition's Action API (`https://{lang}.wikipedia.org/w/api.php`, default `en`). No secret: connect()
just records the language edition. Tracks article titles and categories; each article's plain
extract becomes the body, with the latest revision id as the change signal. Reuses mediawiki.crawl
so the Action API handling lives in one place. Removing a page/category from config prunes it."""

from __future__ import annotations

from urllib.parse import unquote

from . import mediawiki
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "wikipedia"


# -- refs + auth --------------------------------------------------------------------------------
def _base(language: str) -> str:
    return f"https://{language}.wikipedia.org/w/api.php"


def parse_add(item: str):
    s = item.strip()
    low = s.lower()
    if "wikipedia.org/wiki/" in low:
        title = unquote(s.split("/wiki/", 1)[1].split("#")[0]).replace("_", " ").strip()
        if not title:
            return None
        return ("categories", title) if title.lower().startswith("category:") else ("pages", title)
    if low.startswith("wikipedia:"):
        rest = s.split(":", 1)[1].strip()
        if not rest:
            return None
        return ("categories", rest) if rest.lower().startswith("category:") else ("pages", rest)
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    lang = (url or method or "en").strip().lower() or "en"
    if "wikipedia.org" in lang:  # accept a full en.wikipedia.org URL and pull the subdomain
        lang = lang.split("//", 1)[-1].split(".wikipedia", 1)[0].split("/")[-1] or "en"
    base = _base(lang)
    mediawiki._query(base, {"action": "query", "meta": "siteinfo"}, mediawiki.UA, fetch)
    save_credential(CRED, {"language": lang, "url": base})
    log(f"✓ Wikipedia connected ({lang}).")
    return {"language": lang, "url": base}


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED) or {"language": "en", "url": _base("en")}
    base = cred.get("url") or _base(cred.get("language", "en"))
    cats = list(dict.fromkeys(config.get("categories", [])))
    pages = list(dict.fromkeys(config.get("pages", [])))
    return mediawiki.crawl(store, CRED, base, mediawiki.UA, cats, pages,
                           fetch=fetch, full=full, log=log)
