"""Discord source. Auth is a bot token (`Authorization: Bot <token>`); the bot must be a member
of the guilds/channels you track. Like Slack, the message stream is cut into per-channel per-ISO-week
digest documents (`<channel_name>/<YYYY-Www>`) so units stay stable as history grows. Each channel
is paginated backwards by message-id snowflake down to a floor: the initial backfill (`lookback_days`,
default 14) on the first sync, then the per-channel cursor after that (snapped to the ISO-week start
so digests re-render from complete weeks); `--full` reaches back `since_days`. Reuses slack's
ISO-week math. Weekly digests never prune."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from . import slack  # reuse iso_week / week_start so chat sources bucket weeks identically
from .http import api_json
from .store import Store
from .workspace import load_credential, save_credential

API = "https://discord.com/api/v10"
DAY = 86400
# Text channel kinds we index: GUILD_TEXT(0), GUILD_ANNOUNCEMENT(5), threads(10,11,12).
TEXT_TYPES = {0, 5, 10, 11, 12}
_URL_RE = re.compile(r"discord\.com/channels/(\d+)/(\d+)")


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    m = _URL_RE.search(s)
    if m:
        return ("channels", m.group(2))
    if s.lower().startswith("discord:guild:"):
        return ("guilds", s.split(":", 2)[2])
    if s.lower().startswith("discord:"):
        val = s.split(":", 1)[1]
        return ("channels", val) if val.isdigit() else None
    return None


def connect(*, token=None, fetch=None, log=print) -> dict:
    if not token:
        raise RuntimeError("pass --token <bot-token> (Discord Developer Portal → your app → Bot → "
                           "Reset Token; enable the Message Content intent and invite the bot).")
    who = api_json(f"{API}/users/@me", _headers(token), fetch=fetch)
    name = who.get("username") or who.get("id")
    save_credential("discord", {"token": token, "name": name, "id": who.get("id")})
    log(f"✓ Discord connected as {name}.")
    return who


def connected() -> dict | None:
    return load_credential("discord")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}"}


# -- rendering ----------------------------------------------------------------------------------
def _snowflake_ts(mid: str) -> float:
    return ((int(mid) >> 22) + 1420070400000) / 1000.0


def _msg_ts(m: dict) -> float:
    ts = m.get("timestamp")
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return _snowflake_ts(m["id"])


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _render_message(m: dict) -> str:
    author = m.get("author") or {}
    who = author.get("global_name") or author.get("username") or "unknown"
    text = str(m.get("content") or "")
    for att in m.get("attachments") or []:
        text += f"\n[attachment: {att.get('filename', '')}]"
    return f"**@{who}** ({_stamp(_msg_ts(m))}): {text}".rstrip()


def render_week(channel: str, week: str, messages: list[dict]) -> str:
    lines = [f"# #{channel} — week {week}", ""]
    for m in sorted(messages, key=_msg_ts):
        rendered = _render_message(m)
        if rendered.strip():
            lines.append(rendered)
    lines.append("")
    return "\n".join(lines)


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, now: float | None = None,
         log=lambda m: None) -> dict:
    cred = load_credential("discord")
    if not cred:
        raise RuntimeError("not connected — run `bean auth discord --token …`.")
    headers = _headers(cred["token"])
    now = now or time.time()
    lookback = int(config.get("lookback_days", 14))

    # Resolve the tracked channel set: explicit channels plus every text channel in tracked guilds.
    channels: dict[str, str] = {}  # channel_id -> name
    for cid in config.get("channels", []):
        cid = str(cid)
        try:
            ch = api_json(f"{API}/channels/{cid}", headers, fetch=fetch)
            channels[cid] = ch.get("name") or cid
        except RuntimeError as err:
            log(f"discord: channel {cid} skipped ({err})")
    for gid in config.get("guilds", []):
        try:
            for ch in api_json(f"{API}/guilds/{gid}/channels", headers, fetch=fetch):
                if ch.get("type") in TEXT_TYPES:
                    channels[str(ch["id"])] = ch.get("name") or str(ch["id"])
        except RuntimeError as err:
            log(f"discord: guild {gid} skipped ({err})")

    changed = []
    for cid, name in channels.items():
        cursor = store.get_state(f"discord.cursor.{cid}", 0)
        # Lookback is the initial backfill (first sync only); after that continue from the cursor.
        # `--full` ignores the cursor and reaches back since_days. (Shares Slack's window semantics.)
        if full:
            floor = now - since_days * DAY
        elif cursor:
            floor = cursor
        else:
            floor = now - lookback * DAY
        floor = slack.week_start(floor)
        messages = _fetch_messages(cid, headers, fetch, floor, log)
        if not messages:
            continue
        by_week: dict[str, list] = {}
        for m in messages:
            by_week.setdefault(slack.iso_week(_msg_ts(m)), []).append(m)
        for week, week_msgs in by_week.items():
            body = render_week(name, week, week_msgs)
            if store.upsert("discord", f"{name}/{week}", title=f"#{name} — {week}",
                            url=f"https://discord.com/channels/@me/{cid}", revision_id=None, body=body):
                changed.append(f"{name}/{week}")
                log(f"discord: updated #{name} {week}")
        store.set_state(f"discord.cursor.{cid}", max([cursor] + [_msg_ts(m) for m in messages]))
    return {"changed": changed, "removed": []}  # weekly digests never prune


def _fetch_messages(cid, headers, fetch, floor, log) -> list[dict]:
    out: list[dict] = []
    before = None
    while True:
        url = f"{API}/channels/{cid}/messages?limit=100" + (f"&before={before}" if before else "")
        try:
            batch = api_json(url, headers, fetch=fetch)
        except RuntimeError as err:
            log(f"discord: channel {cid} history stopped ({err})")
            break
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        oldest = min(batch, key=_msg_ts)
        if _msg_ts(oldest) <= floor or len(batch) < 100:
            break
        before = oldest["id"]  # paginate backwards by snowflake
    return [m for m in out if _msg_ts(m) >= floor]
