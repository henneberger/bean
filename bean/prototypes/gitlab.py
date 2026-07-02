"""GitLab source. Tracks whole projects (`group/project`) and indexes their issues, merge
requests (description + notes), and Markdown files. Auth is a personal access token (scope
read_api) with a configurable base url so self-managed instances work too. Change detection:
issues/MRs carry `updated_at` as the revision id and re-sync incrementally via a per-project
`updated_after` cursor; Markdown files use their git blob sha. Removing a project prunes
everything under it."""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from ..http import api_get, api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "gitlab"
DEFAULT_URL = "https://gitlab.com"
# group/project path can be nested (group/sub/project); doc ids append #iid / !iid / :path.
_PROJECT_RE = re.compile(r"gitlab\.com/([^?#]+?)(?:/-/.*)?/?$", re.I)


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    if s.startswith("gitlab:"):
        path = s.split(":", 1)[1].strip("/")
        return ("projects", path) if "/" in path else None
    if "gitlab.com/" in s:
        m = _PROJECT_RE.search(s)
        if m:
            path = m.group(1).strip("/")
            return ("projects", path) if "/" in path else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError(
            "pass --token <pat> (create one with scope read_api at "
            "gitlab.com/-/user_settings/personal_access_tokens); --url for self-managed.")
    base = (url or DEFAULT_URL).rstrip("/")
    who = api_json(f"{base}/api/v4/user", _headers(token), fetch=fetch)
    save_credential(CRED, {"token": token, "url": base, "name": who.get("username")})
    log(f"✓ GitLab connected as {who.get('username')}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": "bean"}


def _project_of(doc_id: str) -> str:
    return re.split(r"[#!:]", doc_id, 1)[0]


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth gitlab --token <pat>`.")
    headers = _headers(cred["token"])
    base = cred["url"].rstrip("/")
    api = f"{base}/api/v4"
    include = set(config.get("include") or ["issues", "mrs", "docs"])
    projects = list(dict.fromkeys(config.get("projects", [])))

    def paged(path: str, **params):
        page = 1
        while True:
            q = urlencode({**params, "per_page": 100, "page": page})
            batch = api_json(f"{api}{path}?{q}", headers, fetch=fetch)
            if not isinstance(batch, list) or not batch:
                return
            yield from batch
            if len(batch) < 100:
                return
            page += 1

    changed = []
    for proj in projects:
        pid = quote(proj, safe="")
        cursor_key = f"gitlab.since.{proj}"
        since = None if full else store.get_state(cursor_key)
        newest = since
        params = {"order_by": "updated_at", "sort": "asc", "scope": "all"}
        if since:
            params["updated_after"] = since
        if "issues" in include:
            for it in paged(f"/projects/{pid}/issues", **params):
                try:
                    if _ingest_issue(store, base, proj, it, log):
                        changed.append(f"{proj}#{it.get('iid')}")
                    newest = max(newest or "", it.get("updated_at") or "")
                except Exception as err:
                    log(f"gitlab: issue skipped ({err})")
        if "mrs" in include:
            for mr in paged(f"/projects/{pid}/merge_requests", **params):
                try:
                    if _ingest_mr(store, base, proj, pid, mr, headers, fetch, log):
                        changed.append(f"{proj}!{mr.get('iid')}")
                    newest = max(newest or "", mr.get("updated_at") or "")
                except Exception as err:
                    log(f"gitlab: MR skipped ({err})")
        if "docs" in include:
            try:
                changed += _ingest_docs(store, api, base, proj, pid, headers, fetch, full, log)
            except Exception as err:
                log(f"gitlab: docs skipped for {proj} ({err})")
        if newest:
            store.set_state(cursor_key, newest)

    wanted = set(projects)
    removed = [d for d in store.doc_ids(CRED) if _project_of(d) not in wanted]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _author(obj: dict) -> str:
    a = obj.get("author") or {}
    return a.get("username") or a.get("name") or "?"


def _ingest_issue(store, base, proj, it, log) -> bool:
    iid = it.get("iid")
    doc_id = f"{proj}#{iid}"
    lines = [f"# issue #{iid}: {it.get('title', '')}",
             f"state: {it.get('state')}  author: @{_author(it)}", "",
             (it.get("description") or "").strip()]
    body = "\n".join(lines)
    meta = {"modified_at": it.get("updated_at"), "author": _author(it)}
    if store.upsert(CRED, doc_id, title=f"{proj}#{iid} {it.get('title', '')}",
                    url=it.get("web_url"), revision_id=it.get("updated_at"), body=body, meta=meta):
        log(f"gitlab: updated {doc_id}")
        return True
    return False


def _ingest_mr(store, base, proj, pid, mr, headers, fetch, log) -> bool:
    iid = mr.get("iid")
    doc_id = f"{proj}!{iid}"
    lines = [f"# MR !{iid}: {mr.get('title', '')}",
             f"state: {mr.get('state')}  author: @{_author(mr)}", "",
             (mr.get("description") or "").strip(), ""]
    try:
        for n in api_json(f"{_api(base)}/projects/{pid}/merge_requests/{iid}/notes?per_page=100",
                          headers, fetch=fetch):
            if n.get("system"):
                continue
            who = (n.get("author") or {}).get("username", "?")
            lines += [f"**@{who}**: {(n.get('body') or '').strip()}", ""]
    except Exception:
        pass
    body = "\n".join(lines)
    meta = {"modified_at": mr.get("updated_at"), "author": _author(mr)}
    if store.upsert(CRED, doc_id, title=f"{proj}!{iid} {mr.get('title', '')}",
                    url=mr.get("web_url"), revision_id=mr.get("updated_at"), body=body, meta=meta):
        log(f"gitlab: updated {doc_id}")
        return True
    return False


def _api(base: str) -> str:
    return f"{base.rstrip('/')}/api/v4"


def _ingest_docs(store, api, base, proj, pid, headers, fetch, full, log) -> list[str]:
    info = api_json(f"{api}/projects/{pid}", headers, fetch=fetch)
    branch = info.get("default_branch") or "main"
    changed = []
    page = 1
    while True:
        q = urlencode({"recursive": "true", "per_page": 100, "page": page})
        tree = api_json(f"{api}/projects/{pid}/repository/tree?{q}", headers, fetch=fetch)
        if not isinstance(tree, list) or not tree:
            break
        for node in tree:
            path = str(node.get("path", ""))
            if node.get("type") != "blob" or not path.lower().endswith((".md", ".markdown")):
                continue
            doc_id = f"{proj}:{path}"
            existing = store.get(CRED, doc_id)
            if not full and existing and existing.revision_id == node.get("id"):
                continue
            fp = quote(path, safe="")
            res = api_get(f"{api}/projects/{pid}/repository/files/{fp}/raw?ref={branch}",
                          headers, fetch=fetch)
            if not res.ok:
                continue
            if store.upsert(CRED, doc_id, title=f"{proj}/{path}",
                            url=f"{base}/{proj}/-/blob/{branch}/{path}",
                            revision_id=node.get("id"), body=res.text):
                changed.append(doc_id)
                log(f"gitlab: updated {doc_id}")
        if len(tree) < 100:
            break
        page += 1
    return changed
