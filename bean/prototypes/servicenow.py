"""ServiceNow source. Indexes Knowledge Base articles and Incidents from the Table API. Auth is
either Basic (username/email + password given as `--secret`) or a Bearer token (`--token`) — the
method is chosen by which one you supply at connect time. Change detection: each record's
`sys_updated_on` is the revision id. Whole-collection source (index everything the account can
read), so it never prunes. HTML fields (article `text`) are flattened to text."""

from __future__ import annotations

import base64
from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..html import html_to_text
from ..workspace import load_credential, save_credential

PAGE = 100
DEFAULT_TABLES = ["kb_knowledge", "incident"]


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    item = item.strip().lower()
    if item in ("servicenow:kb_knowledge", "servicenow:knowledge"):
        return ("tables", "kb_knowledge")
    if item == "servicenow:incident":
        return ("tables", "incident")
    return None


def connect(*, subdomain=None, email=None, secret=None, token=None, url=None,
            fetch=None, log=print, **_ignored) -> dict:
    subdomain = subdomain or _subdomain_from_url(url)
    if not subdomain:
        raise RuntimeError("pass --subdomain <instance> (the part before .service-now.com).")
    if secret:
        cred = {"method": "basic", "subdomain": subdomain, "email": email, "secret": secret}
    elif token:
        cred = {"method": "token", "subdomain": subdomain, "token": token}
    else:
        raise RuntimeError("pass either --email … --secret <password> (Basic) or --token <bearer>.")
    api_json(f"{_base(subdomain)}/table/sys_user?sysparm_limit=1",
             _headers(cred), fetch=fetch)  # 2xx verifies the credentials
    save_credential("servicenow", cred)
    log(f"✓ ServiceNow connected ({subdomain}).")
    return cred


def connected() -> dict | None:
    return load_credential("servicenow")


def _subdomain_from_url(url):
    if url and ".service-now.com" in url:
        return url.split("//")[-1].split(".service-now.com")[0]
    return None


def _base(subdomain: str) -> str:
    return f"https://{subdomain}.service-now.com/api/now"


def _headers(cred: dict) -> dict:
    h = {"Accept": "application/json"}
    if cred.get("method") == "basic":
        raw = f"{cred.get('email') or ''}:{cred.get('secret') or ''}".encode()
        h["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        h["Authorization"] = f"Bearer {cred.get('token')}"
    return h


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("servicenow")
    if not cred:
        raise RuntimeError("not connected — run `bean auth servicenow --subdomain … --token …`.")
    subdomain, headers = cred["subdomain"], _headers(cred)
    tables = list(dict.fromkeys(config.get("tables") or DEFAULT_TABLES))
    changed = []

    for table in tables:
        offset = 0
        while True:
            q = urlencode({"sysparm_query": "ORDERBYDESCsys_updated_on",
                           "sysparm_limit": PAGE, "sysparm_offset": offset})
            try:
                resp = api_json(f"{_base(subdomain)}/table/{table}?{q}", headers, fetch=fetch)
            except RuntimeError as err:
                log(f"servicenow: {table} skipped ({err})")
                break
            rows = resp.get("result", [])
            for rec in rows:
                changed += _ingest(store, subdomain, table, rec, full, log)
            if len(rows) < PAGE:
                break
            offset += PAGE

    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _ingest(store, subdomain, table, rec, full, log) -> list[str]:
    sys_id = rec.get("sys_id")
    if not sys_id:
        return []
    doc_id = f"{table}/{sys_id}"
    rev = rec.get("sys_updated_on")
    existing = store.get("servicenow", doc_id)
    if not full and existing and existing.revision_id == rev:
        return []
    short = rec.get("short_description") or ""
    if table == "kb_knowledge":
        title = short or rec.get("number") or "Article"
        parts = [f"# {title}", html_to_text(rec.get("text") or "")]
    else:
        title = f"{rec.get('number') or table}: {short}".strip()
        parts = [f"# {title}", html_to_text(rec.get("description") or ""),
                 rec.get("comments") or "", rec.get("work_notes") or ""]
    body = "\n\n".join(x for x in parts if x)
    url = f"https://{subdomain}.service-now.com/{table}.do?sys_id={sys_id}"
    if store.upsert("servicenow", doc_id, title=title, url=url, revision_id=rev, body=body):
        log(f"servicenow: updated {doc_id}")
        return [doc_id]
    return []
