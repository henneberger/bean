"""Zendesk source. Auth is HTTP Basic with an API token: the credential is
`base64("{email}/token:{apitoken}")`, stored per user. Two doc kinds are indexed whenever the
source is connected: support TICKETS (subject + description + public/internal comments) and
help-center ARTICLES (HTML → text). Tickets are pulled through the incremental export endpoint,
advancing a `zendesk.tickets.start_time` epoch cursor, so each sync only sees tickets touched since
last time; articles are paged in full. Change detection is each object's `updated_at` as the
revision id. A single malformed ticket is logged and skipped, never aborting the sync. Because the
ticket stream is incremental (we never re-observe the whole set) this source does not prune."""

from __future__ import annotations

import base64
import time

from .html import html_to_text
from .http import api_json
from .store import Store
from .workspace import load_credential, save_credential

DAY = 86400
KINDS = ("tickets", "articles")


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`zendesk:tickets` / `zendesk:articles` restrict which kinds sync (default: both)."""
    s = item.strip().lower()
    if s in ("zendesk:tickets", "zendesk:ticket"):
        return ("include", "tickets")
    if s in ("zendesk:articles", "zendesk:article"):
        return ("include", "articles")
    return None


def connect(*, subdomain=None, url=None, email=None, token=None, fetch=None, log=print) -> dict:
    if url and not subdomain:  # accept a full https://acme.zendesk.com URL too
        subdomain = url.split("//", 1)[-1].split(".", 1)[0]
    if not (subdomain and email and token):
        raise RuntimeError(
            "pass --subdomain acme --email you@acme.com --token <api-token> "
            "(Admin Center → Apps and integrations → APIs → Zendesk API → add token).")
    base = _base(subdomain)
    who = api_json(f"{base}/users/me.json", _headers(email, token), fetch=fetch)
    me = who.get("user") or {}
    save_credential("zendesk", {"subdomain": subdomain, "email": email, "token": token,
                                "name": me.get("name")})
    log(f"✓ Zendesk connected as {me.get('name') or email} ({subdomain}).")
    return who


def connected() -> dict | None:
    return load_credential("zendesk")


def _base(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com/api/v2"


def _headers(email: str, token: str) -> dict:
    raw = f"{email}/token:{token}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Content-Type": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, now: float | None = None,
         log=lambda m: None) -> dict:
    cred = load_credential("zendesk")
    if not cred:
        raise RuntimeError("not connected — run `bean auth zendesk --subdomain … --email … --token …`.")
    base = _base(cred["subdomain"])
    headers = _headers(cred["email"], cred["token"])
    now = now or time.time()

    include = set(config.get("include") or KINDS)
    brands = {str(b) for b in (config.get("brands") or [])}  # optional filter; empty = every brand

    changed: list[str] = []
    if "tickets" in include:
        changed += _sync_tickets(store, base, headers, fetch, full, since_days, now, brands, log)
    if "articles" in include:
        changed += _sync_articles(store, base, headers, fetch, full, brands, log)
    # Incremental ticket export never re-observes the full set, so pruning would delete everything.
    return {"changed": changed, "removed": []}


def _sync_tickets(store, base, headers, fetch, full, since_days, now, brands, log) -> list[str]:
    start = 0 if full else store.get_state("zendesk.tickets.start_time")
    if not start:
        start = int(now - since_days * DAY)
    changed: list[str] = []
    end = start
    while True:
        resp = api_json(f"{base}/incremental/tickets.json?start_time={start}", headers, fetch=fetch)
        for t in resp.get("tickets", []):
            try:
                if brands and str(t.get("brand_id")) not in brands:
                    continue
                doc_id = f"ticket/{t['id']}"
                rev = t.get("updated_at")
                existing = store.get("zendesk", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                body = _ticket_body(base, headers, fetch, t)
                if store.upsert("zendesk", doc_id, title=f"#{t['id']} {t.get('subject', '')}",
                                url=f"{base.rsplit('/api/', 1)[0]}/agent/tickets/{t['id']}",
                                revision_id=rev, body=body,
                                meta={"created_at": t.get("created_at"), "modified_at": rev}):
                    changed.append(doc_id)
                    log(f"zendesk: updated {doc_id}")
            except Exception as err:  # one bad ticket must never abort the whole sync
                log(f"zendesk: ticket {t.get('id')} skipped ({err})")
        end = resp.get("end_time") or end
        if resp.get("end_of_stream") or not resp.get("end_time") or resp.get("end_time") == start:
            break
        start = resp["end_time"]
    store.set_state("zendesk.tickets.start_time", end)
    return changed


def _ticket_body(base, headers, fetch, t) -> str:
    lines = [f"# Ticket #{t['id']}: {t.get('subject', '')}",
             f"status: {t.get('status')}  priority: {t.get('priority')}", "",
             (t.get("description") or "").strip(), ""]
    comments = api_json(f"{base}/tickets/{t['id']}/comments.json", headers, fetch=fetch)
    for c in comments.get("comments", []):
        text = c.get("plain_body") or html_to_text(c.get("html_body") or "") or (c.get("body") or "")
        vis = "public" if c.get("public", True) else "internal"
        lines += [f"**{c.get('author_id')}** ({vis}): {text.strip()}", ""]
    return "\n".join(lines)


def _sync_articles(store, base, headers, fetch, full, brands, log) -> list[str]:
    changed: list[str] = []
    page = 1
    while True:
        resp = api_json(f"{base}/help_center/articles.json?page={page}", headers, fetch=fetch)
        arts = resp.get("articles", [])
        for a in arts:
            if brands and str(a.get("brand_id")) not in brands:
                continue
            doc_id = f"article/{a['id']}"
            rev = a.get("updated_at")
            existing = store.get("zendesk", doc_id)
            if not full and existing and existing.revision_id == rev:
                continue
            body = f"# {a.get('title', '')}\n\n" + html_to_text(a.get("body") or "")
            if store.upsert("zendesk", doc_id, title=a.get("title", doc_id),
                            url=a.get("html_url"), revision_id=rev, body=body,
                            meta={"created_at": a.get("created_at"), "modified_at": rev}):
                changed.append(doc_id)
                log(f"zendesk: updated {doc_id}")
        if not resp.get("next_page") or not arts:
            break
        page += 1
    return changed
