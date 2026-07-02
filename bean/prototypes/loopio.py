"""Loopio source. Auth is OAuth2 client-credentials: a client id/secret pair (fields `--key` /
`--secret`) is exchanged at the token endpoint for a short-lived bearer token. Token acquisition is
an injectable `token_fn` (mirroring gdocs' `token_fn`) so `sync()` runs fully offline in tests. One
doc per library ENTRY is indexed whenever the source is connected: the entry's question(s) + answer
(HTML → text) + topic path. Entries are paged via `page`/`totalPages` behind an optional
`lastUpdatedDate` window. Change detection is each entry's `lastUpdatedDate` as the revision id; a
single bad entry is logged and skipped. An optional `include` list narrows to named stacks/topics.
This source re-observes the collection each run and does not prune."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from ..html import html_to_text
from ..http import api_json, api_json_post
from ..store import Store
from ..workspace import load_credential, save_credential

BASE = "https://api.loopio.com"
AUTH_URL = f"{BASE}/oauth2/access_token"
DATA_URL = f"{BASE}/data/v2"
PAGE = 100


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`loopio:stack:<name>` restricts indexing to a named stack/topic. Otherwise not ours."""
    s = item.strip()
    if s.lower().startswith("loopio:stack:"):
        return ("include", s.split(":", 2)[2].lower())
    return None


def connect(*, key=None, secret=None, subdomain=None, url=None, token=None, fetch=None,
            log=print, **_) -> dict:
    if url and not subdomain:
        subdomain = url.split("//", 1)[-1].split(".", 1)[0]
    if not (key and secret):
        raise RuntimeError(
            "pass --key <client-id> --secret <client-secret> [--subdomain acme] "
            "(Loopio → Account → LoopioGPT/API → create OAuth2 client credentials).")
    tok = _fetch_token(key, secret, fetch=fetch)  # verifies the credentials work
    if not tok:
        raise RuntimeError("Loopio returned no access token — check the client id/secret.")
    save_credential("loopio", {"key": key, "secret": secret, "subdomain": subdomain})
    log(f"✓ Loopio connected{f' ({subdomain})' if subdomain else ''}.")
    return {"ok": True}


def connected() -> dict | None:
    return load_credential("loopio")


def _fetch_token(client_id: str, client_secret: str, *, fetch=None) -> str:
    body = urlencode({"grant_type": "client_credentials",
                      "client_id": client_id, "client_secret": client_secret})
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    resp = api_json_post(AUTH_URL, headers, body, fetch=fetch)
    return resp.get("access_token")


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, token_fn=None, log=lambda m: None) -> dict:
    cred = load_credential("loopio")
    if not cred:
        raise RuntimeError("not connected — run `bean auth loopio --key … --secret …`.")
    # token_fn is injectable (offline tests pass a canned one); default mints a real bearer token.
    if token_fn is None:
        def token_fn():
            return _fetch_token(cred["key"], cred["secret"], fetch=fetch)
    token = token_fn()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    sub = cred.get("subdomain")
    include = {str(s).lower() for s in (config.get("include") or [])}  # optional filter

    flt: dict = {}
    if not full:
        start = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        flt["lastUpdatedDate"] = {"gte": start}

    changed: list[str] = []
    page, total_pages = 1, 1
    while page <= total_pages:
        params = {"pageSize": PAGE, "page": page}
        if flt:
            params["filter"] = json.dumps(flt)
        resp = api_json(f"{DATA_URL}/libraryEntries?{urlencode(params)}", headers, fetch=fetch)
        total_pages = resp.get("totalPages", 1) or 1
        for entry in resp.get("items", []):
            try:
                eid = entry.get("id")
                doc_id = f"entry/{eid}"
                topic = _topic(entry)
                if include and not any(t in include for t in topic.lower().split("/")):
                    continue
                rev = entry.get("lastUpdatedDate")
                existing = store.get("loopio", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                body, title = _entry_body(entry, topic)
                url = (f"https://{sub}.loopio.com/library?entry={eid}" if sub else None)
                if store.upsert("loopio", doc_id, title=title, url=url, revision_id=rev, body=body,
                                meta={"created_at": entry.get("createdDate"), "modified_at": rev,
                                      "author": (entry.get("creator") or {}).get("name")}):
                    changed.append(doc_id)
                    log(f"loopio: updated {doc_id}")
            except Exception as err:  # one bad entry must never abort the sync
                log(f"loopio: entry {entry.get('id')} skipped ({err})")
        page += 1
    return {"changed": changed, "removed": []}


def _topic(entry: dict) -> str:
    loc = entry.get("location") or {}
    parts = [p.get("name") for p in loc.values() if isinstance(p, dict) and p.get("name")]
    return "/".join(parts)


def _entry_body(entry: dict, topic: str):
    questions = [q.get("text", "").replace("\xa0", " ").strip()
                 for q in (entry.get("questions") or []) if q.get("text")]
    answer = html_to_text((entry.get("answer") or {}).get("text") or "")
    title = questions[0] if questions else f"Entry {entry.get('id')}"
    lines = [f"# {title}"]
    if topic:
        lines.append(f"topic: {topic}")
    lines += ["", answer]
    if len(questions) > 1:
        lines += ["", "## Related Questions", *[f"- {q}" for q in questions[1:]]]
    return "\n".join(lines), title
