"""Intercom source. Auth is an access token (Bearer). Two doc kinds are indexed whenever the
source is connected: CONVERSATIONS (the opening message plus every conversation part) and help-center
ARTICLES. Both bodies arrive as HTML and are flattened with `html_to_text`. Conversations are listed
cheaply (id + `updated_at`) and only the ones whose `updated_at` changed since last sync are
re-fetched in full for their parts. Change detection is `updated_at` as the revision id; an optional
`tags` list narrows conversations to those carrying a matching tag."""

from __future__ import annotations

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

BASE = "https://api.intercom.io"


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`intercom:tag:Refund` restricts conversations to a tag. Otherwise not ours."""
    s = item.strip()
    if s.lower().startswith("intercom:tag:"):
        return ("tags", s.split(":", 2)[2])
    return None


def connect(*, token=None, fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError("pass --token <access-token> (Intercom → Settings → Developers → "
                           "your app → Authentication → access token).")
    who = api_json(f"{BASE}/me", _headers(token), fetch=fetch)
    name = who.get("name") or (who.get("app") or {}).get("name") or who.get("email")
    save_credential("intercom", {"token": token, "name": name})
    log(f"✓ Intercom connected ({name}).")
    return who


def connected() -> dict | None:
    return load_credential("intercom")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("intercom")
    if not cred:
        raise RuntimeError("not connected — run `bean auth intercom --token …`.")
    headers = _headers(cred["token"])
    want_tags = {str(t).lower() for t in (config.get("tags") or [])}  # optional filter

    def paged(path: str, key: str):
        url = f"{BASE}{path}"
        while url:
            resp = api_json(url, headers, fetch=fetch)
            yield from resp.get(key, [])
            nxt = (resp.get("pages") or {}).get("next")
            if isinstance(nxt, dict) and nxt.get("starting_after"):
                sep = "&" if "?" in path else "?"
                url = f"{BASE}{path}{sep}starting_after={nxt['starting_after']}"
            elif isinstance(nxt, str):
                url = nxt
            else:
                url = None

    changed, seen = [], []
    # -- conversations --
    for conv in paged("/conversations?per_page=150", "conversations"):
        cid = conv.get("id")
        if want_tags and not _tag_match(conv, want_tags):
            continue
        doc_id = f"conversation/{cid}"
        seen.append(doc_id)
        rev = str(conv.get("updated_at"))
        existing = store.get("intercom", doc_id)
        if not full and existing and existing.revision_id == rev:
            continue
        full_conv = api_json(f"{BASE}/conversations/{cid}", headers, fetch=fetch)
        body = _conversation_body(full_conv)
        title = _conversation_title(full_conv)
        if store.upsert("intercom", doc_id, title=title,
                        url=f"https://app.intercom.com/a/inbox/_/inbox/conversation/{cid}",
                        revision_id=rev, body=body,
                        meta={"created_at": _iso(conv.get("created_at")), "modified_at": _iso(conv.get("updated_at"))}):
            changed.append(doc_id)
            log(f"intercom: updated {doc_id}")

    # -- articles --
    for art in paged("/articles?per_page=150", "data"):
        doc_id = f"article/{art['id']}"
        seen.append(doc_id)
        rev = str(art.get("updated_at"))
        existing = store.get("intercom", doc_id)
        if not full and existing and existing.revision_id == rev:
            continue
        body = f"# {art.get('title', '')}\n\n" + html_to_text(art.get("body") or "")
        if store.upsert("intercom", doc_id, title=art.get("title", doc_id), url=art.get("url"),
                        revision_id=rev, body=body,
                        meta={"created_at": _iso(art.get("created_at")), "modified_at": _iso(art.get("updated_at"))}):
            changed.append(doc_id)
            log(f"intercom: updated {doc_id}")

    removed = [d for d in store.doc_ids("intercom") if d not in seen]
    for doc_id in removed:
        store.delete("intercom", doc_id)
    return {"changed": changed, "removed": removed}


def _tag_match(conv: dict, want: set) -> bool:
    tags = (conv.get("tags") or {}).get("tags") or []
    names = {str(t.get("name", "")).lower() for t in tags} | {str(t.get("id")) for t in tags}
    return bool(names & want)


def _conversation_title(conv: dict) -> str:
    src = conv.get("source") or {}
    subject = src.get("subject") or ""
    return (subject or (conv.get("title") or f"Conversation {conv.get('id')}")).strip()[:120]


def _conversation_body(conv: dict) -> str:
    lines = [f"# Conversation {conv.get('id')}", ""]
    src = conv.get("source") or {}
    opening = html_to_text(src.get("body") or "")
    if opening.strip():
        lines += [f"**{(src.get('author') or {}).get('name', 'customer')}**: {opening.strip()}", ""]
    parts = (conv.get("conversation_parts") or {}).get("conversation_parts") or []
    for p in parts:
        text = html_to_text(p.get("body") or "")
        if not text.strip():  # notes, assignments, tag events carry no body
            continue
        who = (p.get("author") or {}).get("name") or (p.get("author") or {}).get("type") or "?"
        lines += [f"**{who}**: {text.strip()}", ""]
    return "\n".join(lines)


def _iso(epoch) -> str | None:
    if not epoch:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
