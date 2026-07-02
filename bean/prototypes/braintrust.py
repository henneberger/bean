"""Braintrust source. Indexes an eval workspace's prompts, datasets, and projects as text
documents so they turn up in search. Auth is a Braintrust API key (Settings → API keys) over
Bearer. The list endpoints (`/v1/prompt`, `/v1/dataset`, `/v1/project`) are cursor-paginated via
`starting_after`. Change detection is the object's `_xact_id` (a monotonically increasing
transaction id) or `updated`/`created`, so unchanged objects re-embed nothing. doc_id is
`{kind}/{id}`. Tracking is workspace-wide (or narrowed to named `projects`); it always runs when
connected and never prunes — objects are keyed by id and simply appear/disappear from the lists."""

from __future__ import annotations

import json

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://api.braintrust.dev/v1"
PAGE = 100


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`braintrust:PROJECT` → the 'projects' list."""
    s = item.strip()
    if s.lower().startswith("braintrust:"):
        proj = s.split(":", 1)[1].strip()
        return ("projects", proj) if proj else None
    return None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def connect(*, token=None, key=None, fetch=None, log=print, **_) -> dict:
    api_key = token or key
    if not api_key:
        raise RuntimeError("pass --token <api-key> (Braintrust → Settings → API keys).")
    # cheap identity/validity check
    api_json(f"{API}/project?limit=1", _headers(api_key), fetch=fetch)
    save_credential("braintrust", {"token": api_key})
    log("✓ Braintrust connected.")
    return {"ok": True}


def connected() -> dict | None:
    return load_credential("braintrust")


# -- listing ------------------------------------------------------------------------------------
def _list_objects(headers: dict, kind: str, project: str | None, fetch) -> list[dict]:
    from urllib.parse import urlencode
    objects, after = [], None
    while True:
        params = {"limit": PAGE}
        if after:
            params["starting_after"] = after
        if project and kind != "project":
            params["project_name"] = project
        data = api_json(f"{API}/{kind}?{urlencode(params)}", headers, fetch=fetch)
        page = data.get("objects", [])
        objects += page
        if len(page) < PAGE:
            return objects
        after = page[-1].get("id")
        if not after:
            return objects


def _revision(obj: dict) -> str | None:
    rev = obj.get("_xact_id") or obj.get("updated") or obj.get("created")
    return str(rev) if rev is not None else None


# -- rendering ----------------------------------------------------------------------------------
def _render(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _render_prompt(obj: dict) -> tuple[str, str]:
    name = obj.get("name") or obj["id"]
    data = obj.get("prompt_data") or {}
    options = data.get("options") or {}
    block = data.get("prompt") or {}
    messages = block.get("messages") or []
    template = "\n".join(f"[{m.get('role', 'message')}] {_render(m.get('content'))}"
                         for m in messages) or _render(block.get("content"))
    lines = [f"# Prompt: {name}", ""]
    if obj.get("description"):
        lines.append(obj["description"])
    if options.get("model"):
        lines.append(f"Model: {options['model']}")
    if template:
        lines += ["", "Template:", template]
    return "\n".join(lines), f"Braintrust prompt: {name}"


def _render_dataset(obj: dict) -> tuple[str, str]:
    name = obj.get("name") or obj["id"]
    lines = [f"# Dataset: {name}", ""]
    if obj.get("description"):
        lines.append(obj["description"])
    if obj.get("project_id"):
        lines.append(f"Project id: {obj['project_id']}")
    return "\n".join(lines), f"Braintrust dataset: {name}"


def _render_project(obj: dict) -> tuple[str, str]:
    name = obj.get("name") or obj["id"]
    lines = [f"# Project: {name}", ""]
    if obj.get("description"):
        lines.append(obj["description"])
    return "\n".join(lines), f"Braintrust project: {name}"


_KINDS = {"prompt": _render_prompt, "dataset": _render_dataset, "project": _render_project}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("braintrust")
    if not cred:
        raise RuntimeError("not connected — run `bean auth braintrust --token …`.")
    headers = _headers(cred["token"])
    projects = list(config.get("projects", [])) or [None]

    changed, seen = [], []
    for kind, render in _KINDS.items():
        objs: dict[str, dict] = {}
        for project in projects:
            try:
                for obj in _list_objects(headers, kind, project, fetch):
                    if obj.get("id"):
                        objs[obj["id"]] = obj
            except RuntimeError as err:
                log(f"braintrust: listing {kind} skipped ({err})")
        for obj in objs.values():
            doc_id = f"{kind}/{obj['id']}"
            seen.append(doc_id)
            revision = _revision(obj)
            existing = store.get("braintrust", doc_id)
            if not full and revision and existing and existing.revision_id == revision:
                continue
            try:
                body, title = render(obj)
            except Exception as err:
                log(f"braintrust: {doc_id} skipped ({err})")
                continue
            if store.upsert("braintrust", doc_id, title=title, url=None,
                            revision_id=revision, body=body):
                changed.append(doc_id)
                log(f"braintrust: updated {doc_id}")
    return {"changed": changed, "removed": []}  # id-keyed objects, no prune
