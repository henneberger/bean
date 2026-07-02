"""Generic IMAP mail source, one document per message. Works against any IMAP server (Fastmail,
Proton Bridge, self-hosted, corporate) using stdlib `imaplib`/`email` — no provider SDK. Auth is
the account email plus a password or app-password; the host (and optional :port) is stored in the
credential. Change detection is the message UID's server metadata (UID + INTERNALDATE), so an
unchanged message re-embeds nothing. The IMAP connection is injectable (`imap_factory=`) so sync()
runs offline in tests. Tracked scope is a list of mailboxes (default INBOX). A mail source never
prunes — messages can move mailboxes or age out of the search window without being deletions."""

from __future__ import annotations

from ..store import Store
from ..workspace import load_credential, save_credential

DEFAULT_PORT = 993


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`imap:MAILBOX` → the 'mailboxes' list (e.g. `imap:INBOX`, `imap:Archive`)."""
    s = item.strip()
    if s.lower().startswith("imap:"):
        mbox = s.split(":", 1)[1].strip()
        return ("mailboxes", mbox) if mbox else None
    return None


def _split_host(url: str) -> tuple[str, int]:
    host = (url or "").strip()
    for prefix in ("imaps://", "imap://", "https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    host = host.rstrip("/")
    if ":" in host:
        h, _, p = host.rpartition(":")
        try:
            return h, int(p)
        except ValueError:
            return host, DEFAULT_PORT
    return host, DEFAULT_PORT


def _login(host: str, port: int, email_addr: str, password: str):
    import imaplib  # stdlib; imported lazily so the module loads even where unused
    M = imaplib.IMAP4_SSL(host, port)
    M.login(email_addr, password)
    return M


def connect(*, url=None, email=None, token=None, secret=None, fetch=None, log=print, **_) -> dict:
    password = secret or token
    if not (url and email and password):
        raise RuntimeError(
            "pass --url imap.host[:port] --email you@host --secret <password-or-app-password>.")
    host, port = _split_host(url)
    M = _login(host, port, email, password)  # verify the login works, then drop it
    try:
        M.logout()
    except Exception:
        pass
    cred = {"host": host, "port": port, "email": email, "password": password}
    save_credential("imap", cred)
    log(f"✓ IMAP connected as {email} at {host}:{port}.")
    return cred


def connected() -> dict | None:
    return load_credential("imap")


# -- rendering ----------------------------------------------------------------------------------
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


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None,
         imap_factory=None) -> dict:
    import email as emaillib
    from datetime import datetime, timedelta

    cred = load_credential("imap")
    if not cred:
        raise RuntimeError("not connected — run `bean auth imap --url … --email … --secret …`.")
    factory = imap_factory or (lambda: _login(cred["host"], cred.get("port", DEFAULT_PORT),
                                              cred["email"], cred["password"]))
    mailboxes = list(config.get("mailboxes", [])) or ["INBOX"]
    since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%d-%b-%Y")

    M = factory()
    changed = []
    try:
        for mbox in mailboxes:
            typ, _ = M.select(mbox, readonly=True)
            if typ != "OK":
                log(f"imap: mailbox {mbox} not found")
                continue
            typ, data = (M.uid("search", None, "ALL") if full
                         else M.uid("search", None, "SINCE", since))
            if typ != "OK":
                log(f"imap: search failed in {mbox}")
                continue
            for uid in (data[0].split() if data and data[0] else []):
                uid = uid.decode() if isinstance(uid, bytes) else uid
                doc_id = f"{mbox}/{uid}"
                try:
                    typ, msgdata = M.uid("fetch", uid, "(RFC822 INTERNALDATE)")
                    if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
                        continue
                    rev = str(msgdata[0][0])  # UID + INTERNALDATE metadata line
                    existing = store.get("imap", doc_id)
                    if not full and existing and existing.revision_id == rev:
                        continue
                    msg = emaillib.message_from_bytes(msgdata[0][1])
                    body, title = _render_email(msg)
                    if store.upsert("imap", doc_id, title=title, url=None,
                                    revision_id=rev, body=body):
                        changed.append(doc_id)
                        log(f"imap: updated {doc_id}")
                except Exception as err:
                    log(f"imap: {doc_id} skipped ({err})")
                    continue
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return {"changed": changed, "removed": []}  # a mail source never prunes
