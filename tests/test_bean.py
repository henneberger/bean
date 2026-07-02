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

from bean.workspace import set_bean_home  # noqa: E402
set_bean_home(tempfile.mkdtemp(prefix="bean-home-"))  # config, not an env var

from bean.http import AuthError, Response, api_get  # noqa: E402
from bean.store import Store, content_hash  # noqa: E402
from bean.chunks import chunk_text  # noqa: E402
from bean.workspace import Workspace, save_credential, load_credential, bean_home  # noqa: E402
from bean import gdocs, slack, config as cfgmod, localfiles, notion, github, pdf  # noqa: E402
from bean.index import search as lance_search  # noqa: E402
from bean.sync import run_sync, reembed  # noqa: E402
from bean.search import search as hybrid_search, recent, thread, neighbors  # noqa: E402
from bean.sources import route_add  # noqa: E402

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

    # source-native metadata: stored, round-tripped, and updated on a comment-only revision
    store.upsert("gdocs", "d2", title="Old", url=None, revision_id="v1", body="aged content",
                 meta={"modified_at": "2020-01-01T00:00:00Z", "author": "Ada", "mime": "application/vnd.google-apps.document"})
    store.upsert("gdocs", "d3", title="New", url=None, revision_id="v1", body="fresh content",
                 meta={"modified_at": "2026-06-01T00:00:00Z", "author": "Bob"})
    ok(store.get("gdocs", "d2").author == "Ada", "author metadata round-trips")
    ok(str(store.get("gdocs", "d3").modified_at).startswith("2026-06-01"), "modified_at parsed to a timestamp")
    order = [r["doc_id"] for r in store.recent(source="gdocs")]
    ok(order.index("d3") < order.index("d2"), "recent orders by the doc's own modified_at, newest first")
    store.upsert("gdocs", "d2", title="Old", url=None, revision_id="v2", body="aged content",
                 meta={"modified_at": "2027-01-01T00:00:00Z", "author": "Ada"})
    ok(str(store.get("gdocs", "d2").modified_at).startswith("2027"), "comment-only revision refreshes modified_at")

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
                         "webViewLink": f"https://docs.google.com/document/d/{m.group(1)}", "trashed": False,
                         "modifiedTime": "2026-05-01T00:00:00Z", "mimeType": "application/vnd.google-apps.document",
                         "lastModifyingUser": {"displayName": "Grace Hopper"}})
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
    ok(store.get("gdocs", "docA").author == "Grace Hopper" and store.get("gdocs", "docA").mime.endswith("document"),
       "gdocs sync captures author + mime from Drive metadata")
    ok(str(store.get("gdocs", "docA").modified_at).startswith("2026-05-01"), "gdocs sync captures modifiedTime")
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

# auto-index: empty config crawls the docs you own; the query carries an ownership + window filter
autodocs = {"m1": {"name": "Owned One", "rev": "a1", "body": "alpha", "md": True},
            "m2": {"name": "Owned Two", "rev": "a2", "body": "beta", "md": True}}
seen_q = {}


def afetch(url, headers):
    from urllib.parse import urlparse, parse_qs
    u = urlparse(url)
    if u.path == "/drive/v3/files":
        seen_q["q"] = parse_qs(u.query).get("q", [""])[0]
        return res(200, {"files": [{"id": "m1"}, {"id": "m2"}]})
    m = re.match(r"^/drive/v3/files/([^/]+)(/export)?$", u.path)
    d = autodocs[m.group(1)]
    if not m.group(2):
        return res(200, {"id": m.group(1), "name": d["name"], "headRevisionId": d["rev"],
                         "webViewLink": f"https://docs.google.com/document/d/{m.group(1)}", "trashed": False})
    return res(200, d["body"])


