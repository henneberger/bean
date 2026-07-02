"""Productboard source. Indexes the whole workspace: every note and feature becomes one doc
keyed `note/{id}` or `feature/{id}`, with the HTML body (`content`/`description`) flattened to
text. Auth is a Bearer access token plus the required `X-Version: 1` header. Change detection is
`updatedAt` as the revision id. This is a whole-collection sync that runs whenever connected;
it does not prune (the API has no reliable tombstones), so removed is always empty."""

from __future__ import annotations

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "productboard"
API = "https://api.productboard.com"


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    if s.startswith("productboard:"):
        kind = s.split(":", 1)[1]
        return ("include", kind) if kind in ("notes", "features") else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError(
            "pass --token <access-token> (create one under Productboard → Settings → "
            "Integrations → Public API).")
    # Cheap identity/permission probe.
    api_json(f"{API}/features?pageLimit=1", _headers(token), fetch=fetch)
    save_credential(CRED, {"token": token})
    log("✓ Productboard connected.")
    return {"token": token}


def connected() -> dict | None:
    return load_credential(CRED)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Version": "1", "Accept": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth productboard --token …`.")
    headers = _headers(cred["token"])
    include = set(config.get("include") or ["notes", "features"])

    def paged(path: str):
        url = f"{API}{path}"
        while url:
            data = api_json(url, headers, fetch=fetch)
            yield from data.get("data", [])
            url = (data.get("links") or {}).get("next")

    changed = []
    if "notes" in include:
        for note in paged("/notes"):
            try:
                if _ingest(store, "note", note, note.get("title"), note.get("content"),
                           note.get("displayUrl"), log):
                    changed.append(f"note/{note.get('id')}")
            except Exception as err:
                log(f"productboard: note skipped ({err})")
    if "features" in include:
        for feat in paged("/features"):
            try:
                link = ((feat.get("links") or {}).get("html"))
                if _ingest(store, "feature", feat, feat.get("name"), feat.get("description"),
                           link, log):
                    changed.append(f"feature/{feat.get('id')}")
            except Exception as err:
                log(f"productboard: feature skipped ({err})")

    return {"changed": changed, "removed": []}


def _ingest(store, kind, obj, title, html, url, log) -> bool:
    oid = obj.get("id")
    doc_id = f"{kind}/{oid}"
    title = title or f"{kind} {oid}"
    body = f"# {title}\n\n{html_to_text(html or '')}"
    rev = obj.get("updatedAt") or obj.get("createdAt")
    meta = {"modified_at": obj.get("updatedAt"), "created_at": obj.get("createdAt")}
    if store.upsert(CRED, doc_id, title=title, url=url, revision_id=rev, body=body, meta=meta):
        log(f"productboard: updated {doc_id}")
        return True
    return False
