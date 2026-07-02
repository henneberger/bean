"""MediaWiki source. Indexes wiki articles over the MediaWiki Action API (`api.php`) using only
GET requests. Tracks whole categories (`categorymembers`) and individual page titles; each page's
plain-text extract (`prop=extracts&explaintext=1`) becomes the body. Change detection is the
page's latest revision id (`lastrevid`), falling back to `touched`, so unchanged pages re-embed
nothing. Public wikis need no auth — connect() only records the `api.php` URL (optional bot
username/password may be stored but read access here is anonymous, which covers most wikis).
Removing a category or page from config prunes its documents.

`crawl()` is factored out so wikipedia.py can drive the same Action API against a language edition."""

from __future__ import annotations

from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "mediawiki"
UA = {"User-Agent": "bean"}


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    if not s.lower().startswith("mediawiki:"):
        return None
    rest = s.split(":", 1)[1].strip()
    if not rest:
        return None
    if rest.lower().startswith("category:"):
        return ("categories", rest)
    return ("pages", rest)


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not url:
        raise RuntimeError(
            "pass --url https://wiki.example.org/w/api.php (the MediaWiki Action API endpoint; "
            "public wikis need no token, optional bot creds via --email <user> --token <pass>).")
    base = url.rstrip("/")
    info = _query(base, {"action": "query", "meta": "siteinfo"}, UA, fetch)
    site = ((info.get("query") or {}).get("general") or {}).get("sitename")
    cred = {"url": base, "user": email or None, "password": token or None, "sitename": site}
    save_credential(CRED, cred)
    log(f"✓ MediaWiki connected ({site or base}).")
    return cred


def connected() -> dict | None:
    return load_credential(CRED)


# -- Action API ---------------------------------------------------------------------------------
def _query(base: str, params: dict, headers: dict, fetch) -> dict:
    q = urlencode({**params, "format": "json", "formatversion": "2"})
    return api_json(f"{base}?{q}", headers, fetch=fetch)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _norm_category(name: str) -> str:
    return name if name.lower().startswith("category:") else f"Category:{name}"


def _category_members(base: str, category: str, headers: dict, fetch) -> list[str]:
    titles: list[str] = []
    cont = None
    while True:
        params = {"action": "query", "list": "categorymembers",
                  "cmtitle": _norm_category(category), "cmtype": "page", "cmlimit": "max"}
        if cont:
            params["cmcontinue"] = cont
        data = _query(base, params, headers, fetch)
        for m in (data.get("query") or {}).get("categorymembers", []):
            if m.get("title"):
                titles.append(m["title"])
        cont = (data.get("continue") or {}).get("cmcontinue")
        if not cont:
            break
    return titles


def _extracts(base: str, titles: list[str], headers: dict, fetch) -> list[dict]:
    params = {"action": "query", "prop": "extracts|info", "explaintext": "1",
              "exlimit": "max", "inprop": "url", "titles": "|".join(titles)}
    data = _query(base, params, headers, fetch)
    pages = (data.get("query") or {}).get("pages", [])
    return pages if isinstance(pages, list) else list(pages.values())


# -- sync ---------------------------------------------------------------------------------------
def crawl(store: Store, source: str, base: str, headers: dict, categories, pages, *,
          fetch=None, full: bool = False, log=lambda m: None) -> dict:
    changed, seen = [], []
    titles: list[str] = []
    for cat in categories:
        try:
            titles += _category_members(base, cat, headers, fetch)
        except Exception as err:  # one bad category must not abort the sync
            log(f"{source}: category {cat} skipped ({err})")
    titles += list(pages)
    titles = list(dict.fromkeys(t for t in titles if t))

    for chunk in _chunks(titles, 20):
        try:
            docs = _extracts(base, chunk, headers, fetch)
        except Exception as err:
            log(f"{source}: batch skipped ({err})")
            continue
        for page in docs:
            _ingest(store, source, page, seen, changed, full, log)

    removed = [d for d in store.doc_ids(source) if d not in seen]
    for d in removed:
        store.delete(source, d)
    return {"changed": changed, "removed": removed}


def _ingest(store, source, page, seen, changed, full, log) -> None:
    if page.get("missing") or page.get("pageid") is None:
        return
    pid = str(page["pageid"])
    seen.append(pid)
    rev = str(page.get("lastrevid") or page.get("touched") or "")
    existing = store.get(source, pid)
    if not full and existing and existing.revision_id == rev:
        return
    title = page.get("title") or "Untitled"
    body = f"# {title}\n\n" + (page.get("extract") or "")
    url = page.get("fullurl") or page.get("canonicalurl")
    if store.upsert(source, pid, title=title, url=url, revision_id=rev, body=body,
                    meta={"modified_at": page.get("touched")}):
        changed.append(pid)
        log(f"{source}: updated \"{title}\"")


def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth mediawiki --url …/api.php`.")
    cats = list(dict.fromkeys(config.get("categories", [])))
    pages = list(dict.fromkeys(config.get("pages", [])))
    return crawl(store, CRED, cred["url"], UA, cats, pages, fetch=fetch, full=full, log=log)