aws = Workspace(repo("gdocs-auto"))
with Store(aws) as store:
    sa = gdocs.sync(store, {}, token_fn=lambda force=False: "tok", fetch=afetch, lookback_days=30)
    ok(sorted(sa["changed"]) == ["m1", "m2"], "empty config auto-indexes owned docs")
    ok("'me' in owners" in seen_q["q"] and "modifiedTime >" in seen_q["q"], "auto query filters by owner + window")
    # a later crawl that no longer returns m2 still retains it (only trash/access-loss evicts)
    def afetch2(url, headers):
        from urllib.parse import urlparse
        if urlparse(url).path == "/drive/v3/files":
            return res(200, {"files": [{"id": "m1"}]})
        return afetch(url, headers)

    sb = gdocs.sync(store, {}, token_fn=lambda force=False: "tok", fetch=afetch2, lookback_days=30)
    ok(sb["removed"] == [] and store.get("gdocs", "m2") is not None, "doc aged out of window is retained")

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
        return res(200, {"ok": True, "channels": [
            {"id": "C1", "name": "eng-payments", "is_member": True},
            {"id": "C2", "name": "random-not-joined", "is_member": False}]})
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

# all-channels mode: no explicit channel list → sync every channel the account is a member of
aws = Workspace(repo("slack-all"))
with Store(aws) as store:
    sa = slack.sync(store, {"channels": []}, token="xoxp-1", team_url="https://t.slack.com", fetch=sfetch, now=NOW)
    ok(sa["changed"] == ["eng-payments/2026-W27"], f"empty channel list syncs all member channels ({sa['changed']})")
    ok(store.get("slack", "random-not-joined/2026-W27") is None, "non-member channels are skipped")

# -- end to end: sync → embed → lance → search --------------------------------------------------
ews = Workspace(repo("e2e"))
ews.save_config({"google": {"docs": ["docA"], "folders": []}, "slack": {"channels": []}})
gdocs._token_cache.update(token="tok", exp=time.time() + 3600)  # bypass gcloud in tests
save_credential("google", {"method": "gcloud"})
(bean_home() / "credentials" / "slack.json").unlink(missing_ok=True)  # isolate: only gdocs active here
result = run_sync(ews, embed_fn=fake_embed, fetch=gfetch)
ok(result["errors"] == [] and len(result["changed"]) == 1 and result["chunks"] >= 1,
   f"run_sync ingests and embeds ({result})")
hits = lance_search(ews, fake_embed(["billing chargeCard receipt payments"])[0], k=3)
ok(hits and hits[0]["title"] == "Payments Guide" and hits[0]["url"].startswith("https://docs.google.com/"),
   f"search returns the doc with title + url ({hits[:1]})")
ok(hits[0]["text"].startswith("# Payments"), "search result carries the chunk text")
again = run_sync(ews, embed_fn=fake_embed, fetch=gfetch)
ok(again["changed"] == [] and again["chunks"] == 0, "re-sync with no upstream change embeds nothing")

# -- config layering + coercion -----------------------------------------------------------------
base = cfgmod.resolve()
ok(base["embedding"]["model"] == "BAAI/bge-small-en-v1.5", "defaults resolve with no config file")
cfgmod.save_global({"embedding": {"model": "custom/model"}, "search": {"hybrid": False}})
merged = cfgmod.resolve()
ok(merged["embedding"]["model"] == "custom/model" and merged["chunking"]["lines"] == 40,
   "global config overrides one leaf, keeps sibling defaults")
g = cfgmod.load_global(); cfgmod.set_in(g, "chunking.lines", "20"); cfgmod.set_in(g, "search.hybrid", "true")
ok(g["chunking"]["lines"] == 20 and g["search"]["hybrid"] is True, "config set coerces to leaf type")
cfgmod.save_global({})  # reset so downstream resolves to defaults

