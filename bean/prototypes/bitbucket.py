"""Bitbucket Cloud source. Tracks whole repos (`workspace/repo`) and indexes their pull requests
(description + comments) and Markdown files. Auth is an Atlassian account email + app password
sent as HTTP Basic (create an app password with Repositories:Read at bitbucket.org). Change
detection: PRs carry `updated_on` as the revision id and re-sync incrementally via a per-repo
`updated_on` cursor; Markdown files use the default branch head commit hash. Removing a repo
prunes everything under it."""

from __future__ import annotations

import base64
import re
from urllib.parse import quote

from ..http import api_get, api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "bitbucket"
API = "https://api.bitbucket.org/2.0"
_REPO_RE = re.compile(r"bitbucket\.org/([\w.-]+)/([\w.-]+?)(?:/.*)?/?$", re.I)


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    if s.startswith("bitbucket:"):
        path = s.split(":", 1)[1].strip("/")
        return ("repos", path) if path.count("/") == 1 else None
    if "bitbucket.org/" in s:
        m = _REPO_RE.search(s)
        if m:
            return ("repos", f"{m.group(1)}/{m.group(2)}")
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not email or not secret:
        raise RuntimeError(
            "pass --email <atlassian-email> --secret <app-password> (create an app password with "
            "Repositories:Read at bitbucket.org/account/settings/app-passwords).")
    who = api_json(f"{API}/user", _headers(email, secret), fetch=fetch)
    save_credential(CRED, {"email": email, "secret": secret,
                           "name": who.get("display_name") or who.get("username")})
    log(f"✓ Bitbucket connected as {who.get('display_name') or who.get('username')}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


def _headers(email: str, secret: str) -> dict:
    raw = f"{email}:{secret}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json"}


def _repo_of(doc_id: str) -> str:
    return re.split(r"[#:]", doc_id, 1)[0]


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth bitbucket --email … --secret …`.")
    headers = _headers(cred["email"], cred["secret"])
    include = set(config.get("include") or ["prs", "docs"])
    repos = list(dict.fromkeys(config.get("repos", [])))

    def paged(url: str):
        while url:
            data = api_json(url, headers, fetch=fetch)
            yield from data.get("values", [])
            url = data.get("next")

    changed = []
    for full_repo in repos:
        if "/" not in full_repo:
            continue
        ws, repo = full_repo.split("/", 1)
        if "prs" in include:
            cursor_key = f"bitbucket.since.{full_repo}"
            since = None if full else store.get_state(cursor_key)
            newest = since
            q = '(state="OPEN" OR state="MERGED" OR state="DECLINED")'
            if since:
                q += f' AND updated_on > "{since}"'
            url = f"{API}/repositories/{ws}/{repo}/pullrequests?pagelen=50&q={quote(q)}"
            for pr in paged(url):
                try:
                    if _ingest_pr(store, ws, repo, pr, headers, fetch, log):
                        changed.append(f"{full_repo}#{pr.get('id')}")
                    newest = max(newest or "", pr.get("updated_on") or "")
                except Exception as err:
                    log(f"bitbucket: PR skipped ({err})")
            if newest:
                store.set_state(cursor_key, newest)
        if "docs" in include:
            try:
                changed += _ingest_docs(store, ws, repo, headers, fetch, full, log)
            except Exception as err:
                log(f"bitbucket: docs skipped for {full_repo} ({err})")

    wanted = set(repos)
    removed = [d for d in store.doc_ids(CRED) if _repo_of(d) not in wanted]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _user(obj: dict) -> str:
    u = obj or {}
    return u.get("display_name") or u.get("nickname") or "?"


def _ingest_pr(store, ws, repo, pr, headers, fetch, log) -> bool:
    pid = pr.get("id")
    doc_id = f"{ws}/{repo}#{pid}"
    link = (((pr.get("links") or {}).get("html") or {}).get("href")
            or f"https://bitbucket.org/{ws}/{repo}/pull-requests/{pid}")
    lines = [f"# PR #{pid}: {pr.get('title', '')}",
             f"state: {pr.get('state')}  author: {_user(pr.get('author'))}", "",
             (pr.get("description") or "").strip(), ""]
    try:
        for c in _paged_comments(ws, repo, pid, headers, fetch):
            text = ((c.get("content") or {}).get("raw") or "").strip()
            if text:
                lines += [f"**{_user(c.get('user'))}**: {text}", ""]
    except Exception:
        pass
    body = "\n".join(lines)
    meta = {"modified_at": pr.get("updated_on"), "author": _user(pr.get("author"))}
    if store.upsert(CRED, doc_id, title=f"{ws}/{repo}#{pid} {pr.get('title', '')}",
                    url=link, revision_id=pr.get("updated_on"), body=body, meta=meta):
        log(f"bitbucket: updated {doc_id}")
        return True
    return False


def _paged_comments(ws, repo, pid, headers, fetch):
    url = f"{API}/repositories/{ws}/{repo}/pullrequests/{pid}/comments?pagelen=100"
    while url:
        data = api_json(url, headers, fetch=fetch)
        yield from data.get("values", [])
        url = data.get("next")


def _ingest_docs(store, ws, repo, headers, fetch, full, log) -> list[str]:
    info = api_json(f"{API}/repositories/{ws}/{repo}", headers, fetch=fetch)
    branch = ((info.get("mainbranch") or {}).get("name")) or "main"
    ref = api_json(f"{API}/repositories/{ws}/{repo}/refs/branches/{quote(branch, safe='')}",
                   headers, fetch=fetch)
    head = ((ref.get("target") or {}).get("hash")) or branch

    changed, stack = [], [""]
    while stack:
        d = stack.pop()
        url = f"{API}/repositories/{ws}/{repo}/src/{head}/{quote(d)}?pagelen=100"
        while url:
            data = api_json(url, headers, fetch=fetch)
            for e in data.get("values", []):
                kind, path = e.get("type"), e.get("path", "")
                if kind == "commit_directory":
                    stack.append(path.rstrip("/") + "/")
                elif kind == "commit_file" and path.lower().endswith((".md", ".markdown")):
                    doc_id = f"{ws}/{repo}:{path}"
                    existing = store.get(CRED, doc_id)
                    if not full and existing and existing.revision_id == head:
                        continue
                    res = api_get(f"{API}/repositories/{ws}/{repo}/src/{head}/{quote(path)}",
                                  headers, fetch=fetch)
                    if not res.ok:
                        continue
                    if store.upsert(CRED, doc_id, title=f"{ws}/{repo}/{path}",
                                    url=f"https://bitbucket.org/{ws}/{repo}/src/{branch}/{path}",
                                    revision_id=head, body=res.text):
                        changed.append(doc_id)
                        log(f"bitbucket: updated {doc_id}")
            url = data.get("next")
    return changed
