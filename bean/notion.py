"""Notion source. Tracks pages by id/URL and indexes each page's text, walking nested block
children into a Markdown-ish body. Auth is an internal-integration token (per user); share the
pages with that integration in Notion. Change detection is the page `last_edited_time` as the
revision id, so unchanged pages re-embed nothing.

Scope note: this uses only Notion's GET endpoints (page + block children), keeping the same
injectable (url, headers) fetch contract as every other source. Database *querying* is POST-only
and lives on the connector backlog; add the individual pages for now."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from .http import api_json
from .store import Store
from .workspace import load_credential, save_credential

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
ID_RE = re.compile(r"([0-9a-f]{32})", re.I)


# -- refs + auth --------------------------------------------------------------------------------
def _dashify(raw: str) -> str | None:
    m = ID_RE.search(raw.replace("-", ""))
    if not m:
        return None
    h = m.group(1)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def parse_add(item: str):
    if "notion.so" in item or "notion.site" in item:
        pid = _dashify(item)
        return ("pages", pid) if pid else None
    if "://" in item or item.startswith("#"):
        return None
    if re.fullmatch(r"[0-9a-f-]{32,36}", item.strip(), re.I):
        return ("pages", _dashify(item))
    return None


def connect(token: str, *, fetch=None, log=print) -> dict:
    who = api_json(f"{API}/users/me", _headers(token), fetch=fetch)
    save_credential("notion", {"token": token, "bot": (who.get("name") or who.get("id"))})
    log(f"✓ Notion connected ({who.get('name') or who.get('id')}).")
    return who


def connected() -> dict | None:
    return load_credential("notion")


def _headers(token: str, version: str = VERSION) -> dict:
    return {"Authorization": f"Bearer {token}", "Notion-Version": version}


# -- block rendering ----------------------------------------------------------------------------
def _rich(rt: list) -> str:
    return "".join(t.get("plain_text", "") for t in (rt or []))


_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
           "bulleted_list_item": "- ", "numbered_list_item": "1. ", "to_do": "- [ ] ",
           "quote": "> ", "callout": "> "}


def _render_block(b: dict) -> str:
    t = b.get("type", "")
    data = b.get(t, {})
    text = _rich(data.get("rich_text"))
    if t == "code":
        return f"```\n{text}\n```"
    if t == "to_do":
        return ("- [x] " if data.get("checked") else "- [ ] ") + text
    return _PREFIX.get(t, "") + text if text else ""


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("notion")
    if not cred:
        raise RuntimeError("not connected — run `bean auth notion --token secret_…`.")
    headers = _headers(cred["token"], config.get("version", VERSION))

    def children(block_id: str, depth: int = 0) -> list[str]:
        if depth > 4:
            return []
        out, cursor = [], None
        while True:
            q = urlencode({"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
            resp = api_json(f"{API}/blocks/{block_id}/children?{q}", headers, fetch=fetch)
            for b in resp.get("results", []):
                line = _render_block(b)
                if line:
                    out.append(("  " * depth) + line)
                if b.get("has_children"):
                    out += children(b["id"], depth + 1)
            cursor = resp.get("next_cursor")
            if not resp.get("has_more"):
                break
        return out

    pages = list(dict.fromkeys(config.get("pages", [])))
    changed, seen = [], []
    for pid in pages:
        try:
            page = api_json(f"{API}/pages/{pid}", headers, fetch=fetch)
        except RuntimeError as err:
            log(f"notion: {pid} skipped ({err})")
            continue
        seen.append(pid)
        existing = store.get("notion", pid)
        if not full and existing and existing.revision_id == page.get("last_edited_time"):
            continue
        title = _page_title(page) or "Untitled"
        body = f"# {title}\n\n" + "\n".join(children(pid))
        if store.upsert("notion", pid, title=title, url=page.get("url"),
                        revision_id=page.get("last_edited_time"), body=body):
            changed.append(pid)
            log(f"notion: updated \"{title}\"")
    removed = [d for d in store.doc_ids("notion") if d not in seen]
    for pid in removed:
        store.delete("notion", pid)
    return {"changed": changed, "removed": removed}


def _page_title(page: dict) -> str | None:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _rich(prop.get("title"))
    return None
