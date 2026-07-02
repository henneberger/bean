"""Egnyte source. Tracks folders and indexes the documents inside them (Markdown, plain text,
office docs, PDF). Auth is a Bearer access token plus the account subdomain (`company` for
company.egnyte.com), both stored in the credential so sync can rebuild the base url. The folder
listing (`/pubapi/v1/fs/{path}?list_content=true`) is walked recursively; file bytes come from
`/pubapi/v1/fs-content/{path}`. Change detection is the per-file `last_modified` (or `checksum`)
as the revision id, so unchanged files re-embed nothing.

Text files (.md/.txt) stream through the normal HTTP fetch seam and decode from Response.text.
Binary formats (office/pdf) need real bytes, which the str-typed Response can't carry losslessly,
so they go through a small injectable `download_bytes` — tests pass a fake to stay offline. doc_id
is the file path; files that leave a tracked folder are pruned."""

from __future__ import annotations

from urllib.parse import quote

from ..http import api_get, api_json
from ..office import OFFICE_EXT
from ..store import Store
from ..workspace import load_credential, save_credential

TEXT_EXT = {".md", ".markdown", ".txt", ".text"}
PDF_EXT = {".pdf"}
SUPPORTED = TEXT_EXT | OFFICE_EXT | PDF_EXT


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`egnyte:/Shared/Path` → the 'folders' list."""
    s = item.strip()
    if s.lower().startswith("egnyte:"):
        path = s[len("egnyte:"):].strip()
        if not path.startswith("/"):
            path = "/" + path
        return ("folders", path.rstrip("/") or "/")
    return None


def _base(domain: str) -> str:
    return f"https://{domain}.egnyte.com"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def connect(*, subdomain=None, token=None, url=None, fetch=None, log=print, **_) -> dict:
    domain = subdomain or url
    if not (domain and token):
        raise RuntimeError(
            "pass --subdomain <company> --token <access-token> (Egnyte → Settings → API access; "
            "'company' means company.egnyte.com).")
    domain = domain.replace(".egnyte.com", "").strip("/ ")
    who = api_json(f"{_base(domain)}/pubapi/v1/userinfo", _headers(token), fetch=fetch)
    name = who.get("username") or who.get("email") or domain
    cred = {"domain": domain, "token": token, "name": name}
    save_credential("egnyte", cred)
    log(f"✓ Egnyte connected as {name} ({domain}.egnyte.com).")
    return who


def connected() -> dict | None:
    return load_credential("egnyte")


# -- listing ------------------------------------------------------------------------------------
def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i >= 0 else ""


def _list_folder(base: str, headers: dict, path: str, fetch, log) -> list[dict]:
    """All file entries under `path`, recursively (Egnyte lists one folder per call)."""
    url = f"{base}/pubapi/v1/fs/{quote(path.lstrip('/'))}?list_content=true"
    data = api_json(url, headers, fetch=fetch)
    files = list(data.get("files", []))
    for folder in data.get("folders", []):
        sub = folder.get("path")
        if not sub:
            continue
        try:
            files += _list_folder(base, headers, sub, fetch, log)
        except RuntimeError as err:
            log(f"egnyte: subfolder {sub!r} skipped ({err})")
    return files


# -- body extraction ----------------------------------------------------------------------------
def _default_download_bytes(base: str, token: str, path: str) -> bytes:
    """Binary download for office/pdf. Uses requests directly because the shared Response is
    str-typed and cannot round-trip arbitrary bytes; injectable so tests never hit the network."""
    import requests
    url = f"{base}/pubapi/v1/fs-content/{quote(path.lstrip('/'))}"
    r = requests.get(url, headers=_headers(token), timeout=60)
    r.raise_for_status()
    return r.content


def _body(base: str, token: str, entry: dict, fetch, download_bytes, log) -> str | None:
    name = entry.get("name", "")
    path = entry.get("path") or name
    ext = _ext(name)
    if ext in TEXT_EXT:
        res = api_get(f"{base}/pubapi/v1/fs-content/{quote(path.lstrip('/'))}", _headers(token),
                      fetch=fetch)
        if not res.ok:
            log(f"egnyte: {name} skipped (HTTP {res.status})")
            return None
        return res.text
    if ext in OFFICE_EXT or ext in PDF_EXT:
        import tempfile
        from pathlib import Path
        try:
            data = download_bytes(path)
        except Exception as err:
            log(f"egnyte: {name} skipped ({err})")
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
                log(f"egnyte: {name} skipped ({err})")
                return None
    return None


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None,
         download_bytes=None) -> dict:
    cred = load_credential("egnyte")
    if not cred:
        raise RuntimeError("not connected — run `bean auth egnyte --subdomain … --token …`.")
    domain, token = cred["domain"], cred["token"]
    base = _base(domain)
    download_bytes = download_bytes or (lambda path: _default_download_bytes(base, token, path))
    folders = list(dict.fromkeys(config.get("folders", []))) or ["/Shared"]

    changed, seen, tracked_roots = [], [], []
    for folder in folders:
        try:
            entries = _list_folder(base, _headers(token), folder, fetch, log)
        except RuntimeError as err:
            log(f"egnyte: folder {folder!r} skipped ({err})")
            continue
        tracked_roots.append(folder.rstrip("/"))  # only prune under folders we actually listed
        for entry in entries:
            if entry.get("is_folder") or _ext(entry.get("name", "")) not in SUPPORTED:
                continue
            doc_id = entry.get("path") or entry.get("entry_id")
            if not doc_id:
                continue
            seen.append(doc_id)
            revision = entry.get("last_modified") or entry.get("checksum") or entry.get("entry_id")
            existing = store.get("egnyte", doc_id)
            if not full and revision and existing and existing.revision_id == revision:
                continue
            body = _body(base, token, entry, fetch, download_bytes, log)
            if body is None:
                continue
            url = (f"{base}/navigate/file/{entry['group_id']}" if entry.get("group_id")
                   else f"{base}/app/index.do#storage/files/1{quote(doc_id)}")
            if store.upsert("egnyte", doc_id, title=entry.get("name"), url=url,
                            revision_id=revision, body=body,
                            meta={"modified_at": entry.get("last_modified"), "file_path": doc_id}):
                changed.append(doc_id)
                log(f"egnyte: updated {entry.get('name')}")

    roots = tuple(tracked_roots)
    removed = [d for d in store.doc_ids("egnyte")
               if any(d == r or d.startswith(r + "/") for r in roots) and d not in seen]
    for doc_id in removed:
        store.delete("egnyte", doc_id)
    return {"changed": changed, "removed": removed}
