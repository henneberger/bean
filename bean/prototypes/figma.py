"""Figma source. Tracks files by key and indexes their text: every TEXT node's `characters`
walked in document order, plus the file's comments. Auth is a personal access token
(`X-Figma-Token`). Change detection: the file's `version` (falling back to `lastModified`) is the
revision id, so an unchanged file re-embeds nothing. Removing a file from config prunes it."""

from __future__ import annotations

import re

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://api.figma.com/v1"
# figma.com/file/KEY/... or figma.com/design/KEY/...
URL_RE = re.compile(r"figma\.com/(?:file|design)/([A-Za-z0-9]+)", re.I)


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    item = item.strip()
    if item.startswith("figma:"):
        key = item[len("figma:"):].strip()
        return ("files", key) if key else None
    m = URL_RE.search(item)
    if m:
        return ("files", m.group(1))
    return None


def connect(*, token=None, fetch=None, log=print, **_ignored) -> dict:
    if not token:
        raise RuntimeError("pass --token … (create one at figma.com → Settings → Personal access tokens).")
    who = api_json(f"{API}/me", _headers(token), fetch=fetch)
    save_credential("figma", {"token": token, "handle": who.get("handle") or who.get("email")})
    log(f"✓ Figma connected as {who.get('handle') or who.get('email')}.")
    return who


def connected() -> dict | None:
    return load_credential("figma")


def _headers(token: str) -> dict:
    return {"X-Figma-Token": token}


# -- node tree ----------------------------------------------------------------------------------
def _collect_text(node: dict, out: list[str]) -> None:
    if node.get("type") == "TEXT" and node.get("characters"):
        out.append(node["characters"])
    for child in node.get("children", []) or []:
        _collect_text(child, out)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("figma")
    if not cred:
        raise RuntimeError("not connected — run `bean auth figma --token …`.")
    headers = _headers(cred["token"])
    keys = list(dict.fromkeys(config.get("files", [])))
    changed, seen = [], []

    for key in keys:
        doc_id = f"file/{key}"
        try:
            data = api_json(f"{API}/files/{key}", headers, fetch=fetch)
        except RuntimeError as err:
            log(f"figma: {key} skipped ({err})")
            continue
        seen.append(doc_id)
        rev = data.get("version") or data.get("lastModified")
        existing = store.get("figma", doc_id)
        if not full and existing and rev and existing.revision_id == rev:
            continue
        name = data.get("name") or key
        text: list[str] = []
        _collect_text(data.get("document", {}), text)
        lines = [f"# {name}", "", "\n".join(text)]
        try:
            comments = api_json(f"{API}/files/{key}/comments", headers, fetch=fetch)
            rows = comments.get("comments", [])
            if rows:
                lines += ["", "## Comments"]
                for c in rows:
                    who = (c.get("user") or {}).get("handle", "?")
                    lines.append(f"- {who}: {c.get('message', '')}")
        except RuntimeError as err:
            log(f"figma: {key} comments skipped ({err})")
        body = "\n".join(lines)
        meta = {"modified_at": data.get("lastModified")} if data.get("lastModified") else None
        if store.upsert("figma", doc_id, title=name, url=f"https://www.figma.com/file/{key}",
                        revision_id=rev, body=body, meta=meta):
            changed.append(doc_id)
            log(f"figma: updated \"{name}\"")

    tracked = {f"file/{k}" for k in keys}
    removed = [d for d in store.doc_ids("figma") if d not in tracked]
    for d in removed:
        store.delete("figma", d)
    return {"changed": changed, "removed": removed}
