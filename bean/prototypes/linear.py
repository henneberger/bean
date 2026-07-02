"""Linear source. Tracks teams (by key) and indexes each issue's title, description, and
comments as one doc keyed by its identifier (TEAM-123). Auth is a personal API key sent raw in
the `Authorization` header (no "Bearer") against the GraphQL endpoint. Change detection is the
issue `updatedAt` as the revision id, with a per-team `linear.since.{TEAM}` cursor filtering the
query incrementally. Removing a team from config prunes its issues."""

from __future__ import annotations

import re

from ..http import api_json_post
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "linear"
API = "https://api.linear.app/graphql"

_ISSUES_Q = """query($t:String!,$after:String){
  issues(filter:{team:{key:{eq:$t}}}, first:50, after:$after, orderBy:updatedAt){
    nodes{ identifier title description updatedAt url state{name} assignee{name}
      comments{nodes{user{name} body}} }
    pageInfo{hasNextPage endCursor} } }"""

_ISSUES_Q_SINCE = """query($t:String!,$after:String,$since:DateTimeOrDuration!){
  issues(filter:{team:{key:{eq:$t}}, updatedAt:{gt:$since}}, first:50, after:$after, orderBy:updatedAt){
    nodes{ identifier title description updatedAt url state{name} assignee{name}
      comments{nodes{user{name} body}} }
    pageInfo{hasNextPage endCursor} } }"""


# -- refs + auth --------------------------------------------------------------------------------
def _headers(api_key: str) -> dict:
    return {"Authorization": api_key, "Content-Type": "application/json"}


def parse_add(item: str):
    s = item.strip()
    if s.startswith("linear:"):
        key = s.split(":", 1)[1]
        return ("teams", key) if key else None
    if "linear.app" in s:
        m = re.search(r"/issue/([A-Za-z][A-Za-z0-9_]*)-\d+", s)
        if m:
            return ("teams", m.group(1))
        m = re.search(r"/team/([A-Za-z][A-Za-z0-9_]*)", s)
        if m:
            return ("teams", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    api_key = token or key
    if not api_key:
        raise RuntimeError("pass --token <api-key> (create one at linear.app → Settings → API).")
    data = api_json_post(API, _headers(api_key), {"query": "{viewer{name email}}"}, fetch=fetch)
    who = ((data.get("data") or {}).get("viewer")) or {}
    save_credential(CRED, {"token": api_key, "name": who.get("name"), "email": who.get("email")})
    log(f"✓ Linear connected as {who.get('name') or who.get('email') or 'user'}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth linear --token lin_api_…`.")
    headers = _headers(cred["token"])
    teams = list(dict.fromkeys(config.get("teams", [])))

    changed = []
    for team in teams:
        cursor_key = f"linear.since.{team}"
        since = None if full else store.get_state(cursor_key)
        query = _ISSUES_Q_SINCE if since else _ISSUES_Q
        after, newest = None, since
        while True:
            variables = {"t": team, "after": after}
            if since:
                variables["since"] = since
            data = api_json_post(API, headers, {"query": query, "variables": variables},
                                 fetch=fetch)
            issues = ((data.get("data") or {}).get("issues")) or {}
            for node in issues.get("nodes", []):
                if _ingest(store, node, log):
                    changed.append(node.get("identifier"))
                newest = max(newest or "", node.get("updatedAt") or "")
            info = issues.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        if newest:
            store.set_state(cursor_key, newest)

    wanted = set(teams)
    removed = [d for d in store.doc_ids(CRED) if _team_of(d) not in wanted]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _team_of(doc_id: str) -> str:
    return doc_id.rsplit("-", 1)[0]


def _ingest(store, node, log) -> bool:
    ident = node.get("identifier")
    title = node.get("title") or ""
    state = ((node.get("state") or {}).get("name")) or "?"
    assignee = ((node.get("assignee") or {}).get("name")) or "unassigned"
    lines = [f"# {ident}: {title}",
             f"state: {state}  assignee: {assignee}", "",
             str(node.get("description") or "").strip(), ""]
    for c in (((node.get("comments") or {}).get("nodes")) or []):
        author = ((c.get("user") or {}).get("name")) or "?"
        lines += [f"**{author}**: {str(c.get('body') or '').strip()}", ""]
    body = "\n".join(lines)
    rev = node.get("updatedAt")
    meta = {"modified_at": node.get("updatedAt"), "author": assignee}
    if store.upsert(CRED, ident, title=f"{ident}: {title}", url=node.get("url"),
                    revision_id=rev, body=body, meta=meta):
        log(f"linear: updated {ident}")
        return True
    return False
