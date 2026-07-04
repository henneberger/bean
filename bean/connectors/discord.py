"""Discord source. Auth is a bot token (`Authorization: Bot <token>`); the bot must be a member
of the guilds/channels you track. Each message becomes its own document (`<channel_name>/<id>`),
so `recent` returns actual recent messages, newest first. Each channel is paginated backwards by
message-id snowflake down to a floor: the initial backfill (`lookback_days`, default 14) on the
first sync, then the per-channel cursor after that; `--rebuild` reaches back `since_days`."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://discord.com/api/v10"
DAY = 86400
# Text channel kinds we index: GUILD_TEXT(0), GUILD_ANNOUNCEMENT(5), threads(10,11,12).
TEXT_TYPES = {0, 5, 10, 11, 12}
_LEGACY_WEEK = re.compile(r"/\d{4}-W\d{2}$")  # old per-ISO-week digest ids, pruned on sight


# -- auth ---------------------------------------------------------------------------------------
def connect(*, token=None, fetch=None, log=print, **_) -> dict:
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


def _who(m: dict) -> str:
    author = m.get("author") or {}
    return author.get("global_name") or author.get("username") or "unknown"


def _text(m: dict) -> str:
    text = str(m.get("content") or "")
    for att in m.get("attachments") or []:
        text += f"\n[attachment: {att.get('filename', '')}]"
    return text.strip()


def _render_message(m: dict) -> str:
    return f"**@{_who(m)}** ({_stamp(_msg_ts(m))}): {_text(m)}".rstrip()


def _subject(m: dict) -> str:
    return " ".join(_text(m).split())[:80] or "(no text)"


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

    # Migration / cleanup: drop any lingering per-ISO-week digest documents from the old model.
    # Delete the row here; run_sync clears the matching Lance vectors from the `removed` list.
    removed = [d for d in store.doc_ids("discord") if _LEGACY_WEEK.search(d)]
    for doc_id in removed:
        store.delete("discord", doc_id)
    changed = []
    for cid, name in channels.items():
        cursor = store.get_state(f"discord.cursor.{cid}", 0)
        # Lookback is the initial backfill (first sync only); after that continue from the cursor.
        # `--rebuild` ignores the cursor and reaches back since_days.
        if full:
            floor = now - since_days * DAY
        elif cursor:
            floor = cursor
        else:
            floor = now - lookback * DAY
        messages = _fetch_messages(cid, headers, fetch, floor, log)
        if not messages:
            continue
        # One document per message, so `recent` surfaces individual recent messages.
        for m in messages:
            rendered = _render_message(m)
            if not rendered.strip():
                continue
            ts = _msg_ts(m)
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            doc_id = f"{name}/{m['id']}"
            if store.upsert("discord", doc_id, title=f"#{name}: {_subject(m)}",
                            url=f"https://discord.com/channels/@me/{cid}/{m['id']}",
                            revision_id=None, body=rendered,
                            meta={"author": _who(m), "created_at": iso, "modified_at": iso}):
                changed.append(doc_id)
                log(f"discord: updated #{name} {m['id']}")
        store.set_state(f"discord.cursor.{cid}", max([cursor] + [_msg_ts(m) for m in messages]))
    return {"changed": changed, "removed": removed}


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
