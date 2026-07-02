"""Trello source. Tracks boards (by id) and indexes each card's name, description, and comment
actions as one doc keyed by the card id. Auth is an API key + token, both sent as query params on
every request. Change detection is the card `dateLastActivity` as the revision id, so unchanged
cards re-embed nothing. Every tracked board is fully listed each sync, so removing a board (or a
card) prunes it."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "trello"
API = "https://api.trello.com/1"


# -- refs + auth --------------------------------------------------------------------------------
def _auth(cred: dict) -> dict:
    return {"key": cred["key"], "token": cred["token"]}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("trello:"):
        bid = s.split(":", 1)[1]
        return ("boards", bid) if bid else None
    if "trello.com" in s:
        m = re.search(r"trello\.com/b/([A-Za-z0-9]+)", s)
        if m:
            return ("boards", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    tok = token or secret
    if not key or not tok:
        raise RuntimeError("pass --key <api-key> --token <token> (get both at "
                           "trello.com/app-key).")
    who = api_json(f"{API}/members/me?" + urlencode({"key": key, "token": tok}), fetch=fetch)
    save_credential(CRED, {"key": key, "token": tok, "name": who.get("fullName") or who.get("username")})
    log(f"✓ Trello connected as {who.get('fullName') or who.get('username') or 'user'}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth trello --key … --token …`.")
    auth = _auth(cred)
    boards = list(dict.fromkeys(config.get("boards", [])))

    changed, seen = [], []
    for board in boards:
        params = {"fields": "name,desc,url,dateLastActivity", "actions": "commentCard",
                  "actions_limit": 50, **auth}
        cards = api_json(f"{API}/boards/{board}/cards?{urlencode(params)}", fetch=fetch)
        for card in cards:
            cid = str(card.get("id"))
            seen.append(cid)
            if _ingest(store, card, log, full):
                changed.append(cid)

    removed = [d for d in store.doc_ids(CRED) if d not in seen]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _ingest(store, card, log, full) -> bool:
    cid = str(card.get("id"))
    rev = card.get("dateLastActivity")
    existing = store.get(CRED, cid)
    if not full and existing and existing.revision_id == rev:
        return False
    name = card.get("name") or "Untitled"
    lines = [f"# {name}", "", (card.get("desc") or "").strip(), ""]
    for a in (card.get("actions") or []):
        if a.get("type") != "commentCard":
            continue
        author = ((a.get("memberCreator") or {}).get("fullName")) or "?"
        text = str(((a.get("data") or {}).get("text")) or "").strip()
        lines += [f"**{author}**: {text}", ""]
    body = "\n".join(lines)
    meta = {"modified_at": rev}
    if store.upsert(CRED, cid, title=name, url=card.get("url"),
                    revision_id=rev, body=body, meta=meta):
        log(f"trello: updated {cid} \"{name}\"")
        return True
    return False
