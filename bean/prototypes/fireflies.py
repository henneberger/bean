"""Fireflies source. Auth is an API key (Bearer) against the GraphQL endpoint. One doc per
MEETING is indexed whenever the source is connected: the speaker-attributed transcript plus the
meeting summary/overview. Transcripts are paged with a `skip`/`limit` GraphQL cursor and filtered
to a `fromDate` window. Change detection is each transcript's `date` (meeting start, epoch ms) as
the revision id; a single bad transcript is logged and skipped. An optional `include` list narrows
to specific organizer emails. This source re-observes the collection each run and does not prune."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..http import api_json_post
from ..store import Store
from ..workspace import load_credential, save_credential

API = "https://api.fireflies.ai/graphql"
PAGE = 50  # Fireflies caps transcript pages at 50

_QUERY = """
query Transcripts($fromDate: DateTime, $limit: Int!, $skip: Int!) {
  transcripts(fromDate: $fromDate, limit: $limit, skip: $skip) {
    id title date duration organizer_email participants transcript_url
    summary { overview }
    sentences { speaker_name text }
  }
}
"""


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    """`fireflies:organizer:<email>` restricts to meetings by an organizer. Otherwise not ours."""
    s = item.strip()
    if s.lower().startswith("fireflies:organizer:"):
        return ("include", s.split(":", 2)[2].lower())
    return None


def connect(*, token=None, key=None, fetch=None, log=print, **_) -> dict:
    token = token or key
    if not token:
        raise RuntimeError(
            "pass --token <api-key> (Fireflies → Settings → Integrations → "
            "Fireflies API → copy your API key).")
    who = api_json_post(API, _headers(token), {"query": "{ users { name } }"}, fetch=fetch)
    if who.get("errors"):
        raise RuntimeError(f"Fireflies rejected the key: {who['errors']}")
    save_credential("fireflies", {"token": token})
    log("✓ Fireflies connected.")
    return who


def connected() -> dict | None:
    return load_credential("fireflies")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict | None = None, fetch=None,
         full: bool = False, since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential("fireflies")
    if not cred:
        raise RuntimeError("not connected — run `bean auth fireflies --token …`.")
    headers = _headers(cred["token"])
    organizers = {str(o).lower() for o in (config.get("include") or [])}  # optional filter

    from_date = None
    if not full:
        from_date = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")

    changed: list[str] = []
    skip = 0
    while True:
        variables = {"limit": PAGE, "skip": skip}
        if from_date:
            variables["fromDate"] = from_date
        resp = api_json_post(API, headers, {"query": _QUERY, "variables": variables}, fetch=fetch)
        if resp.get("errors"):
            log(f"fireflies: query error ({resp['errors']})")
            break
        rows = ((resp.get("data") or {}).get("transcripts")) or []
        for tr in rows:
            try:
                if organizers and str(tr.get("organizer_email") or "").lower() not in organizers:
                    continue
                tid = tr.get("id")
                doc_id = f"transcript/{tid}"
                rev = str(tr.get("date") or "")
                existing = store.get("fireflies", doc_id)
                if not full and existing and existing.revision_id == rev:
                    continue
                if store.upsert("fireflies", doc_id, title=tr.get("title") or f"Meeting {tid}",
                                url=tr.get("transcript_url"), revision_id=rev, body=_body(tr),
                                meta={"created_at": _iso(tr.get("date")), "modified_at": _iso(tr.get("date")),
                                      "author": tr.get("organizer_email")}):
                    changed.append(doc_id)
                    log(f"fireflies: updated {doc_id}")
            except Exception as err:  # one bad transcript must never abort the sync
                log(f"fireflies: transcript {tr.get('id')} skipped ({err})")
        if len(rows) < PAGE:
            break
        skip += PAGE
    return {"changed": changed, "removed": []}


def _body(tr: dict) -> str:
    lines = [f"# {tr.get('title') or 'Meeting'}"]
    overview = ((tr.get("summary") or {}).get("overview") or "").strip()
    if overview:
        lines += ["", "## Summary", overview]
    lines += ["", "## Transcript"]
    # Coalesce consecutive sentences from the same speaker into one attributed monologue.
    speaker = None
    buf: list[str] = []
    for s in tr.get("sentences") or []:
        who = s.get("speaker_name") or "Unknown Speaker"
        text = (s.get("text") or "").replace("\xa0", " ")
        if who != speaker:
            if speaker is not None and buf:
                lines.append(f"**{speaker}**: {' '.join(buf).strip()}")
            speaker, buf = who, []
        buf.append(text)
    if speaker is not None and buf:
        lines.append(f"**{speaker}**: {' '.join(buf).strip()}")
    return "\n".join(lines)


def _iso(epoch_ms) -> str | None:
    if not epoch_ms:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
