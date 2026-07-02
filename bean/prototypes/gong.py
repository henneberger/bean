"""Gong source. Auth is HTTP Basic with a base64-encoded `access_key:access_key_secret`, stored
per user. One doc per CALL is indexed whenever the source is connected: the speaker-attributed
transcript plus the call title/time metadata. Calls are listed through `GET /v2/calls` (paged via
`records.cursor`); transcripts are fetched in a single `POST /v2/calls/transcript` per page and
stitched to their calls. Change detection is each call's `created` timestamp as the revision id; a
single bad call is logged and skipped. An optional `include` list narrows to specific Gong
workspace ids. This source re-observes the whole collection each run and does not prune."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from ..http import api_json, api_json_post
from ..store import Store
from ..workspace import load_credential, save_credential

BASE = "https://api.gong.io"


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`gong:workspace:<id>` restricts calls to a Gong workspace. Otherwise not ours."""
    s = item.strip()
    if s.lower().startswith("gong:workspace:"):
        return ("include", s.split(":", 2)[2])
    return None


def connect(*, key=None, secret=None, token=None, url=None, fetch=None, log=print, **_) -> dict:
    base = (url or BASE).rstrip("/")
    if not (key and secret):
        raise RuntimeError(
            "pass --key <access-key> --secret <access-key-secret> "
            "(Gong → Company Settings → Ecosystem → API → create an access key).")
    headers = _headers(key, secret)
    who = api_json(f"{base}/v2/users?limit=1", headers, fetch=fetch)  # cheap authenticated probe
    save_credential("gong", {"key": key, "secret": secret, "base": base})
    log(f"✓ Gong connected ({base}).")
    return who


def connected() -> dict | None:
    return load_credential("gong")


def _headers(key: str, secret: str) -> dict:
    raw = f"{key}:{secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Content-Type": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("gong")
    if not cred:
        raise RuntimeError("not connected — run `bean auth gong --key … --secret …`.")
    base = cred.get("base", BASE)
    headers = _headers(cred["key"], cred["secret"])
    workspaces = [str(w) for w in (config.get("include") or [])] or [None]  # None = all workspaces

    frm = None
    if not full:
        frm = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    changed: list[str] = []
    for ws in workspaces:
        changed += _sync_workspace(store, base, headers, fetch, full, frm, ws, log)
    return {"changed": changed, "removed": []}


def _sync_workspace(store, base, headers, fetch, full, frm, workspace_id, log) -> list[str]:
    changed: list[str] = []
    cursor = None
    while True:
        q = f"{base}/v2/calls?"
        parts = []
        if frm:
            parts.append(f"fromDateTime={frm}")
        if workspace_id:
            parts.append(f"workspaceId={workspace_id}")
        if cursor:
            parts.append(f"cursor={cursor}")
        resp = api_json(q + "&".join(parts), headers, fetch=fetch)
        calls = {c["id"]: c for c in resp.get("calls", []) if c.get("id")}
        if calls:
            transcripts = _fetch_transcripts(base, headers, fetch, list(calls), frm, workspace_id, log)
            for t in transcripts:
                try:
                    cid = t.get("callId")
                    call = calls.get(cid)
                    if not call:
                        continue
                    doc_id = f"call/{cid}"
                    rev = str(call.get("created") or call.get("started") or "")
                    existing = store.get("gong", doc_id)
                    if not full and existing and existing.revision_id == rev:
                        continue
                    body = _call_body(call, t)
                    if store.upsert("gong", doc_id, title=call.get("title") or f"Call {cid}",
                                    url=call.get("url"), revision_id=rev, body=body,
                                    meta={"created_at": call.get("started"), "modified_at": rev}):
                        changed.append(doc_id)
                        log(f"gong: updated {doc_id}")
                except Exception as err:  # one bad call must never abort the sync
                    log(f"gong: call {t.get('callId')} skipped ({err})")
        cursor = (resp.get("records") or {}).get("cursor")
        if not cursor:
            break
    return changed


def _fetch_transcripts(base, headers, fetch, call_ids, frm, workspace_id, log) -> list:
    flt: dict = {"callIds": call_ids}
    if frm:
        flt["fromDateTime"] = frm
    if workspace_id:
        flt["workspaceId"] = workspace_id
    try:
        resp = api_json_post(f"{base}/v2/calls/transcript", headers, {"filter": flt}, fetch=fetch)
    except Exception as err:
        log(f"gong: transcript page skipped ({err})")
        return []
    return resp.get("callTranscripts", [])


def _call_body(call: dict, transcript: dict) -> str:
    lines = [f"# {call.get('title') or 'Call'}"]
    if call.get("started"):
        lines.append(f"date: {call['started']}")
    lines.append("")
    # Attribute each monologue to a stable per-call speaker label (Gong basic call metadata does
    # not carry party names; the transcript only exposes speakerIds).
    speaker_names: dict[str, str] = {}
    for seg in transcript.get("transcript", []):
        sid = seg.get("speakerId") or ""
        if sid not in speaker_names:
            speaker_names[sid] = f"Speaker {len(speaker_names) + 1}"
        text = " ".join(s.get("text", "") for s in seg.get("sentences", []))
        if text.strip():
            lines += [f"**{speaker_names[sid]}**: {text.strip()}", ""]
    return "\n".join(lines)
