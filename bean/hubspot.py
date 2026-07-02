"""HubSpot source. Auth is a private-app access token (Bearer). Three doc kinds are indexed
whenever the source is connected: support TICKETS (subject + content + priority), NOTES (the CRM
note body, HTML → text), and knowledge-base ARTICLES. Objects are paged through the CRM v3 API
(`paging.next.after` cursors); knowledge-base articles come from the CMS/KB API and are tolerated
if the account does not expose them. Change detection is each object's `updatedAt` (falling back to
the `hs_lastmodifieddate` property) as the revision id. A single malformed object is logged and
skipped. This source re-observes the whole collection each run and does not prune."""

from __future__ import annotations

from .html import html_to_text
from .http import api_json
from .store import Store
from .workspace import load_credential, save_credential

API = "https://api.hubapi.com"
APP = "https://app.hubspot.com"
KINDS = ("tickets", "notes", "kb")
PAGE = 100


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`hubspot:tickets` / `hubspot:notes` / `hubspot:kb` restrict which kinds sync (default: all)."""
    s = item.strip().lower()
    if s in ("hubspot:tickets", "hubspot:ticket"):
        return ("include", "tickets")
    if s in ("hubspot:notes", "hubspot:note"):
        return ("include", "notes")
    if s in ("hubspot:kb", "hubspot:articles", "hubspot:knowledge"):
        return ("include", "kb")
    return None


def connect(*, token=None, key=None, fetch=None, log=print, **_) -> dict:
    token = token or key
    if not token:
        raise RuntimeError(
            "pass --token <private-app-token> (HubSpot → Settings → Integrations → "
            "Private Apps → create an app with CRM + Knowledge Base read scopes).")
    who = api_json(f"{API}/integrations/v1/me", _headers(token), fetch=fetch)
    portal = str(who.get("portalId") or "")
    save_credential("hubspot", {"token": token, "portal_id": portal})
    log(f"✓ HubSpot connected (portal {portal}).")
    return who


def connected() -> dict | None:
    return load_credential("hubspot")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("hubspot")
    if not cred:
        raise RuntimeError("not connected — run `bean auth hubspot --token …`.")
    headers = _headers(cred["token"])
    portal = cred.get("portal_id") or ""
    include = set(config.get("include") or KINDS)

    changed: list[str] = []
    if "tickets" in include:
        changed += _sync_objects(
            store, headers, fetch, full, log, kind="ticket",
            path="/crm/v3/objects/tickets",
            props=["subject", "content", "hs_ticket_priority", "createdate", "hs_lastmodifieddate"],
            title=lambda p, i: p.get("subject") or f"Ticket {i}",
            body=_ticket_body, url=lambda i: f"{APP}/contacts/{portal}/record/0-5/{i}")
    if "notes" in include:
        changed += _sync_objects(
            store, headers, fetch, full, log, kind="note",
            path="/crm/v3/objects/notes",
            props=["hs_note_body", "hs_timestamp", "hs_lastmodifieddate", "hs_createdate"],
            title=lambda p, i: (html_to_text(p.get("hs_note_body") or "")[:60] or f"Note {i}"),
            body=lambda p: html_to_text(p.get("hs_note_body") or ""),
            url=lambda i: f"{APP}/contacts/{portal}/objects/0-4/{i}")
    if "kb" in include:
        changed += _sync_kb(store, headers, fetch, full, log)
    return {"changed": changed, "removed": []}


def _sync_objects(store, headers, fetch, full, log, *, kind, path, props, title, body, url) -> list[str]:
    changed: list[str] = []
    after = None
    query = "&".join(f"properties={p}" for p in props)
    while True:
        u = f"{API}{path}?limit={PAGE}&{query}"
        if after:
            u += f"&after={after}"
        resp = api_json(u, headers, fetch=fetch)
        for obj in resp.get("results", []):
            try:
                oid = obj.get("id")
                props_d = obj.get("properties") or {}
                doc_id = f"{kind}/{oid}"
                rev = obj.get("updatedAt") or props_d.get("hs_lastmodifieddate")
                existing = store.get("hubspot", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                if store.upsert("hubspot", doc_id, title=title(props_d, oid), url=url(oid),
                                revision_id=rev, body=body(props_d),
                                meta={"created_at": obj.get("createdAt"), "modified_at": rev}):
                    changed.append(doc_id)
                    log(f"hubspot: updated {doc_id}")
            except Exception as err:  # one bad object must never abort the sync
                log(f"hubspot: {kind} {obj.get('id')} skipped ({err})")
        after = ((resp.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return changed


def _ticket_body(p: dict) -> str:
    lines = [f"# {p.get('subject', '')}",
             f"priority: {p.get('hs_ticket_priority')}", "",
             html_to_text(p.get("content") or "")]
    return "\n".join(lines)


def _sync_kb(store, headers, fetch, full, log) -> list[str]:
    changed: list[str] = []
    after = None
    while True:
        u = f"{API}/cms/v3/knowledge-base/articles?limit={PAGE}"
        if after:
            u += f"&after={after}"
        try:
            resp = api_json(u, headers, fetch=fetch)
        except Exception as err:  # account may not expose the KB API — tolerate and move on
            log(f"hubspot: knowledge base skipped ({err})")
            break
        for a in resp.get("results", []):
            try:
                aid = a.get("id")
                doc_id = f"article/{aid}"
                rev = a.get("updatedAt") or a.get("updated")
                existing = store.get("hubspot", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                html = a.get("htmlBody") or a.get("body") or a.get("content") or ""
                body = f"# {a.get('title', '')}\n\n" + html_to_text(html)
                if store.upsert("hubspot", doc_id, title=a.get("title", doc_id), url=a.get("url"),
                                revision_id=rev, body=body,
                                meta={"created_at": a.get("createdAt"), "modified_at": rev}):
                    changed.append(doc_id)
                    log(f"hubspot: updated {doc_id}")
            except Exception as err:
                log(f"hubspot: article {a.get('id')} skipped ({err})")
        after = ((resp.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return changed
