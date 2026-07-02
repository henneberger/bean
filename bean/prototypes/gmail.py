"""Gmail source, one document per THREAD. Two auth methods, chosen at connect time and recorded
in the credential's `method`:

  * "gcloud" (default) — reuse the Google token gdocs already mints (`gdocs.access_token`) and call
    the Gmail REST API. Token acquisition is injected as `token_fn` so sync() runs offline in tests.
  * "imap"  — when the user authed with --email + an app-password --token, connect over IMAP with
    the stdlib (`imaplib`/`email`), one document per message.

Tracked scope is a list of search `queries` and/or `labels`. Change detection is the thread
`historyId` (gcloud) or the message `internalDate` (imap). A thread/mail source never prunes."""

from __future__ import annotations

import base64

from .. import gdocs
from ..http import AuthError, api_json
from ..store import Store
from ..workspace import load_credential, save_credential

REST = "https://gmail.googleapis.com/gmail/v1/users/me"
IMAP_HOST = "imap.gmail.com"


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`gmail:label:INBOX` → a label; `gmail:<query>` → a raw Gmail search."""
    s = item.strip()
    low = s.lower()
    if low.startswith("gmail:label:"):
        return ("labels", s.split(":", 2)[2])
    if low.startswith("gmail:"):
        return ("queries", s.split(":", 1)[1])
    return None


def connect(*, email=None, token=None, fetch=None, log=print) -> dict:
    if email and token:  # app-password path → verify an IMAP login works, then store method=imap
        M = _imap_login(email, token)
        M.logout()
        cred = {"method": "imap", "email": email, "token": token}
        save_credential("gmail", cred)
        log(f"✓ Gmail connected over IMAP as {email}.")
        return cred
    if not gdocs.connected():
        raise RuntimeError("Gmail (gcloud) needs a Google login first — run `bean auth google`, or "
                           "pass --email you@gmail.com --token <app-password> to use IMAP instead.")
    cred = {"method": "gcloud", "email": (gdocs.connected() or {}).get("account")}
    save_credential("gmail", cred)
    log("✓ Gmail connected (via Google/gcloud token).")
    return cred


def connected() -> dict | None:
    return load_credential("gmail")


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None,
         token_fn=gdocs.access_token, imap_factory=None) -> dict:
    cred = load_credential("gmail") or {}
    method = cred.get("method", "gcloud")
    if method == "imap":
        return _sync_imap(store, config, cred, full, since_days, log, imap_factory)
    return _sync_gcloud(store, config, fetch, full, log, token_fn)


def _sync_gcloud(store, config, fetch, full, log, token_fn) -> dict:
    token = token_fn()

    def call(path: str):
        nonlocal token
        try:
            return api_json(f"{REST}{path}", {"Authorization": f"Bearer {token}"}, fetch=fetch)
        except AuthError:
            token = token_fn(True)  # expired mid-sync — refresh once
            return api_json(f"{REST}{path}", {"Authorization": f"Bearer {token}"}, fetch=fetch)

    queries = list(config.get("queries", []))
    queries += [f"label:{lbl}" for lbl in config.get("labels", [])]
    if not queries:
        queries = ["in:inbox"]

    changed, seen = [], []
    for q in queries:
        from urllib.parse import quote
        resp = call(f"/threads?maxResults=50&q={quote(q)}")
        for th in resp.get("threads", []):
            tid, doc_id = th["id"], f"thread/{th['id']}"
            if doc_id in seen:
                continue
            seen.append(doc_id)
            rev = str(th.get("historyId"))
            existing = store.get("gmail", doc_id)
            if not full and existing and existing.revision_id == rev:
                continue
            full_thread = call(f"/threads/{tid}?format=full")
            body, title = _render_thread(full_thread)
            if store.upsert("gmail", doc_id, title=title,
                            url=f"https://mail.google.com/mail/u/0/#all/{tid}",
                            revision_id=str(full_thread.get("historyId") or rev), body=body):
                changed.append(doc_id)
                log(f"gmail: updated {doc_id}")
    return {"changed": changed, "removed": []}  # thread source never prunes


def _render_thread(thread: dict) -> tuple[str, str]:
    lines, title = [], None
    for msg in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}
        if title is None:
            title = headers.get("subject") or "(no subject)"
        lines += [f"From: {headers.get('from', '')}",
                  f"Subject: {headers.get('subject', '')}",
                  f"Date: {headers.get('date', '')}", "",
                  _payload_text(msg.get("payload") or {}).strip(), "", "---", ""]
    return "\n".join(lines), (title or "(no subject)")


def _payload_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain":
        data = (payload.get("body") or {}).get("data")
        return _b64url(data) if data else ""
    out = []
    for part in payload.get("parts") or []:
        out.append(_payload_text(part))
    return "\n".join(t for t in out if t)


def _b64url(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")


# -- IMAP path ----------------------------------------------------------------------------------
def _imap_login(email_addr: str, password: str):
    import imaplib  # stdlib; only imported on the IMAP branch so the module loads without it in use
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    M.login(email_addr, password)
    return M


def _sync_imap(store, config, cred, full, since_days, log, imap_factory) -> dict:
    import email as emaillib
    from datetime import datetime, timedelta
    factory = imap_factory or (lambda: _imap_login(cred["email"], cred["token"]))
    M = factory()
    mailboxes = list(config.get("labels", [])) or ["INBOX"]
    since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    changed, seen = [], []
    try:
        for mbox in mailboxes:
            typ, _ = M.select(mbox, readonly=True)
            if typ != "OK":
                log(f"gmail: mailbox {mbox} not found")
                continue
            typ, data = M.uid("search", None, "SINCE", since) if not full else M.uid("search", None, "ALL")
            for uid in (data[0].split() if data and data[0] else []):
                uid = uid.decode() if isinstance(uid, bytes) else uid
                doc_id = f"msg/{uid}"
                seen.append(doc_id)
                typ, msgdata = M.uid("fetch", uid, "(RFC822 INTERNALDATE)")
                if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
                    continue
                msg = emaillib.message_from_bytes(msgdata[0][1])
                rev = str(msgdata[0][0])  # carries INTERNALDATE
                existing = store.get("gmail", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                body, title = _render_email(msg)
                if store.upsert("gmail", doc_id, title=title, url=None, revision_id=rev, body=body):
                    changed.append(doc_id)
                    log(f"gmail: updated {doc_id}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return {"changed": changed, "removed": []}


def _render_email(msg) -> tuple[str, str]:
    subject = msg.get("Subject", "(no subject)")
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    else:
        payload = msg.get_payload(decode=True) or b""
        parts.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
    body = "\n".join([f"From: {msg.get('From', '')}", f"Subject: {subject}",
                      f"Date: {msg.get('Date', '')}", "", "\n".join(parts).strip()])
    return body, subject
