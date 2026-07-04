"""Microsoft 365 source over Graph — OneDrive/SharePoint FILES, Outlook MAIL (one doc per thread),
and Teams messages (one doc per message). Two auth methods, recorded in the
credential's `method`:

  * "device" (default) — device-code public-client flow against the well-known Azure CLI client id;
    the refresh token is stored and exchanged for a short-lived access token each sync.
  * "az"     — shell out to `az account get-access-token --resource https://graph.microsoft.com`,
    the same "ride the installed CLI" trick gdocs uses with gcloud.

Access-token acquisition is injected as `token_fn` so sync() runs fully offline in tests. File bodies
are downloaded via the item's pre-signed `@microsoft.graph.downloadUrl` and run through the same
office/pdf/text extractors as localfiles. Change detection: files by eTag/lastModifiedDateTime, mail
by the newest message's receivedDateTime, Teams messages carry no revision. Files prune; mail threads
and Teams messages never prune."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..html import html_to_text
from ..http import AuthError, api_get, api_json, api_json_post, api_post
from ..office import OFFICE_EXT, extract_office
from ..store import Store
from ..workspace import load_credential, save_credential

GRAPH = "https://graph.microsoft.com/v1.0"
CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # public Azure CLI client (no app registration)
AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
SCOPE = "offline_access Files.Read.All Mail.Read Chat.Read Sites.Read.All User.Read"
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".rst"}
HTML_EXT = {".html", ".htm"}
PDF_EXT = {".pdf"}


# -- auth ---------------------------------------------------------------------------------------
def connect(*, method=None, fetch=None, log=print, **_) -> dict:
    method = method or "device"
    if method == "az":
        tok = _az_token()  # verifies az is installed + logged in
        save_credential("microsoft", {"method": "az"})
        log("✓ Microsoft connected (via az CLI token).")
        return {"method": "az", "token_ok": bool(tok)}
    return _device_connect(fetch=fetch, log=log)


def connected() -> dict | None:
    return load_credential("microsoft")


def _device_connect(*, fetch=None, log=print) -> dict:
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    start = api_json_post(f"{AUTHORITY}/devicecode", form,
                          urlencode({"client_id": CLIENT_ID, "scope": SCOPE}), fetch=fetch)
    log(f"To connect Microsoft, open {start.get('verification_uri')} and enter code: "
        f"{start.get('user_code')}")
    interval = int(start.get("interval", 5))
    deadline = time.time() + int(start.get("expires_in", 900))
    body = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                      "client_id": CLIENT_ID, "device_code": start["device_code"]})
    while time.time() < deadline:
        time.sleep(interval)
        # authorization_pending returns 400 until the user finishes; poll the raw response.
        r = api_post(f"{AUTHORITY}/token", form, body, fetch=fetch)
        if r.ok:
            tok = r.json()
            save_credential("microsoft", {"method": "device", "refresh_token": tok["refresh_token"]})
            log("✓ Microsoft connected (device code).")
            return {"method": "device"}
    raise RuntimeError("Microsoft device-code sign-in timed out — run `bean auth microsoft` again.")


_token_cache: dict = {}


def _az_token() -> str:
    r = subprocess.run(["az", "account", "get-access-token", "--resource",
                        "https://graph.microsoft.com"], capture_output=True, text=True)
    if r.returncode != 0:
        why = (r.stderr or "").strip().splitlines()[-1:] or ["is az installed and logged in?"]
        raise AuthError(f"az could not mint a Graph token ({why[0]}) — run `az login` then retry.")
    return json.loads(r.stdout)["accessToken"]


def access_token(force: bool = False, *, fetch=None) -> str:
    cred = load_credential("microsoft")
    if not cred:
        raise AuthError("Microsoft is not connected — run `bean auth microsoft`.")
    if not force and _token_cache.get("exp", 0) > time.time():
        return _token_cache["token"]
    if cred.get("method") == "az":
        tok = _az_token()
    else:
        body = urlencode({"grant_type": "refresh_token", "client_id": CLIENT_ID, "scope": SCOPE,
                          "refresh_token": cred["refresh_token"]})
        resp = api_json_post(f"{AUTHORITY}/token",
                             {"Content-Type": "application/x-www-form-urlencoded"}, body, fetch=fetch)
        tok = resp["access_token"]
        if resp.get("refresh_token"):  # refresh tokens rotate — persist the new one
            save_credential("microsoft", {**cred, "refresh_token": resp["refresh_token"]})
    _token_cache.update(token=tok, exp=time.time() + 50 * 60)
    return tok


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, now: float | None = None,
         log=lambda m: None, token_fn=access_token) -> dict:
    token = token_fn()
    ocr_cfg = (settings or {}).get("ocr", {})

    def call(url: str):
        nonlocal token
        u = url if url.startswith("http") else f"{GRAPH}{url}"
        try:
            return api_json(u, {"Authorization": f"Bearer {token}"}, fetch=fetch)
        except AuthError:
            token = token_fn(True)
            return api_json(u, {"Authorization": f"Bearer {token}"}, fetch=fetch)

    changed, seen_files = [], []
    if config.get("drives"):
        c, s = _sync_files(store, config, call, fetch, token, token_fn, ocr_cfg, full, log)
        changed += c
        seen_files += s
    if config.get("mail"):
        changed += _sync_mail(store, config, call, full, log)
    if config.get("teams"):
        changed += _sync_teams(store, config, call, full, now, since_days, log)

    # Only files prune; mail threads and Teams messages never do.
    removed = [d for d in store.doc_ids("microsoft")
               if d.startswith("file/") and d not in seen_files]
    for doc_id in removed:
        store.delete("microsoft", doc_id)
    return {"changed": changed, "removed": removed}


def _sync_files(store, config, call, fetch, token, token_fn, ocr_cfg, full, log):
    changed, seen = [], []
    for drive in config.get("drives", []):
        for it in _crawl(call, drive, log):
            doc_id = f"file/{it['id']}"
            seen.append(doc_id)
            rev = it.get("eTag") or it.get("lastModifiedDateTime")
            existing = store.get("microsoft", doc_id)
            if not full and existing and existing.revision_id == rev:
                continue
            body = _download_text(it, fetch, ocr_cfg, log)
            if body is None:
                continue
            if store.upsert("microsoft", doc_id, title=it.get("name", doc_id),
                            url=it.get("webUrl"), revision_id=rev, body=body,
                            meta={"modified_at": it.get("lastModifiedDateTime"),
                                  "created_at": it.get("createdDateTime")}):
                changed.append(doc_id)
                log(f"microsoft: updated {doc_id} ({it.get('name')})")
    return changed, seen


def _crawl(call, drive, log):
    def children(item_id=None):
        if item_id:
            path = (f"/me/drive/items/{item_id}/children" if drive == "me"
                    else f"/drives/{drive}/items/{item_id}/children")
        else:
            path = "/me/drive/root/children" if drive == "me" else f"/drives/{drive}/root/children"
        url = path
        while url:
            resp = call(url)
            yield from resp.get("value", [])
            url = resp.get("@odata.nextLink")

    stack = [None]
    while stack:
        item_id = stack.pop()
        try:
            for it in children(item_id):
                if "folder" in it:  # facets can be empty dicts (falsy) — test membership, not truth
                    stack.append(it["id"])
                elif "file" in it:
                    yield it
        except RuntimeError as err:
            log(f"microsoft: drive {drive} listing stopped ({err})")


def _download_text(it, fetch, ocr_cfg, log):
    url = it.get("@microsoft.graph.downloadUrl")
    if not url:
        return None
    ext = os.path.splitext(it.get("name", ""))[1].lower()
    if ext not in TEXT_EXT | HTML_EXT | OFFICE_EXT | PDF_EXT:
        return None
    try:
        res = api_get(url, {}, fetch=fetch)  # pre-signed; no auth header
        if not res.ok:
            log(f"microsoft: download failed for {it.get('name')} (HTTP {res.status})")
            return None
        if ext in TEXT_EXT:
            return res.text
        if ext in HTML_EXT:
            return html_to_text(res.text)
        data = res.text.encode("utf-8", "surrogateescape")  # binary round-trips through the fetch seam
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            if ext in OFFICE_EXT:
                from pathlib import Path
                return extract_office(Path(tmp))
            if ext in PDF_EXT:
                from ..pdf import extract_pdf
                return extract_pdf(tmp, ocr_cfg, log=log)
        finally:
            os.unlink(tmp)
    except Exception as err:  # one bad file must not abort the sync
        log(f"microsoft: {it.get('name')} skipped ({err})")
    return None


def _sync_mail(store, config, call, full, log):
    fields = ("subject,from,receivedDateTime,bodyPreview,body,conversationId")
    changed, threads = [], {}
    for mbox in config.get("mail", []) or ["inbox"]:
        folder = f"/me/mailFolders/{mbox}/messages" if mbox else "/me/messages"
        q = urlencode({"$select": fields, "$top": "50", "$orderby": "receivedDateTime desc"},
                      safe="$,")
        url = f"{folder}?{q}"
        while url:
            resp = call(url)
            for m in resp.get("value", []):
                threads.setdefault(m.get("conversationId") or m.get("id"), []).append(m)
            url = resp.get("@odata.nextLink")
    for conv_id, msgs in threads.items():
        doc_id = f"mail/{conv_id}"
        msgs.sort(key=lambda m: m.get("receivedDateTime") or "")
        rev = msgs[-1].get("receivedDateTime")
        existing = store.get("microsoft", doc_id)
        if not full and existing and existing.revision_id == rev:
            continue
        body, title = _render_mail(msgs)
        if store.upsert("microsoft", doc_id, title=title, url=None, revision_id=rev, body=body,
                        meta={"modified_at": rev}):
            changed.append(doc_id)
            log(f"microsoft: updated {doc_id}")
    return changed  # mail thread docs never prune


def _render_mail(msgs) -> tuple[str, str]:
    title = msgs[0].get("subject") or "(no subject)"
    lines = []
    for m in msgs:
        frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        content = m.get("body") or {}
        text = html_to_text(content.get("content", "")) if content.get("contentType") == "html" \
            else content.get("content") or m.get("bodyPreview", "")
        lines += [f"From: {frm}", f"Subject: {m.get('subject', '')}",
                  f"Date: {m.get('receivedDateTime', '')}", "", text.strip(), "", "---", ""]
    return "\n".join(lines), title


def _sync_teams(store, config, call, full, now, since_days, log):
    changed = []
    for spec in config.get("teams", []):
        try:
            team_id, channel_id = spec.split("/", 1)
        except ValueError:
            log(f"microsoft: teams spec {spec!r} must be TEAMID/CHANNELID")
            continue
        try:
            ch = call(f"/teams/{team_id}/channels/{channel_id}")
            name = ch.get("displayName") or channel_id
        except RuntimeError as err:
            log(f"microsoft: teams channel {spec} skipped ({err})")
            continue
        messages, url = [], f"/teams/{team_id}/channels/{channel_id}/messages"
        while url:
            resp = call(url)
            messages += resp.get("value", [])
            url = resp.get("@odata.nextLink")
        # One document per message, so `recent` surfaces individual recent messages.
        for m in messages:
            iso = m.get("createdDateTime")
            if _iso_epoch(iso) is None:
                continue
            rendered = _render_teams_message(m)
            if not rendered.strip():
                continue
            who = (((m.get("from") or {}).get("user") or {}).get("displayName")) or "unknown"
            doc_id = f"{name}/{m.get('id')}"
            if store.upsert("microsoft", doc_id, title=f"{name}: {_teams_subject(m)}",
                            url=m.get("webUrl"), revision_id=None, body=rendered,
                            meta={"author": who, "created_at": iso, "modified_at": iso}):
                changed.append(doc_id)
                log(f"microsoft: updated teams {name} {m.get('id')}")
    return changed


def _teams_text(m: dict) -> str:
    content = m.get("body") or {}
    return (html_to_text(content.get("content", "")) if content.get("contentType") == "html"
            else content.get("content") or "").strip()


def _render_teams_message(m: dict) -> str:
    who = (((m.get("from") or {}).get("user") or {}).get("displayName")) or "unknown"
    return f"**{who}** ({m.get('createdDateTime', '')}): {_teams_text(m)}"


def _teams_subject(m: dict) -> str:
    return " ".join(_teams_text(m).split())[:80] or "(no text)"


def _iso_epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None
