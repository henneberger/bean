"""<Provider> connector for bean. Copy to ~/.bean/plugins/<name>.py and fill in the TODOs.

Auth: <one phrase — e.g. personal API token (Bearer)>. Change detection: <the revision signal, e.g.
each item's `updated_at`>. Tracks <what the lists hold, e.g. projects> and indexes <what a doc is>.

This is a drop-in plugin: it uses absolute `from bean.*` imports and exposes a module-level `SOURCE`
that bean's plugin loader picks up. Everything runs locally against the user's own credential; all
HTTP goes through the injectable `fetch` seam so it is offline-testable.
"""

from __future__ import annotations

from bean.http import api_json  # + api_json_post / api_get / api_post / AuthError as needed
from bean.sources import Source
from bean.workspace import load_credential, save_credential
# from bean.html import html_to_text   # if bodies are HTML

NAME = "provider"                                   # TODO: unique connector key (a-z0-9)
API = "https://api.provider.com/v1"                 # TODO: base URL


# -- auth ---------------------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}     # TODO: match the provider's scheme


def connect(*, token=None, url=None, email=None, subdomain=None, key=None, secret=None,
            method=None, fetch=None, log=print, **_) -> dict:
    if not token:
        raise RuntimeError("pass --token … (create one at https://provider.com/settings/tokens).")
    who = api_json(f"{API}/me", _headers(token), fetch=fetch)   # cheap identity check → verifies token
    save_credential(NAME, {"token": token, "name": who.get("name")})
    log(f"✓ {NAME.title()} connected as {who.get('name')}.")
    return who


def connected() -> dict | None:
    return load_credential(NAME)


# -- sync ---------------------------------------------------------------------------------------
def sync(store, config: dict, *, settings, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(NAME)
    if not cred:
        raise RuntimeError(f"not connected — run `bean auth {NAME} --token …`.")
    headers = _headers(cred["token"])
    changed, seen = [], []

    for thing in list(dict.fromkeys(config.get("things", []))):   # TODO: iterate your tracked items
        try:
            items = api_json(f"{API}/things/{thing}/items", headers, fetch=fetch)  # TODO: real endpoint
        except RuntimeError as err:
            log(f"{NAME}: {thing} skipped ({err})")
            continue
        for it in items.get("items", []):
            doc_id = str(it["id"])                    # TODO: a stable id (surviving edits)
            seen.append(doc_id)
            rev = it.get("updated_at")                # TODO: your cheap change signal
            existing = store.get(NAME, doc_id)
            if not full and existing and existing.revision_id == rev:
                continue                              # unchanged — skip the re-embed
            title = it.get("title") or doc_id
            body = f"# {title}\n\n" + (it.get("body") or "")   # TODO: html_to_text(...) if HTML
            if store.upsert(NAME, doc_id, title=title, url=it.get("url"), revision_id=rev,
                            body=body, meta={"modified_at": rev}):   # modified_at must be ISO
                changed.append(doc_id)
                log(f"{NAME}: updated {doc_id}")

    # Item-tracked sources prune vanished docs. A whole-collection source (index everything) should
    # instead `return {"changed": changed, "removed": []}` and set always_when_connected=True below.
    removed = [d for d in store.doc_ids(NAME) if d not in seen]
    for d in removed:
        store.delete(NAME, d)
    return {"changed": changed, "removed": removed}


# -- registration (the plugin loader reads this) ------------------------------------------------
SOURCE = Source(
    NAME, NAME, "Provider", ("things",), sync,
    auth=NAME,                                       # None if the source needs no credential
    add_help=f"{NAME}:THING or a provider.com/thing/… URL",
    auth_help="--token <api-token>",
    connect=connect, connected=connected,
    # interactive_auth=True,        # for browser / device-code flows (no token on the CLI)
    # always_when_connected=True,   # whole-collection sources: sync everything once authed, no prune
)
