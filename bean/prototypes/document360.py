"""Document360 source. Indexes every published article across your Document360 project versions
(a whole-collection source) as one doc, flattening the article HTML to text. Auth is an API token
sent in the `api_token` header. Each project version's category tree (including nested
child_categories) is walked to enumerate article ids, then each article's detail is fetched.
doc_id is `article/{id}` and the revision id is `modified_at`, so unchanged articles re-embed
nothing. Optionally restrict to `categories` (by name); never prunes (the project is the
collection)."""

from __future__ import annotations

from ..http import api_json
from ..store import Store
from ..html import html_to_text
from ..workspace import load_credential, save_credential

CRED = "document360"
API = "https://apihub.document360.io/v2"


# -- refs + auth --------------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {"api_token": token, "accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("document360:category:"):
        name = s.split(":", 2)[2]
        return ("categories", name) if name else None
    if s.startswith("document360:"):
        name = s.split(":", 1)[1]
        return ("categories", name) if name else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError(
            "pass --token <api token> (Document360 → Settings → API tokens).")
    res = api_json(f"{API}/ProjectVersions", _headers(token), fetch=fetch)
    if not isinstance(res, dict):
        raise RuntimeError("Document360 rejected the token.")
    save_credential(CRED, {"token": token})
    log("✓ Document360 connected.")
    return {"ok": True}


def connected() -> dict | None:
    return load_credential(CRED)


def _flatten(category: dict) -> list[dict]:
    out = [category]
    for child in category.get("child_categories") or []:
        out += _flatten(child)
    return out


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth document360 --token …`.")
    headers = _headers(cred["token"])
    want = set(config.get("categories", []))

    versions = api_json(f"{API}/ProjectVersions", headers, fetch=fetch).get("data") or []
    changed = []
    for ver in versions:
        vid = ver.get("id")
        if not vid:
            continue
        try:
            cats = api_json(f"{API}/ProjectVersions/{vid}/categories", headers,
                            fetch=fetch).get("data") or []
        except RuntimeError as err:
            log(f"document360: version {vid} skipped ({err})")
            continue
        for top in cats:
            for cat in _flatten(top):
                if want and cat.get("name") not in want:
                    continue
                for art in cat.get("articles") or []:
                    try:
                        _ingest(store, art, cat.get("name"), headers, fetch, full, changed, log)
                    except Exception as err:
                        log(f"document360: article {art.get('id')} skipped ({err})")
    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _ingest(store, art, category, headers, fetch, full, changed, log):
    aid = str(art.get("id"))
    doc_id = f"article/{aid}"
    detail = api_json(f"{API}/Articles/{aid}?langCode=en", headers, fetch=fetch).get("data") or {}
    rev = str(detail.get("modified_at") or "")
    existing = store.get(CRED, doc_id)
    if not full and existing and rev and existing.revision_id == rev:
        return
    title = detail.get("title") or art.get("title") or "Untitled"
    text = html_to_text(detail.get("html_content") or "")
    body = f"# {title}\n\n" + "\n\n".join(x for x in (detail.get("description"), text) if x)
    authors = detail.get("authors") or []
    meta = {"modified_at": detail.get("modified_at"), "created_at": detail.get("created_at"),
            "author": (authors[0].get("name") if authors else None)}
    if store.upsert(CRED, doc_id, title=title, url=detail.get("url") or "",
                    revision_id=rev or None, body=body, meta=meta):
        changed.append(doc_id)
        log(f"document360: updated \"{title}\"")
