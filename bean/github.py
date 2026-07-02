"""GitHub source. Tracks whole repos (`owner/repo`) and indexes their issues, pull requests
(body + comments), and Markdown files. Auth is a personal access token stored per user in the
credentials dir. Change detection: issues/PRs carry `updated_at` as the revision id and re-sync
incrementally via a per-repo `since` cursor; Markdown files use their git blob sha. Removing a
repo from config prunes everything under it."""

from __future__ import annotations

import base64
import re
from urllib.parse import urlencode

from .http import api_json
from .store import Store
from .workspace import load_credential, save_credential

API = "https://api.github.com"
REPO_RE = re.compile(r"(?:github\.com[/:])?([\w.-]+)/([\w.-]+?)(?:\.git|/.*)?$")


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    if item.startswith("#") or ("://" in item and "github.com" not in item):
        return None
    # Decline other connectors' scheme-prefixed refs (jira:, ms:, dropbox:, obsidian:, …) so the
    # bare owner/repo matcher below doesn't swallow the "a/b" tail of e.g. `ms:teams:T/C`.
    if "github.com" not in item and re.match(r"[a-z][a-z0-9+.-]*:", item):
        return None
    m = REPO_RE.search(item.strip())
    if m and not item.startswith(("/", "~", "./", "../")):
        return ("repos", f"{m.group(1)}/{m.group(2)}")
    return None


def connect(token: str, *, fetch=None, log=print) -> dict:
    who = api_json(f"{API}/user", _headers(token), fetch=fetch)
    save_credential("github", {"token": token, "login": who.get("login")})
    log(f"✓ GitHub connected as {who.get('login')}.")
    return who


def connected() -> dict | None:
    return load_credential("github")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "User-Agent": "bean"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("github")
    if not cred:
        raise RuntimeError("not connected — run `bean auth github --token ghp_…`.")
    headers = _headers(cred["token"])
    include = set(config.get("include") or ["issues", "pulls", "docs"])
    repos = list(dict.fromkeys(config.get("repos", [])))

    def paged(path: str, **params):
        page = 1
        while True:
            q = urlencode({**params, "per_page": 100, "page": page})
            batch = api_json(f"{API}{path}?{q}", headers, fetch=fetch)
            if not isinstance(batch, list) or not batch:
                return
            yield from batch
            if len(batch) < 100:
                return
            page += 1

    changed = []
    for repo in repos:
        cursor_key = f"github.since.{repo}"
        since = None if full else store.get_state(cursor_key)
        params = {"state": "all", "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        newest = since
        if include & {"issues", "pulls"}:
            for it in paged(f"/repos/{repo}/issues", **params):
                is_pr = "pull_request" in it
                if is_pr and "pulls" not in include:
                    continue
                if not is_pr and "issues" not in include:
                    continue
                changed += _ingest_issue(store, repo, it, headers, fetch, log)
                newest = max(newest or "", it.get("updated_at") or "")
        if include & {"docs"}:
            changed += _ingest_docs(store, repo, headers, fetch, full, log)
        if newest:
            store.set_state(cursor_key, newest)

    wanted = set(repos)
    removed = [d for d in store.doc_ids("github") if _repo_of(d) not in wanted]
    for doc_id in removed:
        store.delete("github", doc_id)
    return {"changed": changed, "removed": removed}


def _repo_of(doc_id: str) -> str:
    return re.split(r"[#:]", doc_id, 1)[0]


def _ingest_issue(store, repo, it, headers, fetch, log) -> list[str]:
    number = it["number"]
    doc_id = f"{repo}#{number}"
    kind = "PR" if "pull_request" in it else "issue"
    lines = [f"# {kind} #{number}: {it.get('title', '')}",
             f"state: {it.get('state')}  author: @{(it.get('user') or {}).get('login', '?')}", "",
             (it.get("body") or "").strip(), ""]
    if it.get("comments"):
        for c in api_json(f"{API}/repos/{repo}/issues/{number}/comments?per_page=100",
                          headers, fetch=fetch):
            lines += [f"**@{(c.get('user') or {}).get('login', '?')}**: {(c.get('body') or '').strip()}", ""]
    body = "\n".join(lines)
    if store.upsert("github", doc_id, title=f"{repo}#{number} {it.get('title', '')}",
                    url=it.get("html_link") or it.get("html_url"),
                    revision_id=it.get("updated_at"), body=body):
        log(f"github: updated {doc_id}")
        return [doc_id]
    return []


def _ingest_docs(store, repo, headers, fetch, full, log) -> list[str]:
    info = api_json(f"{API}/repos/{repo}", headers, fetch=fetch)
    branch = info.get("default_branch", "main")
    tree = api_json(f"{API}/repos/{repo}/git/trees/{branch}?recursive=1", headers, fetch=fetch)
    changed = []
    for node in tree.get("tree", []):
        if node.get("type") != "blob" or not str(node.get("path", "")).lower().endswith((".md", ".markdown")):
            continue
        doc_id = f"{repo}:{node['path']}"
        existing = store.get("github", doc_id)
        if not full and existing and existing.revision_id == node.get("sha"):
            continue
        blob = api_json(f"{API}/repos/{repo}/git/blobs/{node['sha']}", headers, fetch=fetch)
        body = base64.b64decode(blob.get("content", "")).decode("utf-8", "replace") \
            if blob.get("encoding") == "base64" else (blob.get("content") or "")
        if store.upsert("github", doc_id, title=f"{repo}/{node['path']}",
                        url=f"https://github.com/{repo}/blob/{branch}/{node['path']}",
                        revision_id=node.get("sha"), body=body):
            changed.append(doc_id)
            log(f"github: updated {doc_id}")
    return changed
