"""Google Docs source. Auth rides on gcloud (`gcloud auth login --enable-gdrive-access`
uses Google's own pre-verified OAuth client — no GCP project, no consent screen to set up);
`gcloud auth print-access-token` mints Drive-scoped tokens per sync. Change detection is
per-doc headRevisionId with the content hash as final authority. Bodies come from the Drive
export endpoint as Markdown, falling back to plain text."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .http import AuthError, api_get, api_json
from .store import Store
from .workspace import load_credential, save_credential

API = "https://www.googleapis.com/drive/v3"
DOC_FIELDS = ("id,name,createdTime,modifiedTime,headRevisionId,webViewLink,trashed,mimeType,"
              "lastModifyingUser(displayName,emailAddress),owners(displayName,emailAddress)")


# -- refs ---------------------------------------------------------------------------------------
def parse_ref(s: str) -> tuple[str, str] | None:
    """('doc'|'folder', id) from a URL or bare id; None when it isn't a Google ref."""
    m = re.search(r"docs\.google\.com/document/d/([\w-]+)", s)
    if m:
        return ("doc", m.group(1))
    m = re.search(r"drive\.google\.com/(?:[^\s]*/)?folders/([\w-]+)", s)
    if m:
        return ("folder", m.group(1))
    if re.fullmatch(r"[\w-]{20,}", s):
        return ("doc", s)
    return None


# -- gcloud auth ----------------------------------------------------------------------------------
def find_gcloud() -> str | None:
    return shutil.which("gcloud")


def connect(log=print) -> None:
    """`bean auth google` — interactive browser sign-in through gcloud."""
    bin_ = find_gcloud()
    if not bin_:
        raise RuntimeError(
            "gcloud is not installed. Install it (https://cloud.google.com/sdk/docs/install, or "
            "`brew install google-cloud-sdk`) and run `bean auth google` again — signing in "
            "through gcloud means you never have to set anything up in Google Cloud."
        )
    log("A browser window will open — sign in with the account that can see your docs, then click Allow.")
    r = subprocess.run([bin_, "auth", "login", "--enable-gdrive-access", "--brief"])
    if r.returncode != 0:
        raise RuntimeError("Google sign-in did not complete — run `bean auth google` to try again.")
    account = subprocess.run([bin_, "config", "get-value", "account"],
                             capture_output=True, text=True).stdout.strip()
    save_credential("google", {"method": "gcloud", "account": account or None})
    log(f"✓ Google connected{f' as {account}' if account else ''}.")


_token_cache: dict = {}


def access_token(force: bool = False) -> str:
    cred = load_credential("google")
    if not cred:
        raise AuthError("Google is not connected — run `bean auth google`.")
    if not force and _token_cache.get("exp", 0) > time.time():
        return _token_cache["token"]
    bin_ = find_gcloud()
    if not bin_:
        raise AuthError("gcloud is gone — run `bean auth google` again.")
    r = subprocess.run([bin_, "auth", "print-access-token"], capture_output=True, text=True)
    if r.returncode != 0:
        why = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise AuthError(f"gcloud could not mint a token ({why[0]}) — run `bean auth google` again.")
    _token_cache.update(token=r.stdout.strip(), exp=time.time() + 50 * 60)
    return _token_cache["token"]


def connected() -> dict | None:
    return load_credential("google")


# -- sync -----------------------------------------------------------------------------------------
def _crawl(call, q: str) -> list[str]:
    """All doc ids matching a Drive query `q`, paged."""
    ids: list[str] = []
    page = None
    while True:
        params = urlencode({
            "q": q, "fields": "nextPageToken,files(id)", "pageSize": "100",
            "corpora": "user", "orderBy": "modifiedTime desc",
            **({"pageToken": page} if page else {}),
        })
        resp = call(f"/files?{params}")
        ids += [f["id"] for f in resp.get("files", [])]
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


def sync(store: Store, config: dict, *, token_fn=access_token, fetch=None,
         full: bool = False, lookback_days: int = 30, log=lambda m: None) -> dict:
    token = token_fn()

    def call(path: str, raw: bool = False):
        nonlocal token
        url = f"{API}{path}"
        try:
            return (api_get if raw else api_json)(url, {"Authorization": f"Bearer {token}"}, fetch=fetch)
        except AuthError:
            token = token_fn(True)  # expired mid-sync — refresh once
            return (api_get if raw else api_json)(url, {"Authorization": f"Bearer {token}"}, fetch=fetch)

    DOC_MIME = "mimeType='application/vnd.google-apps.document' and trashed=false"

    # Explicit adds narrow the scope; with none configured, auto-index the docs you own.
    doc_ids = list(dict.fromkeys(config.get("docs", [])))
    for folder in config.get("folders", []):
        doc_ids += [i for i in _crawl(call, f"'{folder}' in parents and {DOC_MIME}") if i not in doc_ids]

    auto = not config.get("docs") and not config.get("folders")
    if auto:
        q = f"'me' in owners and {DOC_MIME}"
        if lookback_days and lookback_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            q += f" and modifiedTime > '{cutoff}'"
        window = "all time" if not (lookback_days and lookback_days > 0) else f"last {lookback_days}d"
        found = _crawl(call, q)
        log(f"gdocs: auto-indexing docs you own ({window}) — {len(found)} in window")
        doc_ids += [i for i in found if i not in doc_ids]
        # Retain docs already indexed so aging out of the window doesn't evict them; re-stat below
        # confirms they still exist and are accessible (trash/access-loss is what removes them).
        doc_ids += [i for i in store.doc_ids("gdocs") if i not in doc_ids]

    changed, seen = [], []
    for doc_id in doc_ids:
        try:
            meta = call(f"/files/{doc_id}?fields={DOC_FIELDS}")
        except RuntimeError as err:
            log(f"gdocs: {doc_id} skipped ({err})")
            continue
        if meta.get("trashed"):
            continue
        seen.append(doc_id)
        existing = store.get("gdocs", doc_id)
        if not full and existing and existing.revision_id and existing.revision_id == meta.get("headRevisionId"):
            continue
        body = _export(call, doc_id)
        if body is None:
            log(f"gdocs: {meta.get('name', doc_id)} skipped (export failed)")
            continue
        if store.upsert("gdocs", doc_id, title=meta.get("name", doc_id),
                        url=meta.get("webViewLink"), revision_id=meta.get("headRevisionId"),
                        body=body, meta=_meta(meta)):
            changed.append(doc_id)
            log(f"gdocs: updated \"{meta.get('name', doc_id)}\"")

    # Docs that fell out of the configured set (untracked, trashed, access lost) leave the index.
    removed = [d for d in store.doc_ids("gdocs") if d not in seen]
    for doc_id in removed:
        store.delete("gdocs", doc_id)
    return {"changed": changed, "removed": removed}


def _meta(meta: dict) -> dict:
    """Drive file metadata → the store's source-native metadata fields."""
    who = meta.get("lastModifyingUser") or (meta.get("owners") or [{}])[0]
    return {
        "created_at": meta.get("createdTime"),
        "modified_at": meta.get("modifiedTime"),
        "author": who.get("displayName") or who.get("emailAddress"),
        "mime": meta.get("mimeType"),
    }


def _export(call, doc_id: str) -> str | None:
    for mime in ("text/markdown", "text/plain"):
        res = call(f"/files/{doc_id}/export?mimeType={mime.replace('/', '%2F')}", raw=True)
        if res.ok:
            return res.text
    return None
