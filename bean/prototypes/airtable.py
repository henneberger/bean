"""Airtable source. Tracks tables (`baseId/tableIdOrName`) and indexes each record as a
document. Auth is a personal access token (create one at https://airtable.com/create/tokens
with data.records:read + schema.bases:read) sent as a Bearer token. Change detection uses a
table's "Last Modified Time" field as the revision id when one exists; otherwise the content
hash is the sole authority. doc_id is `baseId/tableId/recordId`; records that stop being
returned under a tracked table are pruned."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from ..http import api_get, api_json
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://api.airtable.com/v0"


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`airtable:BASE/TABLE` or an airtable.com/BASE/TABLE/... URL → the 'tables' list."""
    item = item.strip()
    m = re.match(r"airtable:([\w-]+)/([^/\s]+)$", item, re.I)
    if m:
        return ("tables", f"{m.group(1)}/{m.group(2)}")
    m = re.search(r"airtable\.com/(app[\w-]+)/(tbl[\w-]+)", item)
    if m:
        return ("tables", f"{m.group(1)}/{m.group(2)}")
    return None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def connect(token: str, *, fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError(
            "pass --token … (create a personal access token at "
            "https://airtable.com/create/tokens with data.records:read + schema.bases:read).")
    headers = _headers(token)
    who = api_get(f"{API}/meta/whoami", headers, fetch=fetch)
    if who.status == 404:
        # Older/limited tokens lack /meta/whoami — just confirm the token can list bases.
        api_json(f"{API}/meta/bases", headers, fetch=fetch)
        info = {}
    elif not who.ok:
        raise RuntimeError(f"HTTP {who.status}: {who.text[:200]}")
    else:
        info = who.json()
    who_id = info.get("id")
    save_credential("airtable", {"token": token, "id": who_id})
    log(f"✓ Airtable connected{f' ({who_id})' if who_id else ''}.")
    return info


def connected() -> dict | None:
    return load_credential("airtable")


# -- schema (best-effort) -----------------------------------------------------------------------
def _table_meta(base_id: str, headers: dict, fetch) -> dict:
    """Map tableId/name → its schema (primary field, field types). Tolerate 403 (token lacks
    schema.bases:read) by returning {} so we fall back to ids and heuristics."""
    res = api_get(f"{API}/meta/bases/{base_id}/tables", headers, fetch=fetch)
    if not res.ok:
        return {}
    out: dict = {}
    for t in res.json().get("tables", []):
        entry = {
            "name": t.get("name"),
            "primary": None,
            "fields": {f["id"]: f for f in t.get("fields", [])},
        }
        pid = t.get("primaryFieldId")
        for f in t.get("fields", []):
            if f.get("id") == pid:
                entry["primary"] = f.get("name")
        out[t.get("id")] = entry
        if t.get("name"):
            out[t["name"]] = entry
    return out


def _last_modified_field(schema: dict | None) -> str | None:
    if not schema:
        return None
    for f in (schema.get("fields") or {}).values():
        if f.get("type") in ("lastModifiedTime", "lastModifiedBy"):
            return f.get("name")
    return None


# -- rendering ----------------------------------------------------------------------------------
def _render_value(v):
    """Flatten a field value to a string; arrays join with ', '; attachments keep filenames."""
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, dict) and "filename" in item:  # attachment
                parts.append(item["filename"])
            elif isinstance(item, dict):
                parts.append(item.get("name") or item.get("id") or str(item))
            else:
                parts.append(str(item))
        return ", ".join(parts)
    if isinstance(v, dict):  # single collaborator / button / etc.
        return v.get("name") or v.get("email") or v.get("url") or str(v)
    return str(v)


def _body(record: dict, primary: str | None) -> str:
    fields = record.get("fields", {})
    heading = None
    if primary and primary in fields:
        heading = _render_value(fields[primary])
    heading = heading or record.get("id")
    lines = [f"# {heading}"]
    for name, value in fields.items():
        lines.append(f"{name}: {_render_value(value)}")
    return "\n".join(lines)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("airtable")
    if not cred:
        raise RuntimeError("not connected — run `bean auth airtable --token pat…`.")
    headers = _headers(cred["token"])
    tables = list(dict.fromkeys(config.get("tables", [])))

    changed, seen = [], []
    for ref in tables:
        base_id, _, table = ref.partition("/")
        if not base_id or not table:
            log(f"airtable: bad table ref {ref!r} (want baseId/tableIdOrName)")
            continue
        schema_by_table = _table_meta(base_id, headers, fetch)
        schema = schema_by_table.get(table)
        primary = (schema or {}).get("primary")
        lmf = _last_modified_field(schema)
        prefix = f"{base_id}/{table}/"

        offset = None
        while True:
            params = {"pageSize": "100", **({"offset": offset} if offset else {})}
            data = api_json(f"{API}/{base_id}/{table}?{urlencode(params)}", headers, fetch=fetch)
            for record in data.get("records", []):
                rid = record["id"]
                doc_id = f"{base_id}/{table}/{rid}"
                seen.append(doc_id)
                fields = record.get("fields", {})
                revision = fields.get(lmf) if lmf else None
                existing = store.get("airtable", doc_id)
                if not full and revision and existing and existing.revision_id == revision:
                    continue
                title = (_render_value(fields[primary]) if primary and primary in fields
                         else rid)
                if store.upsert(
                        "airtable", doc_id, title=title,
                        url=f"https://airtable.com/{base_id}/{table}/{rid}",
                        revision_id=revision, body=_body(record, primary),
                        meta={"created_at": record.get("createdTime")}):
                    changed.append(doc_id)
                    log(f"airtable: updated {doc_id}")
            offset = data.get("offset")
            if not offset:
                break

    # Prune records that vanished, scoped to the tracked base/table prefixes.
    tracked_prefixes = tuple(f"{r.partition('/')[0]}/{r.partition('/')[2]}/" for r in tables
                             if "/" in r)
    removed = [d for d in store.doc_ids("airtable")
               if d.startswith(tracked_prefixes) and d not in seen]
    for doc_id in removed:
        store.delete("airtable", doc_id)
    return {"changed": changed, "removed": removed}