# -- source routing -----------------------------------------------------------------------------
ok(route_add("#eng")[0].key == "slack", "#channel routes to slack")
ok(route_add("https://docs.google.com/document/d/abcdefghij1234567890/edit")[0].key == "gdocs", "gdoc url → gdocs")
ok(route_add("baidu/Unlimited-OCR")[0].key == "github" and route_add("baidu/Unlimited-OCR")[2] == "baidu/Unlimited-OCR", "owner/repo → github")
ok(route_add("https://www.notion.so/Team-Wiki-0123456789abcdef0123456789abcdef")[0].key == "notion", "notion url → notion")
ok(notion.parse_add("0123456789abcdef0123456789abcdef")[1] == "01234567-89ab-cdef-0123-456789abcdef", "notion id dashified")
ok(route_add("/tmp/some/notes")[0].key == "localfiles", "path → localfiles")
ok(route_add("garbage no ref") is None or route_add("garbage no ref")[0].key == "localfiles", "bare words don't crash routing")

# -- local files connector (incl. mtime skip + prune) -------------------------------------------
docs_dir = Path(tempfile.mkdtemp(prefix="bean-docs-"))
(docs_dir / "guide.md").write_text("# Refunds\n\nRefunds go through refundCard and reverse the receipt.\n")
(docs_dir / "skip.log").write_text("not indexed")  # unsupported extension
lws = Workspace(repo("local"))
lset = cfgmod.resolve(lws)
r1 = localfiles.sync(store_stub := Store(lws), {"paths": [str(docs_dir)]}, settings=lset)
ok(len(r1["changed"]) == 1 and store_stub.get("localfiles", str(docs_dir / "guide.md")), "markdown file indexed, .log ignored")
r2 = localfiles.sync(store_stub, {"paths": [str(docs_dir)]}, settings=lset)
ok(r2["changed"] == [], "unchanged file skipped via mtime")
(docs_dir / "guide.md").unlink()
r3 = localfiles.sync(store_stub, {"paths": [str(docs_dir)]}, settings=lset)
ok(r3["removed"] == [str(docs_dir / "guide.md")], "deleted file pruned")
store_stub.close()

# recursive crawl + office docs: a .docx in a nested subfolder is discovered and its text extracted
import zipfile as _zip
from bean import office  # noqa: E402
nested = docs_dir / "sub" / "deeper"
nested.mkdir(parents=True)
_DOCX_XML = (
    '<?xml version="1.0"?><w:document '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    "<w:p><w:r><w:t>Chargebacks</w:t></w:r><w:r><w:t> go through disputeCard.</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p></w:body></w:document>"
)
with _zip.ZipFile(nested / "report.docx", "w") as zf:
    zf.writestr("word/document.xml", _DOCX_XML)
ok(office.extract_office(nested / "report.docx") == "Chargebacks go through disputeCard.\nSecond paragraph.",
   "docx runs join within a paragraph, paragraphs split by newline")
ok(office._rtf(r"{\rtf1\ansi Hello\par world\'21}") == "Hello\nworld!", "rtf control words stripped, escapes decoded")
lws2 = Workspace(repo("local-office"))
r4 = localfiles.sync(store_office := Store(lws2), {"paths": [str(docs_dir)]}, settings=lset)
ok(str(nested / "report.docx") in r4["changed"], "nested .docx discovered by recursive crawl")
ok("disputeCard" in store_office.get("localfiles", str(nested / "report.docx")).body, "docx body indexed")
store_office.close()

