#!/usr/bin/env python3
"""Offline tests: fake HTTP fetch, deterministic bag-of-words embedder, real DuckDB + Lance
in a temp BEAN_HOME. Covers the store (hash-gated upserts, revisions, cursors), both sources
(change detection, export fallback, week digests, edits in lookback), the retry policy, the
end-to-end sync→search flow, and workspace/credential hygiene."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["BEAN_HOME"] = tempfile.mkdtemp(prefix="bean-home-")

from bean.http import AuthError, Response, api_get  # noqa: E402
from bean.store import Store, content_hash  # noqa: E402
from bean.chunks import chunk_text  # noqa: E402
from bean.workspace import Workspace, save_credential, load_credential, bean_home  # noqa: E402
from bean import gdocs, slack  # noqa: E402
from bean.index import search as lance_search  # noqa: E402
from bean.sync import run_sync  # noqa: E402

CHECKS = FAILED = 0


def ok(cond, msg):
    global CHECKS, FAILED
    CHECKS += 1
    if not cond:
        FAILED += 1
        print(f"  ✗ {msg}")


def res(status, body, headers=None):
    text = body if isinstance(body, str) else json.dumps(body)
    return Response(status=status, text=text, headers=headers or {})


def fake_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in re.findall(r"[a-z]{3,}", t.lower()):
            h = 0
            for ch in w:
                h = (h * 31 + ord(ch)) & 0xFFFFFFFF
            v[h % 64] += 1
        norm = sum(x * x for x in v) ** 0.5 or 1
        out.append([x / norm for x in v])
    return out


def repo(name):
    d = Path(tempfile.mkdtemp(prefix=f"bean-{name}-"))
    (d / ".git").mkdir()
    return d


# -- http retry policy -----------------------------------------------------------------------
calls = {"n": 0}
slept = []


def flaky(url, headers):
    calls["n"] += 1
    return res(429, "slow", {"Retry-After": "2"}) if calls["n"] < 3 else res(200, "fine")


r = api_get("https://x.test/a", fetch=flaky, sleep=slept.append)
ok(r.status == 200 and calls["n"] == 3, "429s retried until success")
ok(all(s == 2.0 for s in slept), "Retry-After honored")
try:
    api_get("https://x.test/a", fetch=lambda u, h: res(401, "no"))
    ok(False, "401 should raise")
except AuthError:
    ok(True, "401 raises AuthError")
bad = api_get("https://x.test/a", fetch=lambda u, h: res(500, "boom"), sleep=lambda s: None, retries=1)
ok(bad.status == 500, "exhausted retries return the failing response")

# -- workspace + credentials -------------------------------------------------------------------
ws1, ws2 = Workspace(repo("alpha")), Workspace(repo("beta"))
ok(ws1.dir != ws2.dir and ws1.dir.parent == ws2.dir.parent == bean_home(), "one workspace folder per repo under ~/.bean")
ok(Workspace(ws1.repo).dir == ws1.dir, "workspace is stable for the same repo")
path = save_credential("slack", {"token": "xoxp-1"})
ok(stat.S_IMODE(path.stat().st_mode) == 0o600, "credentials are mode 0600")
ok(load_credential("slack")["token"] == "xoxp-1", "credential round-trips")

# -- store: hash-gated upserts ------------------------------------------------------------------
with Store(ws1) as store:
    ok(store.upsert("gdocs", "d1", title="Vision", url="https://d/1", revision_id="r1", body="We charge cards."),
       "first upsert reports change")
    ok(not store.upsert("gdocs", "d1", title="Vision", url="https://d/1", revision_id="r2", body="We charge cards."),
       "same body (comment-only revision) reports no change")
    ok(store.get("gdocs", "d1").revision_id == "r2", "metadata still updated")
    ok(store.upsert("gdocs", "d1", title="Vision v2", url="https://d/1", revision_id="r3", body="We charge twice."),
       "content change reports change")
    ok(len(store.revisions("gdocs", "d1")) == 2, "revision ledger records each content change")
    store.set_state("k", {"a": 1})
    ok(store.get_state("k") == {"a": 1}, "state round-trips as json")

# -- chunking -----------------------------------------------------------------------------------
chunks = chunk_text("\n".join(f"line {i} with some sensible amount of text" for i in range(100)), "gdocs/d1")
ok(len(chunks) > 1 and chunks[0].id == "gdocs/d1#L1", "chunking yields stable ids")
ok(all(len(c.text) <= 2000 for c in chunks), "chunks capped")

# -- gdocs sync ---------------------------------------------------------------------------------
ok(gdocs.parse_ref("https://docs.google.com/document/d/aBc-123_x/edit") == ("doc", "aBc-123_x"), "doc url parses")
ok(gdocs.parse_ref("https://drive.google.com/drive/u/0/folders/F0LDER123") == ("folder", "F0LDER123"), "folder url parses")
ok(gdocs.parse_ref("nope") is None, "garbage rejected")

DOCS = {
    "docA": {"name": "Payments Guide", "rev": "r1", "body": "# Payments\n\nBilling goes through chargeCard and emits a receipt.", "md": True},
    "docB": {"name": "Legacy Notes", "rev": "r9", "body": "Plain only.", "md": False},
}
exports = {"n": 0}


def gfetch(url, headers):
    from urllib.parse import urlparse, parse_qs
    u = urlparse(url)
    if u.path == "/drive/v3/files":
        return res(200, {"files": [{"id": "docB"}]})
    m = re.match(r"^/drive/v3/files/([^/]+)(/export)?$", u.path)
    d = DOCS[m.group(1)]
    if not m.group(2):
        return res(200, {"id": m.group(1), "name": d["name"], "headRevisionId": d["rev"],
                         "webViewLink": f"https://docs.google.com/document/d/{m.group(1)}", "trashed": False})
    exports["n"] += 1
    mime = parse_qs(u.query)["mimeType"][0]
    if mime == "text/markdown" and not d["md"]:
        return res(400, "no markdown")
    return res(200, d["body"])


gws = Workspace(repo("gdocs"))
cfg = {"docs": ["docA"], "folders": ["folder1"]}
with Store(gws) as store:
    s1 = gdocs.sync(store, cfg, token_fn=lambda force=False: "tok", fetch=gfetch)
    ok(len(s1["changed"]) == 2, f"first sync ingests both docs ({s1['changed']})")
    ok(store.get("gdocs", "docB").body == "Plain only.", "markdown failure falls back to text/plain")
    s2 = gdocs.sync(store, cfg, token_fn=lambda force=False: "tok", fetch=gfetch)
    ok(s2["changed"] == [] and s2["removed"] == [], "same revisions are a no-op")
    DOCS["docA"]["rev"] = "r2"  # comment-only: new revision, same body
    before = exports["n"]
    s3 = gdocs.sync(store, cfg, token_fn=lambda force=False: "tok", fetch=gfetch)
    ok(s3["changed"] == [] and exports["n"] == before + 1, "comment-only revision exports once, changes nothing")
    DOCS["docA"]["rev"] = "r3"
    DOCS["docA"]["body"] += "\n\nRetries happen twice."
    s4 = gdocs.sync(store, cfg, token_fn=lambda force=False: "tok", fetch=gfetch)
    ok(s4["changed"] == ["docA"], "real change re-ingests")
    s5 = gdocs.sync(store, {"docs": ["docA"], "folders": []}, token_fn=lambda force=False: "tok", fetch=gfetch)
    ok(s5["removed"] == ["docB"] and store.get("gdocs", "docB") is None, "unlisted doc pruned")

    # mid-sync 401: token_fn(force=True) asked once, sync continues
    state = {"failed": False, "forces": 0}

    def gfetch401(url, headers):
        if not state["failed"]:
            state["failed"] = True
            return res(401, "expired")
        return gfetch(url, headers)

    def token_fn(force=False):
        if force:
            state["forces"] += 1
        return "tok2"

    gdocs.sync(store, {"docs": ["docA"], "folders": []}, token_fn=token_fn, fetch=gfetch401)
    ok(state["forces"] == 1, "mid-sync 401 refreshes the token once")

# -- slack sync ---------------------------------------------------------------------------------
NOW = time.mktime(time.strptime("2026-07-02 12:00", "%Y-%m-%d %H:%M"))
ok(slack.iso_week(time.mktime(time.strptime("2026-06-29", "%Y-%m-%d"))) == "2026-W27", "2026-06-29 is W27")


def ts(days_ago, frac=".000100"):
    return f"{int(NOW - days_ago * 86400)}{frac}"


HISTORY = [
    {"ts": ts(1), "user": "U1", "text": "Retries land <@U2>, see <https://x.test|the doc>", "thread_ts": ts(1), "reply_count": 1},
    {"ts": ts(2), "user": "U2", "text": "Deploy done"},
]
REPLIES = [{"ts": ts(1, ".000200"), "user": "U2", "text": "Confirmed", "thread_ts": ts(1)}]


def sfetch(url, headers):
    from urllib.parse import urlparse, parse_qs
    u = urlparse(url)
    q = parse_qs(u.query)
    method = u.path.split("/")[-1]
    if method == "conversations.list":
        return res(200, {"ok": True, "channels": [{"id": "C1", "name": "eng-payments"}]})
    if method == "conversations.history":
        oldest = float(q["oldest"][0])
        return res(200, {"ok": True, "messages": [m for m in HISTORY if float(m["ts"]) >= oldest]})
    if method == "conversations.replies":
        return res(200, {"ok": True, "messages": [HISTORY[0]] + REPLIES})
    if method == "users.list":
        return res(200, {"ok": True, "members": [
            {"id": "U1", "name": "ada", "profile": {"display_name": "ada"}},
            {"id": "U2", "name": "bob", "profile": {}}]})
    return res(404, {"ok": False, "error": "unknown"})


sws = Workspace(repo("slack"))
scfg = {"channels": ["#eng-payments"], "lookback_days": 14}
with Store(sws) as store:
    s1 = slack.sync(store, scfg, token="xoxp-1", team_url="https://t.slack.com", fetch=sfetch, now=NOW)
    ok(s1["changed"] == ["eng-payments/2026-W27"], f"first sync writes the week digest ({s1['changed']})")
    body = store.get("slack", "eng-payments/2026-W27").body
    ok(f"## thread {ts(1)}" in body, "thread renders as its own section")
    ok("@ada" in body and "Retries land @bob, see the doc (https://x.test)" in body, "mentions and links resolve")
    ok("## messages" in body and "Deploy done" in body, "non-thread messages render")
    s2 = slack.sync(store, scfg, token="xoxp-1", team_url="https://t.slack.com", fetch=sfetch, now=NOW)
    ok(s2["changed"] == [], "unchanged history is a no-op")
    ok(store.get_state("slack.cursor.C1") == float(ts(1)), "cursor advanced to latest ts")
    HISTORY[1]["text"] = "Deploy done (edited: rolled back)"
    s3 = slack.sync(store, scfg, token="xoxp-1", team_url="https://t.slack.com", fetch=sfetch, now=NOW)
    ok(s3["changed"] == ["eng-payments/2026-W27"], "edit inside the lookback window rewrites the week")
    ok("rolled back" in store.get("slack", "eng-payments/2026-W27").body, "edited text landed")

# -- end to end: sync → embed → lance → search --------------------------------------------------
ews = Workspace(repo("e2e"))
ews.save_config({"google": {"docs": ["docA"], "folders": []}, "slack": {"channels": []}})
gdocs._token_cache.update(token="tok", exp=time.time() + 3600)  # bypass gcloud in tests
save_credential("google", {"method": "gcloud"})
result = run_sync(ews, embed_fn=fake_embed, fetch=gfetch)
ok(result["errors"] == [] and len(result["changed"]) == 1 and result["chunks"] >= 1,
   f"run_sync ingests and embeds ({result})")
hits = lance_search(ews, fake_embed(["billing chargeCard receipt payments"])[0], k=3)
ok(hits and hits[0]["title"] == "Payments Guide" and hits[0]["url"].startswith("https://docs.google.com/"),
   f"search returns the doc with title + url ({hits[:1]})")
ok(hits[0]["text"].startswith("# Payments"), "search result carries the chunk text")
again = run_sync(ews, embed_fn=fake_embed, fetch=gfetch)
ok(again["changed"] == [] and again["chunks"] == 0, "re-sync with no upstream change embeds nothing")

print(f"bean: {CHECKS - FAILED}/{CHECKS} checks passed" if FAILED == 0 else f"bean: {FAILED}/{CHECKS} checks FAILED")
sys.exit(0 if FAILED == 0 else 1)
