"""Salesforce source. Indexes Knowledge articles and Cases via SOQL over the REST Data API. Auth
is an OAuth access token plus the org's instance URL (both stored at connect time). Change
detection: each record's `LastModifiedDate` is the revision id. This is a whole-collection source
(index everything the token can see), so it never prunes — records simply re-embed when their
LastModifiedDate advances. HTML bodies (Case descriptions, article rich text) are flattened."""

from __future__ import annotations

from urllib.parse import quote

from .http import api_json
from .store import Store
from .html import html_to_text
from .workspace import load_credential, save_credential

VERSION = "v59.0"
ARTICLE_SOQL = ("SELECT Id,Title,Summary,UrlName,LastModifiedDate FROM Knowledge__kav "
                "ORDER BY LastModifiedDate DESC")
CASE_SOQL = ("SELECT Id,CaseNumber,Subject,Description,Status,LastModifiedDate FROM Case "
             "ORDER BY LastModifiedDate DESC")


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    item = item.strip().lower()
    if item == "salesforce:articles":
        return ("objects", "articles")
    if item == "salesforce:cases":
        return ("objects", "cases")
    return None


def connect(*, token=None, url=None, fetch=None, log=print, **_ignored) -> dict:
    if not token or not url:
        raise RuntimeError("pass --token <access-token> --url <https://your.my.salesforce.com> "
                           "(from an OAuth flow / connected app).")
    url = url.rstrip("/")
    name = None
    try:
        who = api_json(f"{url}/services/oauth2/userinfo", _headers(token), fetch=fetch)
        name = who.get("name") or who.get("preferred_username")
    except Exception:
        api_json(f"{url}/services/data/", _headers(token), fetch=fetch)  # 2xx verifies the token
    save_credential("salesforce", {"token": token, "url": url, "name": name})
    log(f"✓ Salesforce connected ({name or url}).")
    return {"name": name, "url": url}


def connected() -> dict | None:
    return load_credential("salesforce")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# -- SOQL paging --------------------------------------------------------------------------------
def _query(instance: str, soql: str, headers: dict, fetch):
    url = f"{instance}/services/data/{VERSION}/query?q={quote(soql)}"
    while url:
        resp = api_json(url, headers, fetch=fetch)
        yield from resp.get("records", [])
        nxt = resp.get("nextRecordsUrl")
        url = f"{instance}{nxt}" if nxt else None


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("salesforce")
    if not cred:
        raise RuntimeError("not connected — run `bean auth salesforce --token … --url …`.")
    instance, headers = cred["url"].rstrip("/"), _headers(cred["token"])
    want = set(config.get("objects") or ["articles", "cases"])
    changed = []

    if "articles" in want:
        for rec in _query(instance, ARTICLE_SOQL, headers, fetch):
            changed += _ingest_article(store, instance, rec, full, log)
    if "cases" in want:
        for rec in _query(instance, CASE_SOQL, headers, fetch):
            changed += _ingest_case(store, instance, rec, full, log)

    return {"changed": changed, "removed": []}  # whole-collection source: never prune


def _ingest_article(store, instance, rec, full, log) -> list[str]:
    rid, doc_id = rec.get("Id"), f"article/{rec.get('Id')}"
    rev = rec.get("LastModifiedDate")
    existing = store.get("salesforce", doc_id)
    if not full and existing and existing.revision_id == rev:
        return []
    title = rec.get("Title") or rec.get("UrlName") or "Article"
    body = "\n\n".join(x for x in [f"# {title}", html_to_text(rec.get("Summary") or ""),
                                   f"urlName: {rec.get('UrlName')}" if rec.get("UrlName") else ""] if x)
    if store.upsert("salesforce", doc_id, title=title, url=f"{instance}/{rid}",
                    revision_id=rev, body=body):
        log(f"salesforce: updated {doc_id}")
        return [doc_id]
    return []


def _ingest_case(store, instance, rec, full, log) -> list[str]:
    rid, doc_id = rec.get("Id"), f"case/{rec.get('Id')}"
    rev = rec.get("LastModifiedDate")
    existing = store.get("salesforce", doc_id)
    if not full and existing and existing.revision_id == rev:
        return []
    title = f"Case {rec.get('CaseNumber')}: {rec.get('Subject') or ''}".strip()
    body = "\n\n".join(x for x in [f"# {title}", f"status: {rec.get('Status')}",
                                   html_to_text(rec.get("Description") or "")] if x)
    if store.upsert("salesforce", doc_id, title=title, url=f"{instance}/{rid}",
                    revision_id=rev, body=body):
        log(f"salesforce: updated {doc_id}")
        return [doc_id]
    return []
