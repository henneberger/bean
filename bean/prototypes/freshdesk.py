"""Freshdesk source. Auth is HTTP Basic with the API key as the username and any string as the
password (`{api_key}:X`), stored per user with the account subdomain. Two doc kinds are indexed
whenever the source is connected: support TICKETS (subject + description + every conversation
reply/note) and solution ARTICLES (the KB, crawled categories → folders → articles). Ticket bodies
and article bodies arrive as HTML and are flattened with `html_to_text`. Change detection is each
object's `updated_at` as the revision id; a single malformed item is logged and skipped, never
aborting the sync. Tickets are paged newest-changed-first behind `updated_since`; this source does
not prune — it re-observes the whole collection each run and lets the content hash gate re-embeds."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

KINDS = ("tickets", "solutions")
PER_PAGE = 100


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`freshdesk:tickets` / `freshdesk:solutions` restrict which kinds sync (default: both)."""
    s = item.strip().lower()
    if s in ("freshdesk:tickets", "freshdesk:ticket"):
        return ("include", "tickets")
    if s in ("freshdesk:solutions", "freshdesk:solution", "freshdesk:articles"):
        return ("include", "solutions")
    return None


def connect(*, subdomain=None, url=None, email=None, key=None, token=None, secret=None,
            method=None, fetch=None, log=print) -> dict:
    key = key or token  # accept --key or --token for the API key
    if url and not subdomain:  # accept a full https://acme.freshdesk.com URL too
        subdomain = url.split("//", 1)[-1].split(".", 1)[0]
    if not (subdomain and key):
        raise RuntimeError(
            "pass --subdomain acme --key <api-key> "
            "(Freshdesk → profile picture → Profile Settings → 'Your API Key').")
    base = _base(subdomain)
    who = api_json(f"{base}/agents/me", _headers(key), fetch=fetch)
    contact = who.get("contact") or {}
    name = contact.get("name") or contact.get("email")
    save_credential("freshdesk", {"subdomain": subdomain, "key": key, "name": name})
    log(f"✓ Freshdesk connected as {name or subdomain} ({subdomain}).")
    return who


def connected() -> dict | None:
    return load_credential("freshdesk")


def _base(subdomain: str) -> str:
    return f"https://{subdomain}.freshdesk.com/api/v2"


def _headers(key: str) -> dict:
    # Freshdesk uses the API key as the username and any value as the password.
    raw = f"{key}:X".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Content-Type": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("freshdesk")
    if not cred:
        raise RuntimeError("not connected — run `bean auth freshdesk --subdomain … --key …`.")
    sub = cred["subdomain"]
    base = _base(sub)
    headers = _headers(cred["key"])
    include = set(config.get("include") or KINDS)

    changed: list[str] = []
    if "tickets" in include:
        changed += _sync_tickets(store, sub, base, headers, fetch, full, since_days, log)
    if "solutions" in include:
        changed += _sync_solutions(store, sub, base, headers, fetch, full, log)
    # Whole-collection re-scan every run; no incremental cursor to invalidate → never prune.
    return {"changed": changed, "removed": []}


def _sync_tickets(store, sub, base, headers, fetch, full, since_days, log) -> list[str]:
    since = None
    if not full:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    changed: list[str] = []
    page = 1
    while True:
        q = f"{base}/tickets?include=description&per_page={PER_PAGE}&page={page}&order_by=updated_at&order_type=asc"
        if since:
            q += f"&updated_since={since}"
        tickets = api_json(q, headers, fetch=fetch)
        if not isinstance(tickets, list):
            break
        for t in tickets:
            try:
                doc_id = f"ticket/{t['id']}"
                rev = t.get("updated_at")
                existing = store.get("freshdesk", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                body = _ticket_body(base, headers, fetch, t)
                url = f"https://{sub}.freshdesk.com/helpdesk/tickets/{t['id']}"
                if store.upsert("freshdesk", doc_id,
                                title=f"#{t['id']} {t.get('subject', '')}", url=url,
                                revision_id=rev, body=body,
                                meta={"created_at": t.get("created_at"), "modified_at": rev}):
                    changed.append(doc_id)
                    log(f"freshdesk: updated {doc_id}")
            except Exception as err:  # one bad ticket must never abort the whole sync
                log(f"freshdesk: ticket {t.get('id')} skipped ({err})")
        if len(tickets) < PER_PAGE:
            break
        page += 1
    return changed


def _ticket_body(base, headers, fetch, t) -> str:
    desc = t.get("description_text") or html_to_text(t.get("description") or "")
    lines = [f"# Ticket #{t['id']}: {t.get('subject', '')}",
             f"status: {t.get('status')}  priority: {t.get('priority')}", "",
             desc.strip(), ""]
    convos = api_json(f"{base}/tickets/{t['id']}/conversations", headers, fetch=fetch)
    for c in (convos if isinstance(convos, list) else []):
        text = c.get("body_text") or html_to_text(c.get("body") or "")
        if not text.strip():
            continue
        vis = "private" if c.get("private") else "public"
        lines += [f"**{c.get('from_email') or c.get('user_id')}** ({vis}): {text.strip()}", ""]
    return "\n".join(lines)


def _sync_solutions(store, sub, base, headers, fetch, full, log) -> list[str]:
    changed: list[str] = []
    try:
        cats = api_json(f"{base}/solutions/categories", headers, fetch=fetch)
    except Exception as err:
        log(f"freshdesk: solution categories skipped ({err})")
        return changed
    for cat in (cats if isinstance(cats, list) else []):
        try:
            folders = api_json(f"{base}/solutions/categories/{cat['id']}/folders", headers, fetch=fetch)
        except Exception as err:
            log(f"freshdesk: category {cat.get('id')} skipped ({err})")
            continue
        for fol in (folders if isinstance(folders, list) else []):
            try:
                arts = api_json(f"{base}/solutions/folders/{fol['id']}/articles", headers, fetch=fetch)
            except Exception as err:
                log(f"freshdesk: folder {fol.get('id')} skipped ({err})")
                continue
            for a in (arts if isinstance(arts, list) else []):
                try:
                    doc_id = f"article/{a['id']}"
                    rev = a.get("updated_at")
                    existing = store.get("freshdesk", doc_id)
                    if not full and existing and existing.revision_id == rev:
                        continue
                    body = f"# {a.get('title', '')}\n\n" + html_to_text(a.get("description") or "")
                    url = f"https://{sub}.freshdesk.com/support/solutions/articles/{a['id']}"
                    if store.upsert("freshdesk", doc_id, title=a.get("title", doc_id), url=url,
                                    revision_id=rev, body=body,
                                    meta={"created_at": a.get("created_at"), "modified_at": rev}):
                        changed.append(doc_id)
                        log(f"freshdesk: updated {doc_id}")
                except Exception as err:
                    log(f"freshdesk: article {a.get('id')} skipped ({err})")
    return changed
