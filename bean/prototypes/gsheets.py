"""Google Sheets source. Reuses the `google` gcloud credential (see gdocs.py) — no separate
auth. Tracks spreadsheets by id/URL; each tab becomes its own document rendered as a Markdown
table (first row = header). Change detection is the spreadsheet's Drive `modifiedTime` used as
the revision id for every tab, so an unchanged spreadsheet re-exports nothing. doc_id is
`spreadsheetId/tabTitle`; tabs or spreadsheets that go away are pruned."""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from .. import gdocs
from ..http import AuthError, api_get, api_json
from ..store import Store

SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3"


# -- refs ---------------------------------------------------------------------------------------
def parse_add(item: str):
    """`gsheet:ID` or a docs.google.com/spreadsheets/d/ID/... URL → the 'sheets' list."""
    item = item.strip()
    m = re.match(r"gsheet:([\w-]+)$", item, re.I)
    if m:
        return ("sheets", m.group(1))
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", item)
    if m:
        return ("sheets", m.group(1))
    return None


def connect(log=print):
    """Google Sheets rides the shared `google` credential — connect via `bean auth google`."""
    return gdocs.connect(log=log)


def connected() -> dict | None:
    return gdocs.connected()


# -- rendering ----------------------------------------------------------------------------------
def _cell(v) -> str:
    return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")


def _markdown_table(rows: list[list]) -> str:
    if not rows:
        return "_(empty)_"
    width = max(len(r) for r in rows)
    header = rows[0] + [""] * (width - len(rows[0]))
    out = ["| " + " | ".join(_cell(c) for c in header) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        r = r + [""] * (width - len(r))
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(out)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, token_fn=gdocs.access_token,
         fetch=None, full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    token = token_fn()

    def call(url: str, raw: bool = False):
        nonlocal token
        try:
            return (api_get if raw else api_json)(url, {"Authorization": f"Bearer {token}"},
                                                  fetch=fetch)
        except AuthError:
            token = token_fn(True)  # expired mid-sync — refresh once
            return (api_get if raw else api_json)(url, {"Authorization": f"Bearer {token}"},
                                                  fetch=fetch)

    sheet_ids = list(dict.fromkeys(config.get("sheets", [])))
    changed, seen = [], []
    for sid in sheet_ids:
        try:
            drive = call(f"{DRIVE}/files/{sid}?fields=modifiedTime,name")
        except RuntimeError as err:
            log(f"gsheets: {sid} skipped ({err})")
            continue
        modified = drive.get("modifiedTime")

        fields = "properties.title,sheets.properties(title,sheetId,gridProperties)"
        try:
            meta = call(f"{SHEETS}/{sid}?fields={quote(fields)}")
        except RuntimeError as err:
            log(f"gsheets: {sid} skipped ({err})")
            continue
        book_title = (meta.get("properties") or {}).get("title") or sid

        for sh in meta.get("sheets", []):
            props = sh.get("properties", {})
            tab = props.get("title") or "Sheet1"
            gid = props.get("sheetId")
            doc_id = f"{sid}/{tab}"
            seen.append(doc_id)
            existing = store.get("gsheets", doc_id)
            if not full and modified and existing and existing.revision_id == modified:
                continue  # spreadsheet untouched since last sync — skip the values export
            rng = quote(tab, safe="")
            values = call(f"{SHEETS}/{sid}/values/{rng}?{urlencode({'majorDimension': 'ROWS'})}")
            body = f"# {book_title} — {tab}\n\n" + _markdown_table(values.get("values", []))
            url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
            if gid is not None:
                url += f"#gid={gid}"
            if store.upsert("gsheets", doc_id, title=f"{book_title} — {tab}", url=url,
                            revision_id=modified, body=body,
                            meta={"modified_at": modified}):
                changed.append(doc_id)
                log(f"gsheets: updated \"{book_title} — {tab}\"")

    removed = [d for d in store.doc_ids("gsheets") if d not in seen]
    for doc_id in removed:
        store.delete("gsheets", doc_id)
    return {"changed": changed, "removed": removed}