# -- pdf backend routing (fakes, no models) -----------------------------------------------------
native = lambda p: ["real embedded text on this page", "   "]  # 2nd page has no embedded text
ocr = lambda p, cfg, only=None: [f"OCR[{i}]" for i in (only if only is not None else [0, 1])]
auto = pdf.extract_pdf("x.pdf", {"backend": "auto"}, native_text=native, ocr_pages=ocr)
ok("real embedded text" in auto and "OCR[1]" in auto and "OCR[0]" not in auto, "auto OCRs only image-only pages")
text_only = pdf.extract_pdf("x.pdf", {"backend": "text"}, native_text=native, ocr_pages=ocr)
ok("OCR" not in text_only, "text backend never OCRs")
forced = pdf.extract_pdf("x.pdf", {"backend": "unlimited-ocr"}, native_text=native, ocr_pages=ocr)
ok(forced == "OCR[0]\n\nOCR[1]", "unlimited-ocr backend OCRs every page")
degrade = pdf.extract_pdf("x.pdf", {"backend": "auto"}, native_text=native,
                          ocr_pages=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no device")))
ok(degrade.strip() == "real embedded text on this page", "auto keeps embedded text when a page's OCR errors")
try:
    pdf.extract_pdf("x.pdf", {"backend": "unlimited-ocr"}, native_text=native,
                    ocr_pages=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ok(False, "explicit unlimited-ocr should surface OCR errors")
except RuntimeError:
    ok(True, "explicit unlimited-ocr surfaces the error instead of silently degrading")

# -- hybrid search: deterministic keyword rescue + fusion ---------------------------------------
hws = Workspace(repo("hybrid"))
with Store(hws) as store:
    store.upsert("gdocs", "d1", title="Payments", url="u1", revision_id=None,
                 body="Billing uses chargeCard to bill customers monthly.")
    store.upsert("gdocs", "d2", title="Refunds", url="u2", revision_id=None,
                 body="An identifier like ZQ-9001 marks a reversed transaction.")
reembed(hws, embed_fn=fake_embed)  # embeds the two manually-stored docs into Lance + chunk mirror
kw = hybrid_search(hws, "ZQ-9001", embed_query_fn=lambda q: fake_embed([q])[0], hybrid=True, k=3)
ok(kw and kw[0]["doc_id"] == "d2", "rare exact identifier retrieved by the keyword half")
vec = hybrid_search(hws, "how are customers billed", embed_query_fn=lambda q: fake_embed([q])[0], k=3)
ok(any(h["doc_id"] == "d1" for h in vec), "semantic query finds the billing doc")
exp = hybrid_search(hws, "chargeCard", embed_query_fn=lambda q: fake_embed([q])[0], expand=1, k=2)
ok(exp and "context" in exp[0], "expand attaches surrounding context")

# -- retrieval primitives -----------------------------------------------------------------------
rec = recent(hws, source="gdocs", limit=5)
ok(len(rec) == 2 and rec[0]["source"] == "gdocs", "recent lists indexed docs")
th = thread(hws, "Refunds")
ok(th and "ZQ-9001" in th[0]["text"], "thread/doc returns the full body by title match")
with Store(hws) as store:
    first_chunk = store.find_docs(source="gdocs")  # sanity that docs exist
ok(bool(first_chunk), "find_docs returns documents")

# -- reembed onto different settings ------------------------------------------------------------
re_res = reembed(hws, embed_fn=fake_embed)
ok(re_res["docs"] == 2 and re_res["chunks"] >= 2, "reembed rebuilds every stored doc")

# -- notion + github render (offline fakes) -----------------------------------------------------
def nfetch(url, headers):
    from urllib.parse import urlparse
    path = urlparse(url).path
    if path.endswith("/pages/01234567-89ab-cdef-0123-456789abcdef"):
        return res(200, {"url": "https://notion.so/p", "last_edited_time": "t1",
                         "properties": {"Name": {"type": "title", "title": [{"plain_text": "Runbook"}]}}})
    if "/children" in path:
        return res(200, {"results": [{"type": "paragraph", "has_children": False,
                                      "paragraph": {"rich_text": [{"plain_text": "Restart the worker."}]}}],
                         "has_more": False, "next_cursor": None})
    return res(404, {})
save_credential("notion", {"token": "secret_x"})
nws = Workspace(repo("notion"))
with Store(nws) as store:
    nr = notion.sync(store, {"pages": ["01234567-89ab-cdef-0123-456789abcdef"]}, settings={}, fetch=nfetch)
    ok(nr["changed"] == ["01234567-89ab-cdef-0123-456789abcdef"], "notion page ingested")
    ok("Restart the worker." in store.get("notion", "01234567-89ab-cdef-0123-456789abcdef").body, "notion blocks rendered")

print(f"bean: {CHECKS - FAILED}/{CHECKS} checks passed" if FAILED == 0 else f"bean: {FAILED}/{CHECKS} checks FAILED")
sys.exit(0 if FAILED == 0 else 1)
