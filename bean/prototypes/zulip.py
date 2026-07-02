"""Zulip source. Like Slack, the stream is cut into per-stream per-ISO-week digest documents
(topics as their own sections) so units stay stable as history grows. A lookback window (default
14 days) re-fetches recent history each sync, snapped to the ISO-week start so digests re-render
from complete data; `--full` re-fetches everything within since_days. Auth is the bot email plus
its API key over HTTP Basic (Organization settings → Bots, or your own Personal settings → API
key); the realm base url is stored in the credential. Per-stream cursors live in the state table.
A weekly-digest source never prunes."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from .. import slack# reuse iso_week / week_start so week boundaries match across chat sources
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

DAY = 86400


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`zulip:STREAM` → the 'streams' list."""
    s = item.strip()
    if s.lower().startswith("zulip:"):
        stream = s.split(":", 1)[1].strip().lstrip("#")
        return ("streams", stream) if stream else None
    return None


def _base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def _headers(email: str, token: str) -> dict:
    raw = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def connect(*, url=None, email=None, token=None, fetch=None, log=print, **_) -> dict:
    if not (url and email and token):
        raise RuntimeError(
            "pass --url https://your-org.zulipchat.com --email bot@your-org.zulipchat.com "
            "--token <api-key> (Personal settings → API key, or a bot's key).")
    base = _base(url)
    who = api_json(f"{base}/api/v1/users/me", _headers(email, token), fetch=fetch)
    if who.get("result") == "error":
        raise RuntimeError(f"Zulip rejected the credentials: {who.get('msg')}")
    cred = {"url": base, "email": email, "token": token,
            "name": who.get("full_name") or email}
    save_credential("zulip", cred)
    log(f"✓ Zulip connected as {cred['name']} at {base}.")
    return cred


def connected() -> dict | None:
    return load_credential("zulip")


# -- rendering ----------------------------------------------------------------------------------
def _stamp(ts) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def render_week(stream: str, week: str, messages: list[dict]) -> str:
    """One digest per stream per ISO week, topics as sections (Zulip's native unit of thread)."""
    by_topic: dict[str, list] = {}
    for m in messages:
        by_topic.setdefault(str(m.get("subject") or "(no topic)"), []).append(m)
    lines = [f"# {stream} — week {week}", ""]
    for topic in sorted(by_topic):
        lines += [f"## {topic}", ""]
        for m in sorted(by_topic[topic], key=lambda m: float(m.get("timestamp", 0))):
            who = m.get("sender_full_name") or m.get("sender_email") or "unknown"
            content = str(m.get("content") or "").replace("\n", "\n  ")
            lines.append(f"**{who}** ({_stamp(m.get('timestamp', 0))}): {content}")
        lines.append("")
    return "\n".join(lines)


# -- fetch --------------------------------------------------------------------------------------
def _subscribed_streams(base: str, headers: dict, fetch) -> list[str]:
    resp = api_json(f"{base}/api/v1/users/me/subscriptions", headers, fetch=fetch)
    return [s["name"] for s in resp.get("subscriptions", []) if s.get("name")]


def _stream_messages(base: str, headers: dict, stream: str, oldest_ts: float, fetch) -> list[dict]:
    """Walk `/api/v1/messages` backwards (anchor paging) until we pass the lookback floor or hit
    the oldest message. Zulip has no timestamp search, so we always start from newest and go back."""
    narrow = json.dumps([{"operator": "stream", "operand": stream}])
    out, seen, anchor = [], set(), "newest"
    for _ in range(200):  # hard cap so a huge stream can't spin forever
        params = {"anchor": anchor, "num_before": 100, "num_after": 0,
                  "narrow": narrow, "apply_markdown": "false"}
        resp = api_json(f"{base}/api/v1/messages?{urlencode(params)}", headers, fetch=fetch)
        if resp.get("result") == "error":
            raise RuntimeError(f"Zulip messages failed: {resp.get('msg')}")
        msgs = resp.get("messages", [])
        if not msgs:
            break
        for m in msgs:
            if m.get("id") not in seen:
                seen.add(m["id"])
                out.append(m)
        oldest = min(msgs, key=lambda m: m.get("id", 0))
        if resp.get("found_oldest") or float(oldest.get("timestamp", 0)) < oldest_ts:
            break
        if oldest["id"] == anchor:  # no progress — bail rather than loop
            break
        anchor = oldest["id"]  # inclusive; the dup is dropped by `seen`
    return [m for m in out if float(m.get("timestamp", 0)) >= oldest_ts]


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, now: float | None = None,
         log=lambda m: None) -> dict:
    cred = load_credential("zulip")
    if not cred:
        raise RuntimeError("not connected — run `bean auth zulip --url … --email … --token …`.")
    base, headers = cred["url"], _headers(cred["email"], cred["token"])
    now = now or time.time()

    wanted = [str(s).lstrip("#") for s in config.get("streams", []) if str(s).strip()]
    if not wanted or "*" in wanted:
        try:
            wanted = _subscribed_streams(base, headers, fetch)
        except RuntimeError as err:
            log(f"zulip: could not list subscriptions ({err})")
            return {"changed": [], "removed": []}

    lookback = int(config.get("lookback_days", 14))
    changed = []
    for stream in wanted:
        cursor = store.get_state(f"zulip.cursor.{stream}", 0)
        floor = (now - since_days * DAY) if (full or not cursor) \
            else min(cursor, now - lookback * DAY)
        oldest_ts = slack.week_start(floor)
        try:
            messages = _stream_messages(base, headers, stream, oldest_ts, fetch)
        except RuntimeError as err:
            log(f"zulip: stream {stream!r} skipped ({err})")
            continue

        by_week: dict[str, list] = {}
        for m in messages:
            by_week.setdefault(slack.iso_week(float(m.get("timestamp", 0))), []).append(m)
        for week, week_msgs in by_week.items():
            body = render_week(stream, week, week_msgs)
            doc_id = f"{stream}/{week}"
            if store.upsert("zulip", doc_id, title=f"{stream} — {week}",
                            url=f"{base}/#narrow/stream/{stream}", revision_id=None, body=body):
                changed.append(doc_id)
                log(f"zulip: updated {doc_id}")
        latest = max([cursor] + [float(m.get("timestamp", 0)) for m in messages])
        store.set_state(f"zulip.cursor.{stream}", latest)
    return {"changed": changed, "removed": []}  # weekly digests never prune
