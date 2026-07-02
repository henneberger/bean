"""ClickUp source. Tracks lists and spaces (spaces expand to their lists) plus individually
added tasks, and indexes each task's name, description, and comments as one doc keyed by task
id. Auth is a personal API token (`pk_…`) sent verbatim in the Authorization header. Change
detection is the task `date_updated` (ms epoch) as the revision id. Every tracked list/space is
walked in full each sync, so pruning is a seen-set: any stored task id no longer produced is
removed."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "clickup"
API = "https://api.clickup.com/api/v2"
_TASK_URL_RE = re.compile(r"clickup\.com/t/([\w-]+)", re.I)


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    if s.startswith("clickup:"):
        rest = s.split(":", 1)[1]
        if rest.startswith("list:"):
            return ("lists", rest.split(":", 1)[1])
        if rest.startswith("space:"):
            return ("spaces", rest.split(":", 1)[1])
        if rest.startswith("task:"):
            return ("tasks", rest.split(":", 1)[1])
        return None
    if "clickup.com/t/" in s:
        m = _TASK_URL_RE.search(s)
        if m:
            return ("tasks", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError(
            "pass --token pk_… (create a personal token at app.clickup.com → Settings → Apps).")
    who = api_json(f"{API}/user", _headers(token), fetch=fetch)
    user = (who.get("user") or {})
    save_credential(CRED, {"token": token, "name": user.get("username")})
    log(f"✓ ClickUp connected as {user.get('username')}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


def _headers(token: str) -> dict:
    return {"Authorization": token, "Accept": "application/json"}


def _iso(ms) -> str | None:
    # ClickUp timestamps are milliseconds since epoch (as strings).
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth clickup --token pk_…`.")
    headers = _headers(cred["token"])

    # Resolve every tracked list id (config lists + lists inside tracked spaces).
    list_ids = list(dict.fromkeys(config.get("lists", [])))
    for sid in dict.fromkeys(config.get("spaces", [])):
        try:
            list_ids += [x for x in _space_lists(sid, headers, fetch) if x not in list_ids]
        except Exception as err:
            log(f"clickup: space {sid} skipped ({err})")

    changed, seen = [], set()
    for lid in list_ids:
        try:
            for task in _list_tasks(lid, headers, fetch):
                _handle(store, task, headers, fetch, changed, seen, log)
        except Exception as err:
            log(f"clickup: list {lid} skipped ({err})")
    for tid in dict.fromkeys(config.get("tasks", [])):
        try:
            q = urlencode({"include_markdown_description": "true"})
            task = api_json(f"{API}/task/{tid}?{q}", headers, fetch=fetch)
            _handle(store, task, headers, fetch, changed, seen, log)
        except Exception as err:
            log(f"clickup: task {tid} skipped ({err})")

    removed = [d for d in store.doc_ids(CRED) if d not in seen]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _space_lists(sid: str, headers, fetch) -> list[str]:
    ids = []
    folderless = api_json(f"{API}/space/{sid}/list?archived=false", headers, fetch=fetch)
    ids += [l["id"] for l in folderless.get("lists", [])]
    folders = api_json(f"{API}/space/{sid}/folder?archived=false", headers, fetch=fetch)
    for f in folders.get("folders", []):
        ids += [l["id"] for l in f.get("lists", [])]
    return ids


def _list_tasks(lid: str, headers, fetch):
    page = 0
    while True:
        q = urlencode({"include_closed": "true", "include_markdown_description": "true",
                       "page": page})
        resp = api_json(f"{API}/list/{lid}/task?{q}", headers, fetch=fetch)
        tasks = resp.get("tasks", [])
        yield from tasks
        if resp.get("last_page") is True or len(tasks) < 100:
            return
        page += 1


def _handle(store, task, headers, fetch, changed, seen, log) -> None:
    tid = task.get("id")
    if not tid:
        return
    seen.add(tid)
    try:
        if _ingest(store, task, headers, fetch, log):
            changed.append(tid)
    except Exception as err:
        log(f"clickup: task {tid} skipped ({err})")


def _ingest(store, task, headers, fetch, log) -> bool:
    tid = task["id"]
    name = task.get("name") or f"Task {tid}"
    status = ((task.get("status") or {}).get("status")) or "?"
    desc = task.get("markdown_description") or task.get("description") or ""
    lines = [f"# {name}", f"status: {status}", "", desc.strip(), ""]
    try:
        for c in api_json(f"{API}/task/{tid}/comment", headers, fetch=fetch).get("comments", []):
            who = (c.get("user") or {}).get("username", "?")
            text = (c.get("comment_text") or "").strip()
            if text:
                lines += [f"**{who}**: {text}", ""]
    except Exception:
        pass
    body = "\n".join(lines)
    meta = {"modified_at": _iso(task.get("date_updated"))}
    if store.upsert(CRED, tid, title=name,
                    url=task.get("url") or f"https://app.clickup.com/t/{tid}",
                    revision_id=str(task.get("date_updated")), body=body, meta=meta):
        log(f"clickup: updated {tid}")
        return True
    return False
