"""Dropbox source. Tracks folders and indexes the documents inside them (Markdown, plain text,
office docs, PDF). Auth is an access token generated in the Dropbox App Console
(https://www.dropbox.com/developers/apps → your app → Settings → "Generated access token") —
individual-friendly, no full OAuth dance. The Dropbox API is POST-based. Change detection is the
per-file `rev` (or content_hash) as the revision id, so unchanged files re-embed nothing.

Text files (.md/.markdown/.txt) stream through the normal HTTP fetch seam and decode from
Response.text. Binary formats (office/pdf) need real bytes, which our str-typed Response can't
carry losslessly, so they go through a small injectable `download_bytes` (a binary GET/POST,
defaulting to a requests-based one) — tests pass a fake to stay offline. doc_id is the Dropbox
file id; files that leave a tracked folder are pruned."""

from __future__ import annotations

import json

from ..http import api_json_post, api_post
from ..office import OFFICE_EXT
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"

TEXT_EXT = {".md", ".markdown", ".txt", ".text"}
PDF_EXT = {".pdf"}


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`dropbox:/Folder/Path` → the 'folders' list."""
    item = item.strip()
    if item.lower().startswith("dropbox:"):
        path = item[len("dropbox:"):]
        if not path.startswith("/"):
            path = "/" + path
        return ("folders", path.rstrip("/") or "")
    return None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def connect(token: str, *, fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError(
            "pass --token … (generate one in the Dropbox App Console at "
            "https://www.dropbox.com/developers/apps → your app → Settings → "
            "'Generated access token').")
    # get_current_account takes a literal JSON `null` body.
    who = api_json_post(f"{API}/users/get_current_account", _headers(token), "null", fetch=fetch)
    name = (who.get("name") or {}).get("display_name") or who.get("email")
    save_credential("dropbox", {"token": token, "name": name})
    log(f"✓ Dropbox connected{f' as {name}' if name else ''}.")
    return who


def connected() -> dict | None:
    return load_credential("dropbox")


# -- listing ------------------------------------------------------------------------------------
def _list_folder(token: str, folder: str, fetch) -> list[dict]:
    """All file entries under `folder`, recursively, paging via list_folder/continue."""
    headers = _headers(token)
    resp = api_json_post(f"{API}/files/list_folder", headers,
                         {"path": folder, "recursive": True}, fetch=fetch)
    entries = list(resp.get("entries", []))
    while resp.get("has_more"):
        resp = api_json_post(f"{API}/files/list_folder/continue", headers,
                             {"cursor": resp["cursor"]}, fetch=fetch)
        entries += resp.get("entries", [])
    return [e for e in entries if e.get(".tag") == "file"]


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i >= 0 else ""


# -- body extraction ----------------------------------------------------------------------------
def _download_text(token: str, path_lower: str, fetch) -> str:
    """Download a file and return its body as str (for text formats)."""
    headers = {"Authorization": f"Bearer {token}",
               "Dropbox-API-Arg": json.dumps({"path": path_lower})}
    res = api_post(f"{CONTENT}/files/download", headers, b"", fetch=fetch)
    if not res.ok:
        raise RuntimeError(f"download HTTP {res.status}")
    return res.text


def _default_download_bytes(token: str, path_lower: str) -> bytes:
    """Binary download for office/pdf. Uses requests directly because the shared Response is
    str-typed and cannot round-trip arbitrary bytes; injectable so tests never hit the network."""
    import requests
    headers = {"Authorization": f"Bearer {token}",
               "Dropbox-API-Arg": json.dumps({"path": path_lower})}
    r = requests.post(f"{CONTENT}/files/download", headers=headers, data=b"", timeout=60)
    r.raise_for_status()
    return r.content


def _body(token: str, entry: dict, fetch, download_bytes, log) -> str | None:
    name = entry.get("name", "")
    path_lower = entry.get("path_lower")
    ext = _ext(name)
    if ext in TEXT_EXT:
        try:
            return _download_text(token, path_lower, fetch)
        except RuntimeError as err:
            log(f"dropbox: {name} skipped ({err})")
            return None
    if ext in OFFICE_EXT or ext in PDF_EXT:
        import tempfile
        from pathlib import Path
        try:
            data = download_bytes(path_lower)
        except Exception as err:
            log(f"dropbox: {name} skipped ({err})")
            return None
        with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            p = Path(tmp.name)
            try:
                if ext in PDF_EXT:
                    from ..pdf import extract_pdf
                    return extract_pdf(p, {}, log=log)
                from ..office import extract_office
                return extract_office(p)
            except Exception as err:
                log(f"dropbox: {name} skipped ({err})")
                return None
    return None


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None, download_bytes=None) -> dict:
    cred = load_credential("dropbox")
    if not cred:
        raise RuntimeError("not connected — run `bean auth dropbox --token sl.…`.")
    token = cred["token"]
    download_bytes = download_bytes or (lambda pl: _default_download_bytes(token, pl))
    folders = list(dict.fromkeys(config.get("folders", [])))

    changed, seen = [], []
    for folder in folders:
        try:
            entries = _list_folder(token, folder, fetch)
        except RuntimeError as err:
            log(f"dropbox: folder {folder!r} skipped ({err})")
            continue
        for entry in entries:
            if _ext(entry.get("name", "")) not in (TEXT_EXT | OFFICE_EXT | PDF_EXT):
                continue
            doc_id = entry.get("id") or entry.get("path_lower")
            seen.append(doc_id)
            revision = entry.get("rev") or entry.get("content_hash")
            existing = store.get("dropbox", doc_id)
            if not full and revision and existing and existing.revision_id == revision:
                continue
            body = _body(token, entry, fetch, download_bytes, log)
            if body is None:
                continue
            if store.upsert("dropbox", doc_id, title=entry.get("name"), url=None,
                            revision_id=revision, body=body,
                            meta={"modified_at": entry.get("server_modified")}):
                changed.append(doc_id)
                log(f"dropbox: updated {entry.get('name')}")

    removed = [d for d in store.doc_ids("dropbox") if d not in seen]
    for doc_id in removed:
        store.delete("dropbox", doc_id)
    return {"changed": changed, "removed": removed}
