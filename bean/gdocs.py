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

from .http import AuthError, api_get, api_json
from .store import Store
from .workspace import load_credential, save_credential

API = "https://www.googleapis.com/drive/v3"
DOC_FIELDS = "id,name,modifiedTime,headRevisionId,webViewLink,trashed"


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
def sync(store: Store, config: dict, *, token_fn=access_token, fetch=None,
         full: bool = False, log=lambda m: None) -> dict:
    token = token_fn()

    def call(path: str, raw: bool = False):
        nonlocal token
        url = f"{API}{path}"
        try:
            return (api_get if raw else api_json)(url, {"Authorization": f"Bearer {token}"}, fetch=fetch)
        except AuthError:
            token = token_fn(True)  # expired mid-sync — refresh once
            return (api_get if raw else api_json)(url, {"Authorization": f"Bearer {token}"}, fetch=fetch)

    # Folders expand to their Google Docs (one level; subfolders are added explicitly).
    doc_ids = list(dict.fromkeys(config.get("docs", [])))
    for folder in config.get("folders", []):
        page = None
        while True:
            from urllib.parse import urlencode
            q = urlencode({
                "q": f"'{folder}' in parents and mimeType='application/vnd.google-apps.document' and trashed=false",
                "fields": "nextPageToken,files(id)", "pageSize": "100",
                **({"pageToken": page} if page else {}),
            })
            resp = call(f"/files?{q}")
            doc_ids += [f["id"] for f in resp.get("files", []) if f["id"] not in doc_ids]
            page = resp.get("nextPageToken")
            if not page:
                break

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
                        body=body):
            changed.append(doc_id)
            log(f"gdocs: updated \"{meta.get('name', doc_id)}\"")

    # Docs that fell out of the configured set (untracked, trashed, access lost) leave the index.
    removed = [d for d in store.doc_ids("gdocs") if d not in seen]
    for doc_id in removed:
        store.delete("gdocs", doc_id)
    return {"changed": changed, "removed": removed}


def _export(call, doc_id: str) -> str | None:
    for mime in ("text/markdown", "text/plain"):
        res = call(f"/files/{doc_id}/export?mimeType={mime.replace('/', '%2F')}", raw=True)
        if res.ok:
            return res.text
    return None
