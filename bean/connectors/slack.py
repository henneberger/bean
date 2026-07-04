"""Slack source. Each thread (a root message and its replies) becomes one document; each
standalone message becomes its own document — so `recent` returns actual recent messages/threads,
newest first, not a coarse time bucket. `lookback_days` (default 14, 0 = all) is the initial
backfill: the first sync of a channel reaches back that far. Later syncs re-scan a trailing
`REFRESH_DAYS` window (to catch edits and late replies) plus anything newer than the cursor; edits
older than that window are missed by design. `--rebuild` re-fetches everything within since_days."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://slack.com/api"
DAY = 86400
REFRESH_DAYS = 7  # trailing window re-scanned each incremental sync so recent edits/replies land
_LEGACY_WEEK = re.compile(r"/\d{4}-W\d{2}$")  # old per-ISO-week digest ids, pruned on sight


# -- auth -----------------------------------------------------------------------------------------
def connect(*, token=None, fetch=None, log=print, **_) -> dict:
    if not token:
        raise RuntimeError("pass --token xoxp-… (a Slack user token; see the connect-slack skill).")
    if not token.startswith(("xoxp-", "xoxb-")):
        raise RuntimeError("that does not look like a Slack token (expected xoxp-… or xoxb-…).")
    who = api_json(f"{API}/auth.test", {"Authorization": f"Bearer {token}"}, fetch=fetch)
    if not who.get("ok"):
        raise RuntimeError(f"Slack rejected the token: {who.get('error')}. Check it and try again.")
    save_credential("slack", {"token": token, "team": who.get("team"), "user": who.get("user"),
                              "url": who.get("url")})
    log(f"✓ Slack connected as {who.get('user')} in {who.get('team')}.")
    return who


def connected() -> dict | None:
    return load_credential("slack")


# -- rendering ------------------------------------------------------------------------------------
def _stamp(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _clean_text(text: str, users: dict) -> str:
    text = str(text or "")
    text = re.sub(r"<@(U[A-Z0-9]+)>", lambda g: f"@{users.get(g.group(1), g.group(1))}", text)
    text = re.sub(r"<(https?:[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?:[^>]+)>", r"\1", text)
    return text


def _who(m: dict, users: dict) -> str:
    return users.get(m.get("user"), m.get("username") or m.get("user") or "unknown")


def _render_message(m: dict, users: dict) -> str:
    body = _clean_text(m.get("text"), users).replace("\n", "\n  ")
    return f"**@{_who(m, users)}** ({_stamp(m['ts'])}): {body}"


def _subject(m: dict, users: dict) -> str:
    """A short, human title for a message/thread — first line, mentions and links resolved."""
    return " ".join(_clean_text(m.get("text"), users).split())[:80] or "(no text)"


def _permalink(team_url: str | None, channel_id: str, ts: str) -> str | None:
    if not team_url:
        return None
    return f"{team_url.rstrip('/')}/archives/{channel_id}/p{ts.replace('.', '')}"


def render_thread(messages: list[dict], users: dict) -> str:
    """A root message and its replies (or a single message), oldest first, as one block."""
    return "\n".join(_render_message(m, users)
                     for m in sorted(messages, key=lambda m: float(m["ts"])))


# -- sync -----------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, token: str, team_url: str | None = None, fetch=None,
         full: bool = False, since_days: int = 90, now: float | None = None,
         log=lambda m: None) -> dict:
    now = now or time.time()
    headers = {"Authorization": f"Bearer {token}"}

    def get(method: str, **params) -> dict:
        resp = api_json(f"{API}/{method}?{urlencode(params)}", headers, fetch=fetch)
        if not resp.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {resp.get('error')}")
        return resp

    def paged(method: str, key: str, **params) -> list[dict]:
        out, cursor = [], None
        while True:
            resp = get(method, **params, limit=params.pop("limit", 200),
                       **({"cursor": cursor} if cursor else {}))
            out += resp.get(key, [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return out

    def list_channels(types: str) -> list[dict]:
        return paged("conversations.list", "channels", types=types, exclude_archived="true")

    # Private channels need the `groups:read` scope; degrade to public-only if the token lacks it
    # rather than failing the whole sync.
    try:
        channels = list_channels("public_channel,private_channel")
    except RuntimeError as err:
        if "missing_scope" not in str(err):
            raise
        log("slack: token lacks groups:read — indexing public channels only")
        channels = list_channels("public_channel")
    by_name = {c["name"]: c for c in channels}
    # Default: index every channel the account is a member of — no per-channel adds. An explicit
    # channel list narrows to just those; "*" also means all.
    raw = [str(c).lstrip("#") for c in config.get("channels", [])]
    if config.get("all") or not raw or "*" in raw:
        wanted = [c["name"] for c in by_name.values() if c.get("is_member")]
    else:
        wanted = [w for w in raw if w != "*"]

    # Migration / cleanup: drop any lingering per-ISO-week digest documents from the old model.
    # Delete the row here; run_sync clears the matching Lance vectors from the `removed` list.
    removed = [d for d in store.doc_ids("slack") if _LEGACY_WEEK.search(d)]
    for doc_id in removed:
        store.delete("slack", doc_id)
    if not wanted:
        return {"changed": [], "removed": removed}

    users: dict = store.get_state("slack.users", {})

    def ensure_users(ids):
        if not any(i and i not in users for i in ids):
            return
        for u in paged("users.list", "members"):
            profile = u.get("profile") or {}
            users[u["id"]] = profile.get("display_name") or profile.get("real_name") or u.get("name")
        store.set_state("slack.users", users)

    lookback = int(config.get("lookback_days", 14))
    changed = []
    for name in wanted:
        ch = by_name.get(name)
        if not ch:
            log(f"slack: #{name} not found (is the account a member?)")
            continue
        cursor = store.get_state(f"slack.cursor.{ch['id']}", 0)
        # `--rebuild` reaches back since_days. The first sync backfills `lookback` days (0 = all).
        # Later syncs re-scan a trailing REFRESH_DAYS window (so recent edits/replies re-render)
        # together with anything newer than the cursor.
        if full:
            floor = now - since_days * DAY
        elif not cursor:
            floor = 0 if lookback == 0 else now - lookback * DAY
        else:
            floor = min(cursor, now - REFRESH_DAYS * DAY)
        oldest = f"{floor:.6f}"

        messages = [m for m in paged("conversations.history", "messages", channel=ch["id"], oldest=oldest)
                    if not m.get("subtype") or m["subtype"] == "thread_broadcast"]
        replies: dict = {}
        for m in messages:
            if m.get("thread_ts") == m["ts"] and m.get("reply_count"):
                replies[m["ts"]] = [r for r in paged("conversations.replies", "messages",
                                                     channel=ch["id"], ts=m["ts"], oldest="0")
                                    if r["ts"] != m["ts"] and (not r.get("subtype") or r["subtype"] == "thread_broadcast")]
        ensure_users({m.get("user") for m in messages} | {r.get("user") for rs in replies.values() for r in rs})

        # One document per thread (root + replies) and per standalone message. Roots are the
        # top-level messages; thread_broadcast copies live under their root, never on their own.
        roots = [m for m in messages if not m.get("thread_ts") or m["thread_ts"] == m["ts"]]
        for root in roots:
            thread = [root, *replies.get(root["ts"], [])]
            last_ts = max(float(x["ts"]) for x in thread)
            doc_id = f"{name}/{root['ts']}"
            if store.upsert("slack", doc_id, title=f"#{name}: {_subject(root, users)}",
                            url=_permalink(team_url, ch["id"], root["ts"]), revision_id=None,
                            body=render_thread(thread, users),
                            meta={"author": _who(root, users), "created_at": _iso(root["ts"]),
                                  "modified_at": _iso(f"{last_ts:.6f}")}):
                changed.append(doc_id)
                log(f"slack: updated #{name} {root['ts']}")
        if messages:
            latest = max(float(x["ts"]) for m in messages
                         for x in (m, *replies.get(m["ts"], [])))
            store.set_state(f"slack.cursor.{ch['id']}", latest)
    return {"changed": changed, "removed": removed}
