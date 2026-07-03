"""Slack source. The stream is cut into per-channel per-ISO-week digest documents (threads
as their own sections) so units stay stable as history grows. `lookback_days` (default 14) is
the initial backfill: the first sync of a channel reaches back that far. After that each sync
continues from the last message it saw (a per-channel cursor in the workspace state table),
re-rendering the in-progress week so same-week edits land; edits to older weeks are missed by
design (`--rebuild` re-fetches everything within since_days)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://slack.com/api"
DAY = 86400


# -- auth -----------------------------------------------------------------------------------------
def connect(token: str, *, fetch=None, log=print) -> dict:
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


# -- week math + deterministic rendering -----------------------------------------------------------
def iso_week(ts_seconds: float) -> str:
    d = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(ts_seconds: float) -> float:
    d = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
    midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return midnight.timestamp() - (d.isoweekday() - 1) * DAY


def _stamp(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _render_message(m: dict, users: dict) -> str:
    import re
    who = users.get(m.get("user"), m.get("username") or m.get("user") or "unknown")
    text = str(m.get("text") or "")
    text = re.sub(r"<@(U[A-Z0-9]+)>", lambda g: f"@{users.get(g.group(1), g.group(1))}", text)
    text = re.sub(r"<(https?:[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?:[^>]+)>", r"\1", text)
    return f"**@{who}** ({_stamp(m['ts'])}): {text.replace(chr(10), chr(10) + '  ')}"


def render_week(channel: str, week: str, messages: list[dict], replies: dict, users: dict) -> str:
    roots = sorted((m for m in messages if not m.get("thread_ts") or m["thread_ts"] == m["ts"]),
                   key=lambda m: float(m["ts"]))
    lines = [f"# #{channel} — week {week}", ""]
    threads = [m for m in roots if m.get("reply_count")]
    singles = [m for m in roots if not m.get("reply_count")]
    for t in threads:
        subject = " ".join(str(t.get("text") or "").split())[:80]
        lines += [f"## thread {t['ts']} — {subject}", "", _render_message(t, users)]
        for r in sorted(replies.get(t["ts"], []), key=lambda m: float(m["ts"])):
            lines.append(_render_message(r, users))
        lines.append("")
    if singles:
        lines += ["## messages", ""]
        lines += [_render_message(m, users) for m in singles]
        lines.append("")
    return "\n".join(lines)


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
    # channel list (via `bean add #name`) narrows to just those; "*" also means all.
    raw = [str(c).lstrip("#") for c in config.get("channels", [])]
    if config.get("all") or not raw or "*" in raw:
        wanted = [c["name"] for c in by_name.values() if c.get("is_member")]
    else:
        wanted = [w for w in raw if w != "*"]
    if not wanted:
        return {"changed": [], "removed": []}

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
        # Lookback is the initial backfill only: the first sync reaches back `lookback` days. After
        # that we continue from the last message we saw (cursor), snapped to its ISO-week start so
        # the in-progress week always re-renders from complete data (catching same-week edits). A
        # `--rebuild` sync ignores the cursor and reaches back `since_days`.
        if full:
            floor = now - since_days * DAY
        elif cursor:
            floor = cursor
        else:
            floor = now - lookback * DAY
        oldest = f"{week_start(floor):.6f}"

        messages = [m for m in paged("conversations.history", "messages", channel=ch["id"], oldest=oldest)
                    if not m.get("subtype") or m["subtype"] == "thread_broadcast"]
        replies: dict = {}
        for m in messages:
            if m.get("thread_ts") == m["ts"] and m.get("reply_count"):
                replies[m["ts"]] = [r for r in paged("conversations.replies", "messages",
                                                     channel=ch["id"], ts=m["ts"], oldest="0")
                                    if r["ts"] != m["ts"] and (not r.get("subtype") or r["subtype"] == "thread_broadcast")]
        ensure_users({m.get("user") for m in messages} | {r.get("user") for rs in replies.values() for r in rs})

        by_week: dict[str, list] = {}
        for m in messages:
            by_week.setdefault(iso_week(float(m["ts"])), []).append(m)
        for week, week_msgs in by_week.items():
            body = render_week(name, week, week_msgs, replies, users)
            url = f"{team_url.rstrip('/')}/archives/{ch['id']}" if team_url else None
            if store.upsert("slack", f"{name}/{week}", title=f"#{name} — {week}", url=url,
                            revision_id=None, body=body):
                changed.append(f"{name}/{week}")
                log(f"slack: updated #{name} {week}")
        latest = max([cursor] + [float(m["ts"]) for m in messages])
        store.set_state(f"slack.cursor.{ch['id']}", latest)
    return {"changed": changed, "removed": []}  # weekly digests never prune
