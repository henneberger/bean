"""Jira source. Tracks projects (by key) and indexes each issue's summary, description, and
comments as one doc keyed by issue key (PROJ-123). Auth supports both Atlassian Cloud (email +
API token → HTTP Basic) and Server/Data Center (PAT → Bearer), decided by whether an email was
supplied at connect time. Change detection is the issue `updated` timestamp as the revision id,
with a per-project `jira.since.{PROJ}` cursor for incremental JQL. Removing a project prunes its
issues."""

from __future__ import annotations

import base64
import re
from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "jira"


# -- refs + auth --------------------------------------------------------------------------------
def _atlassian_auth(cred: dict) -> dict:
    if cred.get("method") == "cloud":
        raw = f"{cred.get('email', '')}:{cred['token']}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
                "Accept": "application/json"}
    return {"Authorization": f"Bearer {cred['token']}", "Accept": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("jira:"):
        key = s.split(":", 1)[1]
        return ("projects", key) if key else None
    if "atlassian.net" in s or "/browse/" in s:
        m = re.search(r"/browse/([A-Za-z][A-Za-z0-9_]*)-\d+", s)
        if m:
            return ("projects", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token or not url:
        raise RuntimeError(
            "pass --url https://your.atlassian.net --token <token> (Cloud also needs --email; "
            "get a Cloud token at id.atlassian.com/manage-profile/security/api-tokens).")
    url = url.rstrip("/")
    method = "cloud" if email else "dc"
    cred = {"method": method, "url": url, "email": email, "token": token, "name": None}
    who = {}
    try:
        who = api_json(f"{url}/rest/api/2/myself", _atlassian_auth(cred), fetch=fetch)
    except RuntimeError:
        who = {}
    cred["name"] = who.get("displayName") or who.get("name") or email
    save_credential(CRED, cred)
    log(f"✓ Jira connected ({cred['name'] or url}).")
    return cred


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth jira --url … --token …`.")
    headers = _atlassian_auth(cred)
    base = cred["url"].rstrip("/")
    projects = list(dict.fromkeys(config.get("projects", [])))

    changed = []
    for proj in projects:
        cursor_key = f"jira.since.{proj}"
        since = None if full else store.get_state(cursor_key)
        jql = f'project={proj}'
        if since:
            jql += f' AND updated > "{since}"'
        jql += " ORDER BY updated DESC"
        newest = since
        start = 0
        fields = "summary,description,updated,status,assignee,reporter,comment"
        while True:
            q = urlencode({"jql": jql, "startAt": start, "maxResults": 50, "fields": fields})
            resp = api_json(f"{base}/rest/api/2/search?{q}", headers, fetch=fetch)
            issues = resp.get("issues", [])
            for issue in issues:
                doc_id = issue.get("key")
                if _ingest(store, base, issue, log):
                    changed.append(doc_id)
                upd = (issue.get("fields") or {}).get("updated") or ""
                newest = max(newest or "", upd)
            total = resp.get("total", 0)
            start += len(issues)
            if not issues or start >= total:
                break
        if newest:
            store.set_state(cursor_key, newest)

    wanted = set(projects)
    removed = [d for d in store.doc_ids(CRED) if _project_of(d) not in wanted]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _project_of(doc_id: str) -> str:
    return doc_id.rsplit("-", 1)[0]


def _ingest(store, base, issue, log) -> bool:
    key = issue.get("key")
    f = issue.get("fields") or {}
    summary = f.get("summary") or ""
    status = ((f.get("status") or {}).get("name")) or "?"
    reporter = ((f.get("reporter") or {}).get("displayName")) or "?"
    assignee = ((f.get("assignee") or {}).get("displayName")) or "unassigned"
    lines = [f"# {key}: {summary}",
             f"status: {status}  reporter: {reporter}  assignee: {assignee}", ""]
    desc = f.get("description")
    if desc:
        lines += [str(desc).strip(), ""]
    for c in (((f.get("comment") or {}).get("comments")) or []):
        author = ((c.get("author") or {}).get("displayName")) or "?"
        lines += [f"**{author}**: {str(c.get('body') or '').strip()}", ""]
    body = "\n".join(lines)
    rev = f.get("updated")
    meta = {"modified_at": f.get("updated"), "author": reporter}
    if store.upsert(CRED, key, title=f"{key}: {summary}", url=f"{base}/browse/{key}",
                    revision_id=rev, body=body, meta=meta):
        log(f"jira: updated {key}")
        return True
    return False
