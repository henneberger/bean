"""Asana source. Tracks projects (by gid) and indexes each task's name, notes, and comment
stories as one doc keyed by the task gid. Auth is a personal access token (Bearer). Change
detection is the task `modified_at` as the revision id, so unchanged tasks re-embed nothing.
Every tracked project is fully listed each sync, so removing a project (or a task) prunes it."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "asana"
API = "https://app.asana.com/api/1.0"


# -- refs + auth --------------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("asana:"):
        gid = s.split(":", 1)[1]
        return ("projects", gid) if gid else None
    if "app.asana.com" in s:
        m = re.search(r"app\.asana\.com/\d+/(\d+)", s)
        if m:
            return ("projects", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError("pass --token <pat> (create one at app.asana.com → My Settings → Apps "
                           "→ Personal access token).")
    data = api_json(f"{API}/users/me", _headers(token), fetch=fetch)
    who = data.get("data") or {}
    save_credential(CRED, {"token": token, "name": who.get("name"), "gid": who.get("gid")})
    log(f"✓ Asana connected as {who.get('name') or 'user'}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth asana --token <pat>`.")
    headers = _headers(cred["token"])
    projects = list(dict.fromkeys(config.get("projects", [])))

    changed, seen = [], []
    for proj in projects:
        offset = None
        while True:
            params = {"opt_fields": "name,notes,modified_at,completed,permalink_url", "limit": 100}
            if offset:
                params["offset"] = offset
            resp = api_json(f"{API}/projects/{proj}/tasks?{urlencode(params)}", headers, fetch=fetch)
            for task in resp.get("data", []):
                gid = str(task.get("gid"))
                seen.append(gid)
                if _ingest(store, headers, fetch, task, log, full):
                    changed.append(gid)
            offset = ((resp.get("next_page") or {}) or {}).get("offset")
            if not offset:
                break

    # Every task of every tracked project is listed each sync, so `seen` is authoritative:
    # a task dropped from a project (or a project removed from config) is simply absent.
    removed = [d for d in store.doc_ids(CRED) if d not in seen]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _ingest(store, headers, fetch, task, log, full) -> bool:
    gid = str(task.get("gid"))
    rev = task.get("modified_at")
    existing = store.get(CRED, gid)
    if not full and existing and existing.revision_id == rev:
        return False
    name = task.get("name") or "Untitled"
    lines = [f"# {name}", "", (task.get("notes") or "").strip(), ""]
    stories = api_json(f"{API}/tasks/{gid}/stories?"
                       + urlencode({"opt_fields": "text,created_by.name,type"}),
                       headers, fetch=fetch)
    for s in stories.get("data", []):
        if s.get("type") != "comment":
            continue
        author = ((s.get("created_by") or {}).get("name")) or "?"
        lines += [f"**{author}**: {str(s.get('text') or '').strip()}", ""]
    body = "\n".join(lines)
    meta = {"modified_at": rev}
    if store.upsert(CRED, gid, title=name, url=task.get("permalink_url"),
                    revision_id=rev, body=body, meta=meta):
        log(f"asana: updated {gid} \"{name}\"")
        return True
    return False
