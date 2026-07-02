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
with Store(ews) as _s:
    ok(_s.get_state("last_sync") is not None, "run_sync records last_sync for staleness checks")
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

# == retrieval upgrades: weighted RRF, routing, recency, merge, enrichment, large chunks, rerank ==
from bean.search import related as _related, _merge_sections, _query_weights  # noqa: E402
from bean import graph as _graph  # noqa: E402

_WCFG = {"vector_weight": 1.0, "keyword_weight": 1.0, "auto_weight": True}
vw, kw = _query_weights("ZQ-9001", _WCFG)
ok(kw > vw, "auto_weight leans keyword for an identifier query")
vw, kw = _query_weights("how are customers billed today", _WCFG)
ok(vw > kw, "auto_weight leans vector for a natural-language question")
vw2, kw2 = _query_weights("ZQ-9001", {**_WCFG, "auto_weight": False})
ok(vw2 == 1.0 and kw2 == 1.0, "auto_weight off keeps the fixed weights")

# query variants: a variant carrying the identifier rescues a doc the main query misses
qvws = Workspace(repo("q-variants"))
with Store(qvws) as store:
    store.upsert("gdocs", "dm", title="Meeting", url=None, revision_id=None,
                 body="the quarterly planning notes for the team roadmap discussion this week")
    store.upsert("gdocs", "di", title="Ticket", url=None, revision_id=None,
                 body="incident ZX-42 root cause analysis and the remediation steps that were taken")
reembed(qvws, embed_fn=fake_embed)
main_only = hybrid_search(qvws, "quarterly roadmap planning", embed_query_fn=lambda q: fake_embed([q])[0], k=5)
with_variant = hybrid_search(qvws, "quarterly roadmap planning", queries=["ZX-42"],
                             embed_query_fn=lambda q: fake_embed([q])[0], k=5)
ok(main_only[0]["doc_id"] == "dm", "main query ranks the topical doc first")
ok(with_variant[0]["doc_id"] == "di", "the ZX-42 variant floats the identifier doc to the top via fusion")

# recency: with decay on, the newer doc outranks an equally-relevant old one
rws = Workspace(repo("recency"))
with Store(rws) as store:
    store.upsert("gdocs", "old", title="Old widget", url=None, revision_id=None,
                 body="widget alpha beta gamma delta epsilon components overview and details",
                 meta={"modified_at": "2019-01-01T00:00:00Z"})
    store.upsert("gdocs", "new", title="New widget", url=None, revision_id=None,
                 body="widget alpha beta gamma delta epsilon components overview and details",
                 meta={"modified_at": "2026-06-01T00:00:00Z"})
reembed(rws, embed_fn=fake_embed)
cfgmod.save_global({"search": {"merge_sections": False}})
base = hybrid_search(rws, "widget alpha", embed_query_fn=lambda q: fake_embed([q])[0], k=2)
cfgmod.save_global({"search": {"merge_sections": False, "recency_decay": 8.0, "recency_floor": 0.05}})
decayed = hybrid_search(rws, "widget alpha", embed_query_fn=lambda q: fake_embed([q])[0], k=2, now=NOW)
ok(decayed and decayed[0]["doc_id"] == "new", f"recency decay floats the newer doc to the top ({[h['doc_id'] for h in decayed]})")
ok({h["doc_id"] for h in base} == {"old", "new"}, "both docs retrieved regardless of recency")
cfgmod.save_global({})

# section merge: two adjacent chunks of one doc collapse into a single section spanning both
mws = Workspace(repo("merge"))
body = "\n".join(f"line {i} content here about topic" for i in range(120))
with Store(mws) as store:
    store.upsert("gdocs", "big", title="Big", url=None, revision_id=None, body=body)
reembed(mws, embed_fn=fake_embed)
with Store(mws) as store:
    ch = store._rows("SELECT chunk_id AS id, source, doc_id, title, url, ord, start, \"end\", text "
                     "FROM chunks WHERE doc_id='big' ORDER BY ord", [])
    two = [{**ch[0], "score": 0.9}, {**ch[1], "score": 0.8}]
    merged = _merge_sections(store, two)
    ok(len(merged) == 1, "adjacent same-doc chunks merge into one section")
    ok(merged[0]["start"] == ch[0]["start"] and merged[0]["end"] == ch[1]["end"],
       "merged section spans the union line range")

# chunk enrichment: title_prefix lets a title-only term retrieve a doc whose body lacks it
ews = Workspace(repo("enrich"))
with Store(ews) as store:
    store.upsert("gdocs", "z", title="Zephyr", url=None, revision_id=None,
                 body="the quick brown fox jumps over the lazy dog again and again today")
reembed(ews, embed_fn=fake_embed)  # title_prefix defaults True
hit = hybrid_search(ews, "Zephyr", embed_query_fn=lambda q: fake_embed([q])[0], k=3)
ok(any(h["doc_id"] == "z" for h in hit), "title_prefix embeds the title so a title term retrieves the doc")
with Store(ews) as _st:
    ok("Zephyr" not in _st.get("gdocs", "z").body, "…even though the body never says it (title stored separately)")

# large chunks: enabling them adds coarse vectors on top of the base chunks
lgws = Workspace(repo("largechunks"))
longbody = "\n".join(f"line {i} about payments and billing" for i in range(200))
with Store(lgws) as store:
    store.upsert("gdocs", "L", title="Long", url=None, revision_id=None, body=longbody)
cfgmod.save_global({})
n_base = reembed(lgws, embed_fn=fake_embed)["chunks"]
cfgmod.save_global({"chunking": {"large_chunks": True}})
n_large = reembed(lgws, embed_fn=fake_embed)["chunks"]
ok(n_large > n_base, f"large_chunks adds coarse vectors ({n_base} -> {n_large})")
cfgmod.save_global({})

# rerank: an injected cross-encoder reorders the fused candidates
rkws = Workspace(repo("rerank"))
with Store(rkws) as store:
    store.upsert("gdocs", "a", title="A", url=None, revision_id=None,
                 body="payments billing invoice records for the finance team monthly review")
    store.upsert("gdocs", "b", title="B", url=None, revision_id=None,
                 body="payments billing MARKER invoice records for the finance team monthly review")
reembed(rkws, embed_fn=fake_embed)
cfgmod.save_global({"search": {"rerank": {"enabled": True}, "merge_sections": False}})
rr = hybrid_search(rkws, "payments billing", embed_query_fn=lambda q: fake_embed([q])[0], k=2,
                   rerank_fn=lambda q, texts: [1.0 if "MARKER" in t else 0.0 for t in texts])
ok(rr and rr[0]["doc_id"] == "b", "injected reranker floats the MARKER doc to the top")
cfgmod.save_global({})

# graph: implied edges (authored_by / in-container) + `related` one-hop expansion
ok([e["dst"] for e in _graph.implied_edges(type("D", (), {"source": "github", "doc_id": "acme/repo#1", "author": "Ada"})())]
   == ["Ada", "acme/repo"], "implied_edges derives author + repo container from a github doc id")
gws2 = Workspace(repo("graph"))
with Store(gws2) as store:
    for did, auth in [("acme/repo#1", "Ada"), ("acme/repo#2", "Bo"), ("other/x#9", "Ada")]:
        store.upsert("github", did, title=did, url=None, revision_id=None, body=f"issue {did}",
                     meta={"author": auth})
        store.replace_edges("github", did, _graph.implied_edges(store.get("github", did)))
    rel = store.related("github", "acme/repo#1")
    ids = {r["doc_id"] for r in rel}
    ok("acme/repo#2" in ids, "related finds a doc in the same repo container")
    ok("other/x#9" in ids, "related finds another doc by the same author")
    ok(all("reason" in r for r in rel), "each related hit carries a reason")

# metadata filters: author + date narrowing on search and recent
fws = Workspace(repo("filters"))
with Store(fws) as store:
    store.upsert("gdocs", "ada1", title="Ada plan", url=None, revision_id=None,
                 body="roadmap widget alpha planning notes for the next quarter and beyond",
                 meta={"author": "Ada Lovelace", "modified_at": "2026-06-01T00:00:00Z"})
    store.upsert("gdocs", "bob1", title="Bob plan", url=None, revision_id=None,
                 body="roadmap widget alpha planning notes for the next quarter and beyond",
                 meta={"author": "Bob", "modified_at": "2020-01-01T00:00:00Z"})
reembed(fws, embed_fn=fake_embed)
byauthor = hybrid_search(fws, "roadmap widget", author="Ada", embed_query_fn=lambda q: fake_embed([q])[0], k=5)
ok({h["doc_id"] for h in byauthor} == {"ada1"}, "search --author filters to that author's docs")
recent_since = recent(fws, since="2025-01-01")
ok({h["doc_id"] for h in recent_since} == {"ada1"}, "recent --since filters by modified date")
ok({h["doc_id"] for h in recent(fws, author="Bob")} == {"bob1"}, "recent --author filters by author")

# staleness: bean warns when the index is old, but never syncs on its own
from bean.cli import _staleness_note  # noqa: E402
import datetime as _dt  # noqa: E402
stws = Workspace(repo("stale"))
ok(_staleness_note(stws) is None, "never-synced index does not nag (setup handles it)")
with Store(stws) as store:
    store.set_state("last_sync", "2020-01-01T00:00:00+00:00")
_note = _staleness_note(stws)
ok(_note is not None and "stale" in _note and "sync" in _note, "an old last_sync triggers a stale warning")
with Store(stws) as store:
    store.set_state("last_sync", _dt.datetime.now(_dt.timezone.utc).isoformat())
ok(_staleness_note(stws) is None, "a fresh sync clears the warning")
cfgmod.save_global({"sync": {"stale_days": 0}})
with Store(stws) as store:
    store.set_state("last_sync", "2020-01-01T00:00:00+00:00")
ok(_staleness_note(stws) is None, "stale_days=0 disables the warning")
cfgmod.save_global({})

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

# == new connectors (offline: fake fetch, real Store) ===========================================
from bean import confluence, jira, zendesk, discord, microsoft, salesforce  # noqa: E402  (core)
from bean.prototypes import (linear, asana, trello, intercom, gmail, coda, servicenow,  # noqa: E402
                             readwise, figma, web, rss, obsidian, sqldb, airtable, gsheets,
                             dropbox, buckets)


def check(name, fn):
    """Run one connector's offline test in isolation so a raise can't abort the whole suite."""
    try:
        fn()
    except Exception as err:  # noqa: BLE001
        import traceback
        ok(False, f"{name}: raised {err!r}\n{traceback.format_exc()}")


def _store(tag):
    return Store(Workspace(repo(tag)))


def t_confluence():
    save_credential("confluence", {"method": "cloud", "url": "https://x.atlassian.net/wiki",
                                   "email": "e", "token": "t"})
    def f(u, h, method="GET", body=None):
        if "spaceKey" in u:
            return res(200, {"results": [{"id": "123", "title": "Runbook",
                "body": {"storage": {"value": "<p>Restart the worker.</p>"}},
                "version": {"number": 2, "by": {"displayName": "Ada"}},
                "history": {"lastUpdated": {"when": "2026-01-02T00:00:00Z"}},
                "_links": {"webui": "/pages/123"}}], "size": 1})
        return res(200, {})
    with _store("confluence") as s:
        r = confluence.sync(s, {"spaces": ["ENG"], "pages": []}, settings={}, fetch=f)
        ok(r["changed"] == ["123"], f"confluence page ingested ({r})")
        ok("Restart the worker." in s.get("confluence", "123").body, "confluence storage HTML flattened")
        r2 = confluence.sync(s, {"spaces": ["ENG"], "pages": []}, settings={}, fetch=f)
        ok(r2["changed"] == [], "confluence unchanged version is a no-op")


def t_jira():
    save_credential("jira", {"method": "cloud", "url": "https://x.atlassian.net", "email": "e", "token": "t"})
    def f(u, h, method="GET", body=None):
        return res(200, {"issues": [{"key": "PROJ-1", "fields": {"summary": "Fix it",
            "updated": "2026-01-01T00:00:00.000+0000", "status": {"name": "Open"},
            "reporter": {"displayName": "B"}, "assignee": None, "description": "desc",
            "comment": {"comments": []}}}], "total": 1})
    with _store("jira") as s:
        r = jira.sync(s, {"projects": ["PROJ"]}, settings={}, fetch=f)
        ok(r["changed"] == ["PROJ-1"], f"jira issue ingested ({r})")


def t_linear():
    save_credential("linear", {"token": "lin"})
    def f(u, h, method="GET", body=None):
        return res(200, {"data": {"issues": {"nodes": [{"identifier": "ENG-1", "title": "T",
            "description": "d", "updatedAt": "2026-01-01T00:00:00Z", "url": "http://l",
            "state": {"name": "Todo"}, "assignee": None, "comments": {"nodes": []}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
    with _store("linear") as s:
        r = linear.sync(s, {"teams": ["ENG"]}, settings={}, fetch=f)
        ok(r["changed"] == ["ENG-1"], f"linear issue ingested via GraphQL ({r})")


def t_asana():
    save_credential("asana", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if "/stories" in u:
            return res(200, {"data": []})
        return res(200, {"data": [{"gid": "g1", "name": "Task", "notes": "n",
                                   "modified_at": "2026-01-01"}]})
    with _store("asana") as s:
        r = asana.sync(s, {"projects": ["p1"]}, settings={}, fetch=f)
        ok(r["changed"] == ["g1"], f"asana task ingested ({r})")


def t_trello():
    save_credential("trello", {"key": "k", "token": "t"})
    def f(u, h, method="GET", body=None):
        return res(200, [{"id": "c1", "name": "Card", "desc": "d", "url": "http://t",
                          "dateLastActivity": "2026-01-01", "actions": []}])
    with _store("trello") as s:
        r = trello.sync(s, {"boards": ["b1"]}, settings={}, fetch=f)
        ok(r["changed"] == ["c1"], f"trello card ingested ({r})")


def t_zendesk():
    save_credential("zendesk", {"subdomain": "acme", "email": "e", "token": "t"})
    def f(u, h, method="GET", body=None):
        if "incremental/tickets" in u:
            return res(200, {"tickets": [{"id": 7, "subject": "S", "updated_at": "2026-01-01",
                "created_at": "2025-12-01", "description": "d", "status": "open"}],
                "end_of_stream": True, "end_time": 123})
        if "/tickets/7/comments" in u:
            return res(200, {"comments": []})
        if "help_center/articles" in u:
            return res(200, {"articles": [{"id": 3, "title": "A", "body": "<p>x</p>",
                "updated_at": "2026-01-01", "created_at": "2025-01-01", "html_url": "http://a"}],
                "next_page": None})
        return res(404, {})
    with _store("zendesk") as s:
        r = zendesk.sync(s, {}, settings={}, fetch=f, now=200.0)
        ok(sorted(r["changed"]) == ["article/3", "ticket/7"], f"zendesk tickets+articles ({r})")


def t_intercom():
    save_credential("intercom", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if "/conversations/11" in u:
            return res(200, {"id": "11", "updated_at": 100, "created_at": 90,
                "source": {"body": "<p>hi</p>", "author": {"name": "cust"}},
                "conversation_parts": {"conversation_parts": []}})
        if "/conversations" in u:
            return res(200, {"conversations": [{"id": "11", "updated_at": 100}], "pages": {}})
        if "/articles" in u:
            return res(200, {"data": [{"id": "5", "title": "A", "body": "<p>b</p>",
                                       "updated_at": 100, "created_at": 90}], "pages": {}})
        return res(404, {})
    with _store("intercom") as s:
        r = intercom.sync(s, {}, settings={}, fetch=f)
        ok(sorted(r["changed"]) == ["article/5", "conversation/11"], f"intercom convo+article ({r})")


def t_discord():
    import datetime as _dt
    save_credential("discord", {"token": "t"})
    mts = NOW - 2 * 86400
    iso = _dt.datetime.fromtimestamp(mts, tz=_dt.timezone.utc).isoformat()
    week = slack.iso_week(mts)
    def f(u, h, method="GET", body=None):
        if "/channels/999/messages" in u:
            if "before=" in u:
                return res(200, [])
            return res(200, [{"id": "1000", "timestamp": iso, "content": "hello team",
                              "author": {"username": "ada"}}])
        if "/channels/999" in u:
            return res(200, {"name": "general"})
        return res(404, {})
    with _store("discord") as s:
        r = discord.sync(s, {"channels": ["999"]}, settings={}, fetch=f, now=NOW)
        ok(r["changed"] == [f"general/{week}"], f"discord week digest ({r})")
        ok("hello team" in s.get("discord", f"general/{week}").body, "discord message rendered")


def t_gmail():
    import base64 as _b64
    save_credential("gmail", {"method": "gcloud"})
    data = _b64.urlsafe_b64encode(b"Hello from the thread").decode().rstrip("=")
    def f(u, h, method="GET", body=None):
        if "/threads/t1" in u:
            return res(200, {"id": "t1", "historyId": "55", "messages": [{"payload": {
                "mimeType": "text/plain", "headers": [{"name": "Subject", "value": "Hi"},
                {"name": "From", "value": "a@b.c"}], "body": {"data": data}}}]})
        if "/threads" in u:
            return res(200, {"threads": [{"id": "t1", "historyId": "55"}]})
        return res(404, {})
    with _store("gmail") as s:
        r = gmail.sync(s, {"queries": ["x"]}, settings={}, fetch=f, token_fn=lambda force=False: "tok")
        ok(r["changed"] == ["thread/t1"], f"gmail thread ingested (gcloud) ({r})")
        ok("Hello from the thread" in s.get("gmail", "thread/t1").body, "gmail body base64url-decoded")


def t_microsoft():
    save_credential("microsoft", {"method": "az"})
    def f(u, h, method="GET", body=None):
        if "/me/drive/root/children" in u:
            return res(200, {"value": [{"id": "F1", "name": "notes.md", "file": {}, "eTag": "e1",
                "@microsoft.graph.downloadUrl": "https://dl/notes"}]})
        if u.startswith("https://dl/"):
            return res(200, "# notes from onedrive")
        return res(200, {"value": []})
    with _store("microsoft") as s:
        r = microsoft.sync(s, {"drives": ["me"]}, settings={}, fetch=f, token_fn=lambda force=False: "tok")
        ok(r["changed"] == ["file/F1"], f"microsoft onedrive file ingested ({r})")


def t_coda():
    save_credential("coda", {"token": "t"})
    def f(u, h, method="GET", body=None):
        p = u.split("?")[0]
        if method == "POST" and p.endswith("/export"):
            return res(200, {"id": "r1", "href": "https://coda.io/apis/v1/docs/D1/pages/p1/export/r1"})
        if "/export/" in p:
            return res(200, {"status": "complete", "downloadLink": "https://dl/coda"})
        if p.endswith("/pages"):
            return res(200, {"items": [{"id": "p1", "name": "Page", "contentVersion": 3,
                                        "browserLink": "http://c"}]})
        if p.startswith("https://dl/"):
            return res(200, "# Page body")
        return res(404, {})
    with _store("coda") as s:
        r = coda.sync(s, {"docs": ["D1"]}, settings={}, fetch=f, sleep=lambda x: None)
        ok(r["changed"] == ["D1/p1"], f"coda page exported+ingested ({r})")


def t_salesforce():
    save_credential("salesforce", {"token": "t", "url": "https://x.my.salesforce.com"})
    def f(u, h, method="GET", body=None):
        if "Knowledge__kav" in u:
            return res(200, {"records": [{"Id": "k1", "Title": "KB", "Summary": "<p>s</p>",
                                          "UrlName": "kb", "LastModifiedDate": "2026-01-01"}]})
        if "Case" in u:
            return res(200, {"records": [{"Id": "c1", "CaseNumber": "0001", "Subject": "Sub",
                "Description": "<p>d</p>", "Status": "New", "LastModifiedDate": "2026-01-01"}]})
        return res(200, {"records": []})
    with _store("salesforce") as s:
        r = salesforce.sync(s, {}, settings={}, fetch=f)
        ok(sorted(r["changed"]) == ["article/k1", "case/c1"], f"salesforce KB+cases ({r})")


def t_servicenow():
    save_credential("servicenow", {"method": "token", "subdomain": "acme", "token": "t"})
    def f(u, h, method="GET", body=None):
        if "/table/kb_knowledge" in u:
            return res(200, {"result": [{"sys_id": "a1", "short_description": "KB",
                                         "text": "<p>t</p>", "sys_updated_on": "2026-01-01"}]})
        if "/table/incident" in u:
            return res(200, {"result": [{"sys_id": "i1", "short_description": "Inc",
                "description": "<p>d</p>", "number": "INC1", "sys_updated_on": "2026-01-01"}]})
        return res(200, {"result": []})
    with _store("servicenow") as s:
        r = servicenow.sync(s, {}, settings={}, fetch=f)
        ok(sorted(r["changed"]) == ["incident/i1", "kb_knowledge/a1"], f"servicenow KB+incidents ({r})")


def t_readwise():
    save_credential("readwise", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if "/v2/export" in u:
            return res(200, {"results": [{"user_book_id": 10, "title": "Book", "author": "A",
                "highlights": [{"text": "insight", "highlighted_at": "2026-01-01"}]}],
                "nextPageCursor": None})
        if "/v3/list" in u:
            return res(403, {"detail": "no reader"})
        return res(404, {})
    with _store("readwise") as s:
        r = readwise.sync(s, {}, settings={}, fetch=f)
        ok(r["changed"] == ["book/10"], f"readwise book highlights ({r}); reader 403 tolerated")


def t_figma():
    save_credential("figma", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if u.endswith("/comments"):
            return res(200, {"comments": []})
        if "/files/ABC" in u:
            return res(200, {"name": "Design", "version": "7", "lastModified": "2026-01-01",
                             "document": {"children": [{"type": "TEXT", "characters": "Hello"}]}})
        return res(404, {})
    with _store("figma") as s:
        r = figma.sync(s, {"files": ["ABC"]}, settings={}, fetch=f)
        ok(r["changed"] == ["file/ABC"], f"figma file text+comments ({r})")
        ok("Hello" in s.get("figma", "file/ABC").body, "figma TEXT nodes collected")


def t_airtable():
    save_credential("airtable", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if "/meta/bases/" in u:
            return res(403, {})
        return res(200, {"records": [{"id": "rec1", "createdTime": "2026-01-01",
                                      "fields": {"Name": "Alpha"}}]})
    with _store("airtable") as s:
        r = airtable.sync(s, {"tables": ["appX/tblY"]}, settings={}, fetch=f)
        ok(r["changed"] == ["appX/tblY/rec1"], f"airtable record ingested ({r})")


def t_gsheets():
    def f(u, h, method="GET", body=None):
        if "/drive/v3/files/" in u:
            return res(200, {"modifiedTime": "2026-01-01", "name": "Book"})
        if "/values/" in u:
            return res(200, {"values": [["h1", "h2"], ["a", "b"]]})
        if "/spreadsheets/sid1" in u:
            return res(200, {"properties": {"title": "Book"},
                             "sheets": [{"properties": {"title": "Tab1", "sheetId": 0}}]})
        return res(404, {})
    with _store("gsheets") as s:
        r = gsheets.sync(s, {"sheets": ["sid1"]}, settings={},
                         token_fn=lambda force=False: "tok", fetch=f)
        ok(r["changed"] == ["sid1/Tab1"], f"gsheets tab → markdown table ({r})")
        ok("| a | b |" in s.get("gsheets", "sid1/Tab1").body, "gsheets renders a markdown table")


def t_dropbox():
    save_credential("dropbox", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if u.endswith("/files/list_folder"):
            return res(200, {"entries": [{".tag": "file", "id": "id:1", "name": "a.md",
                "path_lower": "/f/a.md", "rev": "r1", "server_modified": "2026-01-01"}],
                "has_more": False})
        if u.endswith("/files/download"):
            return res(200, "# dropbox body")
        return res(404, {})
    with _store("dropbox") as s:
        r = dropbox.sync(s, {"folders": ["/f"]}, settings={}, fetch=f)
        ok(r["changed"] == ["id:1"], f"dropbox markdown file ingested ({r})")


def t_buckets():
    class Fake:
        def list(self, bucket, prefix):
            yield buckets._Obj(key="pre/doc.md", revision="etag1")
        def download(self, bucket, key):
            return b"# hi from s3"
    with _store("buckets") as s:
        r = buckets.sync(s, {"buckets": ["s3://mybucket/pre"]}, settings={}, client=Fake())
        ok(r["changed"] == ["s3://mybucket/pre/doc.md"], f"buckets object ingested ({r})")


def t_web():
    def f(u, h, method="GET", body=None):
        return res(200, "<html><title>Hi</title><body><nav>skip</nav><p>hello world</p></body></html>",
                   {"ETag": '"v1"'})
    with _store("web") as s:
        r = web.sync(s, {"pages": ["http://x/a"], "sitemaps": []}, settings={}, fetch=f)
        ok(r["changed"] == ["http://x/a"], f"web page ingested ({r})")
        ok("hello world" in s.get("web", "http://x/a").body and "skip" not in s.get("web", "http://x/a").body,
           "web extract_readable drops nav chrome")
        r2 = web.sync(s, {"pages": ["http://x/a"], "sitemaps": []}, settings={}, fetch=f)
        ok(r2["changed"] == [], "web unchanged ETag is a no-op")


def t_rss():
    RSS = ("<rss><channel><item><guid>g1</guid><title>T</title><link>http://x/1</link>"
           "<description>hi there</description><pubDate>2026-01-01</pubDate></item></channel></rss>")
    with _store("rss") as s:
        r = rss.sync(s, {"feeds": ["http://x/f"]}, settings={},
                     fetch=lambda u, h, method="GET", body=None: res(200, RSS))
        ok(r["changed"] == ["g1"] and r["removed"] == [], f"rss entry ingested, never prunes ({r})")


def t_obsidian():
    vault = Path(tempfile.mkdtemp(prefix="bean-vault-"))
    (vault / "A.md").write_text("Note A links to [[B]].\n")
    (vault / "B.md").write_text("Note B stands alone.\n")
    with _store("obsidian") as s:
        r = obsidian.sync(s, {"vaults": [str(vault)]}, settings={})
        ok(len(r["changed"]) == 2, f"obsidian indexed both notes ({r})")
        ok("Links: [[B]]" in s.get("obsidian", str(vault / "A.md")).body, "obsidian embeds wikilinks")
        ok("Backlinks" in s.get("obsidian", str(vault / "B.md")).body, "obsidian computes backlinks")


def t_sqldb():
    import sqlite3
    db = Path(tempfile.mkdtemp(prefix="bean-sql-")) / "notes.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE notes(id INTEGER, title TEXT, body TEXT, url TEXT)")
    con.execute("INSERT INTO notes VALUES (1,'t1','refunds go through refundCard','u1')")
    con.execute("INSERT INTO notes VALUES (2,'t2','b2','u2')")
    con.commit(); con.close()
    routed = sqldb.parse_add(f"sql:sqlite:///{db}|SELECT id,title,body,url FROM notes")
    ok(routed and routed[0] == "queries", "sqldb parse_add returns a query dict")
    with _store("sqldb") as s:
        r = sqldb.sync(s, {"queries": [routed[1]]}, settings={})
        ok(len(r["changed"]) == 2, f"sqldb indexed both rows ({r})")
        ok("refundCard" in s.get("sqldb", "notes#1").body, "sqldb maps body column")


for _name, _fn in [
    ("confluence", t_confluence), ("jira", t_jira), ("linear", t_linear), ("asana", t_asana),
    ("trello", t_trello), ("zendesk", t_zendesk), ("intercom", t_intercom), ("discord", t_discord),
    ("gmail", t_gmail), ("microsoft", t_microsoft), ("coda", t_coda), ("salesforce", t_salesforce),
    ("servicenow", t_servicenow), ("readwise", t_readwise), ("figma", t_figma), ("airtable", t_airtable),
    ("gsheets", t_gsheets), ("dropbox", t_dropbox), ("buckets", t_buckets), ("web", t_web),
    ("rss", t_rss), ("obsidian", t_obsidian), ("sqldb", t_sqldb),
]:
    check(_name, _fn)

# == Onyx-parity connectors (offline) ===========================================================
from bean import hubspot  # noqa: E402  (core)
from bean.prototypes import (guru, gitbook, outline, slab, bookstack, document360, mediawiki,  # noqa: E402
                             wikipedia, drupal_wiki, axero, discourse, xenforo, gitlab, bitbucket,
                             clickup, productboard, testrail, canvas, freshdesk, gong, fireflies,
                             highspot, loopio, zulip, egnyte, braintrust, google_site)
from bean.prototypes import imap as imap_conn  # noqa: E402

BIG = 3650000  # a "no lookback filtering" since_days for whole-collection windowed sources


def t_guru():
    save_credential("guru", {"user": "e", "token": "t"})
    def f(u, h, method="GET", body=None):
        return res(200, [{"id": "c1", "preferredPhrase": "Runbook", "slug": "abc",
            "content": "<p>Restart worker.</p>", "lastModified": "2026-01-02",
            "collection": {"name": "Eng"}, "owner": {"firstName": "A", "lastName": "B"}}])
    with _store("guru") as s:
        ok(guru.sync(s, {}, settings={}, fetch=f)["changed"] == ["c1"], "guru card ingested")


def t_gitbook():
    save_credential("gitbook", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if u.endswith("/content"):
            return res(200, {"pages": [{"id": "p1", "title": "Intro", "updatedAt": "2026-01-01",
                                        "urls": {"app": "http://gb/p1"}}]})
        return res(200, {"document": "Hello world markdown", "markdown": "Hello world markdown"})
    with _store("gitbook") as s:
        ok(gitbook.sync(s, {"spaces": ["sp1"]}, settings={}, fetch=f)["changed"] == ["sp1/p1"],
           "gitbook page ingested")


def t_outline():
    save_credential("outline", {"token": "t", "url": "https://out"})
    def f(u, h, method="GET", body=None):
        return res(200, {"data": [{"id": "d1", "title": "Doc", "text": "body text",
            "updatedAt": "2026-01-01", "url": "/doc/d1", "collectionId": "col1"}], "pagination": {}})
    with _store("outline") as s:
        ok(outline.sync(s, {}, settings={}, fetch=f)["changed"] == ["d1"], "outline doc ingested")


def t_slab():
    save_credential("slab", {"token": "t", "url": "https://org.slab.com"})
    def f(u, h, method="GET", body=None):
        b = json.loads(body) if isinstance(body, str) else (body or {})
        if "organization" in b.get("query", ""):
            return res(200, {"data": {"organization": {"posts": [
                {"id": "p1", "title": "T", "updatedAt": "2026-01-01", "version": "5"}]}}})
        return res(200, {"data": {"post": {"title": "T", "content": '[{"insert":"hello slab"}]',
                                           "updatedAt": "2026-01-01", "version": "5"}}})
    with _store("slab") as s:
        ok(slab.sync(s, {}, settings={}, fetch=f)["changed"] == ["p1"], "slab post ingested")


def t_bookstack():
    save_credential("bookstack", {"url": "https://bs", "key": "i", "secret": "x", "id": "i"})
    def f(u, h, method="GET", body=None):
        if "/api/pages/" in u:
            return res(200, {"name": "Page", "html": "<p>page body</p>", "slug": "page",
                             "updated_at": "2026-01-01"})
        return res(200, {"data": [{"id": "7", "name": "Page", "book_id": "3", "book_slug": "bk",
                                   "slug": "page", "updated_at": "2026-01-01"}]})
    with _store("bookstack") as s:
        ok(bookstack.sync(s, {}, settings={}, fetch=f)["changed"] == ["page/7"], "bookstack page ingested")


def t_document360():
    save_credential("document360", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if "/Articles/" in u or "/articles/" in u:
            return res(200, {"data": {"id": "a1", "title": "Art", "html_content": "<p>article body</p>",
                "modified_at": "2026-01-01", "url": "http://d/a1", "description": "desc"}})
        if u.endswith("/categories") or "categories" in u:
            return res(200, {"data": [{"name": "Cat", "articles": [{"id": "a1", "title": "Art"}],
                                       "child_categories": []}]})
        return res(200, {"data": [{"id": "v1", "version_code_name": "v1"}]})
    with _store("document360") as s:
        ok(document360.sync(s, {}, settings={}, fetch=f)["changed"] == ["article/a1"],
           "document360 article ingested")


def t_mediawiki():
    save_credential("mediawiki", {"url": "http://w/api.php"})
    def mw(u, h, method="GET", body=None):
        if "categorymembers" in u:
            return res(200, {"query": {"categorymembers": [{"pageid": 10, "title": "Alpha"}]}})
        return res(200, {"query": {"pages": [{"pageid": 10, "title": "Alpha", "lastrevid": 99,
                                              "extract": "hi", "fullurl": "http://w/Alpha"}]}})
    with _store("mediawiki") as s:
        ok(mediawiki.sync(s, {"categories": ["Foo"]}, settings={}, fetch=mw)["changed"] == ["10"],
           "mediawiki page ingested")


def t_wikipedia():
    save_credential("wikipedia", {"language": "en", "url": "http://w/api.php"})
    def mw(u, h, method="GET", body=None):
        if "categorymembers" in u:
            return res(200, {"query": {"categorymembers": [{"pageid": 10, "title": "Alpha"}]}})
        return res(200, {"query": {"pages": [{"pageid": 10, "title": "Alpha", "lastrevid": 99,
                                              "extract": "hi", "fullurl": "http://w/Alpha"}]}})
    with _store("wikipedia") as s:
        ok("10" in wikipedia.sync(s, {"pages": ["Alpha"]}, settings={}, fetch=mw)["changed"],
           "wikipedia reuses mediawiki crawl")


def t_drupalwiki():
    save_credential("drupalwiki", {"url": "http://dw", "token": "t"})
    def dw(u, h, method="GET", body=None):
        if "/page/8" in u:
            return res(200, {"id": 8, "title": "P", "body": "<p>w</p>", "lastModified": 1700000000})
        if "/page" in u:
            return res(200, {"content": [{"id": 8, "title": "P", "homeSpace": 3,
                                          "lastModified": 1700000000}], "last": True})
        return res(200, {"content": [], "last": True})
    with _store("drupalwiki") as s:
        ok(drupal_wiki.sync(s, {"spaces": ["3"]}, settings={}, fetch=dw, since_days=BIG)["changed"] == ["8"],
           "drupalwiki page ingested")


def t_axero():
    save_credential("axero", {"url": "http://a", "key": "k"})
    def ax(u, h, method="GET", body=None):
        if "StartPage=1" in u or "start" in u.lower():
            return res(200, {"TotalRecords": 1, "ResponseData": [{"ContentID": 42, "ContentTitle": "Doc",
                "ContentURL": "/a/42", "ContentBody": "<p>b</p>", "DateUpdated": "2024-06-01T00:00:00"}]})
        return res(200, {"TotalRecords": 1, "ResponseData": []})
    with _store("axero") as s:
        ok(axero.sync(s, {"spaces": ["1"]}, settings={}, fetch=ax, since_days=BIG)["changed"] == ["42"],
           "axero content ingested")


def t_discourse():
    save_credential("discourse", {"url": "http://d", "token": "k", "email": "u"})
    def dc(u, h, method="GET", body=None):
        if "categories.json" in u:
            return res(200, {"category_list": {"categories": [{"id": 5, "slug": "general"}]}})
        if "/c/general/5.json" in u or ("/c/" in u and "5" in u):
            return res(200, {"topic_list": {"topics": [{"id": 7, "slug": "hi",
                                                        "bumped_at": "2024-06-01T00:00:00Z"}]}})
        if "/t/7.json" in u:
            return res(200, {"title": "Hi", "slug": "hi", "post_stream": {"posts": [
                {"username": "b", "cooked": "<p>x</p>"}]}})
        return res(200, {"topic_list": {"topics": []}})
    with _store("discourse") as s:
        ok(discourse.sync(s, {"categories": ["5"]}, settings={}, fetch=dc, since_days=BIG)["changed"] == ["topic/7"],
           "discourse topic ingested")


def t_xenforo():
    save_credential("xenforo", {"url": "http://x", "key": "k"})
    def xf(u, h, method="GET", body=None):
        if "/api/threads/55" in u or "/threads/55/" in u:
            return res(200, {"thread": {"thread_id": 55, "title": "T", "last_post_date": 1700000000},
                "posts": [{"username": "a", "message_parsed": "<p>p</p>"}], "pagination": {"last_page": 1}})
        if "/api/threads" in u or "/threads" in u:
            return res(200, {"threads": [{"thread_id": 55, "title": "T", "last_post_date": 1700000000}],
                             "pagination": {"last_page": 1}})
        return res(200, {"threads": []})
    with _store("xenforo") as s:
        ok(xenforo.sync(s, {"forums": ["2"]}, settings={}, fetch=xf, since_days=BIG)["changed"] == ["thread/55"],
           "xenforo thread ingested")


def t_gitlab():
    save_credential("gitlab", {"token": "t", "url": "https://gitlab.com"})
    def f(u, h, method="GET", body=None):
        if "/issues" in u:
            return res(200, [{"iid": 7, "title": "Bug", "description": "x",
                "updated_at": "2026-01-02T00:00:00Z", "web_url": "http://g/7",
                "author": {"username": "ada"}, "state": "opened"}])
        if "/notes" in u:
            return res(200, [])
        return res(200, [])
    with _store("gitlab") as s:
        ok(gitlab.sync(s, {"projects": ["g/p"], "include": ["issues"]}, settings={}, fetch=f)["changed"] == ["g/p#7"],
           "gitlab issue ingested")


def t_bitbucket():
    save_credential("bitbucket", {"email": "e", "secret": "s"})
    def f(u, h, method="GET", body=None):
        if "/pullrequests" in u and "/comments" not in u:
            return res(200, {"values": [{"id": 3, "title": "Add", "description": "d", "state": "OPEN",
                "updated_on": "2026-01-02T00:00:00+00:00", "author": {"display_name": "Ada"},
                "links": {"html": {"href": "http://b/3"}}}]})
        return res(200, {"values": []})
    with _store("bitbucket") as s:
        ok(bitbucket.sync(s, {"repos": ["w/r"], "include": ["prs"]}, settings={}, fetch=f)["changed"] == ["w/r#3"],
           "bitbucket PR ingested")


def t_clickup():
    save_credential("clickup", {"token": "pk"})
    def f(u, h, method="GET", body=None):
        if "/comment" in u:
            return res(200, {"comments": []})
        if "/task" in u:
            return res(200, {"tasks": [{"id": "abc", "name": "Do it", "markdown_description": "go",
                "date_updated": "1704153600000", "url": "http://c/abc", "status": {"status": "open"}}],
                "last_page": True})
        return res(200, {"tasks": [], "last_page": True})
    with _store("clickup") as s:
        ok(clickup.sync(s, {"lists": ["L"]}, settings={}, fetch=f)["changed"] == ["abc"],
           "clickup task ingested")


def t_productboard():
    save_credential("productboard", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if "/notes" in u:
            return res(200, {"data": [{"id": "n1", "title": "Note", "content": "<p>hi</p>",
                                       "updatedAt": "2026-01-01"}], "links": {}})
        return res(200, {"data": [], "links": {}})
    with _store("productboard") as s:
        ok(productboard.sync(s, {"include": ["notes", "features"]}, settings={}, fetch=f)["changed"] == ["note/n1"],
           "productboard note ingested")


def t_testrail():
    save_credential("testrail", {"url": "https://x.testrail.io", "email": "e", "token": "k"})
    def f(u, h, method="GET", body=None):
        if "get_cases" in u and "offset=0" in u.replace(" ", ""):
            return res(200, {"cases": [{"id": 5, "title": "Login", "custom_steps": "click",
                                        "custom_expected": "in", "updated_on": 1704153600}]})
        if "get_cases" in u:
            return res(200, {"cases": []})
        if "get_suites" in u:
            return res(200, [])
        return res(200, {"sections": [], "runs": []})
    with _store("testrail") as s:
        ok(testrail.sync(s, {"projects": ["1"]}, settings={}, fetch=f)["changed"] == ["case/5"],
           "testrail case ingested")


def t_canvas():
    save_credential("canvas", {"url": "https://school.instructure.com", "token": "t"})
    def f(u, h, method="GET", body=None):
        if "/pages" in u and "/pages/" not in u:
            return res(200, [{"url": "intro", "title": "Intro", "body": "<p>welcome</p>",
                              "updated_at": "2026-01-01T00:00:00Z", "page_id": 1}])
        if u.rstrip("?").endswith("/courses/42") or "syllabus_body" in u:
            return res(200, {"name": "C", "syllabus_body": "", "updated_at": "2026-01-01T00:00:00Z"})
        return res(200, [])
    with _store("canvas") as s:
        ok(canvas.sync(s, {"courses": ["42"]}, settings={}, fetch=f)["changed"] == ["42/page/intro"],
           "canvas page ingested")


def t_freshdesk():
    save_credential("freshdesk", {"subdomain": "acme", "key": "k"})
    def f(u, h, method="GET", body=None):
        if "/tickets/" in u and "/conversations" in u:
            return res(200, [])
        if "/tickets" in u:
            return res(200, [{"id": 1, "subject": "Hi", "updated_at": "2026-01-01",
                              "created_at": "2025-12-01", "description_text": "d", "description": "<p>d</p>"}])
        return res(200, [])
    with _store("freshdesk") as s:
        ok(freshdesk.sync(s, {"include": ["tickets"]}, settings={}, fetch=f)["changed"] == ["ticket/1"],
           "freshdesk ticket ingested")


def t_hubspot():
    save_credential("hubspot", {"token": "t", "portal_id": "7"})
    def f(u, h, method="GET", body=None):
        if "/tickets" in u:
            return res(200, {"results": [{"id": "5", "updatedAt": "2026-01-02T00:00:00Z",
                "properties": {"subject": "T", "content": "c"}}]})
        return res(200, {"results": []})
    with _store("hubspot") as s:
        ok(hubspot.sync(s, {"include": ["tickets"]}, settings={}, fetch=f)["changed"] == ["ticket/5"],
           "hubspot ticket ingested")


def t_gong():
    save_credential("gong", {"key": "k", "secret": "s", "base": "https://api.gong.io"})
    def f(u, h, method="GET", body=None):
        if "/calls/transcript" in u:
            return res(200, {"callTranscripts": [{"callId": "c1", "transcript": [
                {"speakerId": "s1", "sentences": [{"text": "hi"}]}]}]})
        if "/calls" in u:
            return res(200, {"calls": [{"id": "c1", "title": "Call", "url": "u",
                                        "created": "2026-01-03T00:00:00Z"}], "records": {}})
        return res(200, {})
    with _store("gong") as s:
        ok(gong.sync(s, {}, settings={}, fetch=f)["changed"] == ["call/c1"], "gong call ingested")


def t_fireflies():
    save_credential("fireflies", {"token": "t"})
    state = {"n": 0}
    def f(u, h, method="GET", body=None):
        state["n"] += 1
        if state["n"] == 1:
            return res(200, {"data": {"transcripts": [{"id": "m1", "title": "Mtg",
                "date": 1700000000000, "sentences": [{"speaker_name": "A", "text": "hi"}],
                "summary": {"overview": "ov"}}]}})
        return res(200, {"data": {"transcripts": []}})
    with _store("fireflies") as s:
        ok(fireflies.sync(s, {}, settings={}, fetch=f)["changed"] == ["transcript/m1"],
           "fireflies transcript ingested")


def t_highspot():
    save_credential("highspot", {"key": "k", "secret": "s", "base": "https://api-su2.highspot.com/v1.0"})
    def f(u, h, method="GET", body=None):
        if "/items/it1" in u:
            return res(200, {"id": "it1", "title": "Deck", "description": "<p>d</p>",
                             "date_updated": "2026-01-04"})
        if "/items" in u:
            return res(200, {"collection": [{"id": "it1"}]})
        if "/spots" in u:
            return res(200, {"collection": [{"id": "sp1", "title": "Sales"}]})
        return res(200, {"collection": []})
    with _store("highspot") as s:
        ok(highspot.sync(s, {}, settings={}, fetch=f)["changed"] == ["item/it1"], "highspot item ingested")


def t_loopio():
    save_credential("loopio", {"key": "k", "secret": "s", "subdomain": "acme"})
    def f(u, h, method="GET", body=None):
        return res(200, {"totalPages": 1, "items": [{"id": 42, "questions": [{"text": "Q?"}],
            "answer": {"text": "<p>A</p>"}, "location": {}, "lastUpdatedDate": "2026-01-05"}]})
    with _store("loopio") as s:
        r = loopio.sync(s, {}, settings={}, fetch=f, token_fn=lambda: "faketoken")
        ok(r["changed"] == ["entry/42"], "loopio library entry ingested (token_fn injected)")


def t_zulip():
    save_credential("zulip", {"url": "https://x.zulipchat.com", "email": "b@x", "token": "t"})
    mts = NOW - 2 * 86400
    week = slack.iso_week(mts)
    def f(u, h, method="GET", body=None):
        if "subscriptions" in u:
            return res(200, {"result": "success", "subscriptions": [{"name": "general"}]})
        return res(200, {"result": "success", "found_oldest": True, "found_newest": True,
            "messages": [{"id": 1, "timestamp": mts, "subject": "hi", "content": "hello team",
                          "sender_full_name": "Al", "stream_id": 1, "display_recipient": "general"}]})
    with _store("zulip") as s:
        ok(zulip.sync(s, {"streams": ["general"]}, settings={}, fetch=f, now=NOW)["changed"] == [f"general/{week}"],
           "zulip week digest ingested")


def t_imap():
    save_credential("imap", {"host": "h", "port": 993, "email": "e", "password": "p", "token": "p"})
    class M:
        def select(self, m, readonly=True):
            return ("OK", None)
        def uid(self, cmd, *a):
            if cmd == "search":
                return ("OK", [b"1"])
            return ("OK", [(b'1 (INTERNALDATE "x")', b"From: a@b\r\nSubject: Hi\r\n\r\nBody")])
        def logout(self):
            pass
    with _store("imap") as s:
        r = imap_conn.sync(s, {"mailboxes": ["INBOX"]}, settings={}, imap_factory=lambda: M())
        ok(r["changed"] == ["INBOX/1"], f"imap message ingested ({r})")


def t_egnyte():
    save_credential("egnyte", {"domain": "co", "subdomain": "co", "token": "t"})
    def f(u, h, method="GET", body=None):
        if "/fs/" in u:
            return res(200, {"files": [{"name": "a.txt", "path": "/Shared/a.txt",
                                        "last_modified": "2026-01-01T00:00:00Z",
                                        "is_folder": False, "entry_id": "e1"}], "folders": []})
        return res(200, "file body text")
    with _store("egnyte") as s:
        ok(egnyte.sync(s, {"folders": ["/Shared"]}, settings={}, fetch=f)["changed"] == ["/Shared/a.txt"],
           "egnyte file ingested")


def t_braintrust():
    save_credential("braintrust", {"token": "t"})
    def f(u, h, method="GET", body=None):
        if "/prompt" in u:
            return res(200, {"objects": [{"id": "p1", "name": "P", "_xact_id": "9",
                "prompt_data": {"prompt": {"messages": [{"role": "user", "content": "hi"}]}}}]})
        return res(200, {"objects": []})
    with _store("braintrust") as s:
        ok("prompt/p1" in braintrust.sync(s, {}, settings={}, fetch=f)["changed"],
           "braintrust prompt ingested")


def t_google_site():
    def f(u, h, method="GET", body=None):
        if u.endswith("/home"):
            return res(200, '<title>Home</title><a href="/s/page2">p2</a><body>hi</body>', {"ETag": "v1"})
        return res(200, '<title>P2</title><body>world</body>', {"ETag": "v2"})
    with _store("gsite") as s:
        out = google_site.sync(s, {"sites": ["https://sites.google.com/s/home"]}, settings={}, fetch=f)["changed"]
        ok("https://sites.google.com/s/home" in out, "google_site base page ingested")


for _name, _fn in [
    ("guru", t_guru), ("gitbook", t_gitbook), ("outline", t_outline), ("slab", t_slab),
    ("bookstack", t_bookstack), ("document360", t_document360), ("mediawiki", t_mediawiki),
    ("wikipedia", t_wikipedia), ("drupalwiki", t_drupalwiki), ("axero", t_axero),
    ("discourse", t_discourse), ("xenforo", t_xenforo), ("gitlab", t_gitlab),
    ("bitbucket", t_bitbucket), ("clickup", t_clickup), ("productboard", t_productboard),
    ("testrail", t_testrail), ("canvas", t_canvas), ("freshdesk", t_freshdesk), ("hubspot", t_hubspot),
    ("gong", t_gong), ("fireflies", t_fireflies), ("highspot", t_highspot), ("loopio", t_loopio),
    ("zulip", t_zulip), ("imap", t_imap), ("egnyte", t_egnyte), ("braintrust", t_braintrust),
    ("google_site", t_google_site),
]:
    check(_name, _fn)

# == plugin system: core-only by default, enable a prototype, load a drop-in plugin =============
import bean.sources as _S  # noqa: E402
from bean import config as _cfg  # noqa: E402
from bean.plugins import discover_sources  # noqa: E402

core_keys = {s.key for s in _S.CORE_SOURCES}
ok(core_keys == {"slack", "gdocs", "notion", "github", "confluence", "jira", "zendesk",
                 "salesforce", "hubspot", "microsoft", "discord"}, "11 cloud core connectors")
ok(_S.SOURCES[-1].key == "localfiles", "localfiles registered last (path catch-all)")
ok("linear" not in {s.key for s in _S.SOURCES}, "prototypes are OFF by default")

# enabling a prototype by name (as `bean plugins enable` / a written global config would)
_cfg.save_global({"plugins": {"prototypes": ["linear", "web"]}})
_S.reload_sources()
keys = [s.key for s in _S.SOURCES]
ok("linear" in keys and "web" in keys, "enabled prototypes join the registry")
ok(keys[-1] == "localfiles", "localfiles still last after enabling prototypes")
ok(_S.route_add("linear:ENG")[0].key == "linear", "enabled prototype routes")

# a drop-in plugin file: a standalone module exposing SOURCE
plugdir = Path(tempfile.mkdtemp(prefix="bean-plugins-"))
(plugdir / "acme.py").write_text(
    "from bean.sources import Source\n"
    "def parse_add(item):\n"
    "    return ('boards', item.split(':',1)[1]) if item.startswith('acme:') else None\n"
    "def sync(store, config, *, settings, fetch=None, full=False, since_days=90, log=lambda m: None):\n"
    "    changed = []\n"
    "    for b in config.get('boards', []):\n"
    "        if store.upsert('acme', f'acme/{b}', title=b, url=None, revision_id='r1',\n"
    "                        body=f'# {b}\\nacme board body'):\n"
    "            changed.append(f'acme/{b}')\n"
    "    return {'changed': changed, 'removed': []}\n"
    "def connected():\n"
    "    return {'ok': True}\n"
    "SOURCE = Source('acme', 'acme', 'Acme', ('boards',), sync, parse_add, auth=None,\n"
    "                add_help='acme:BOARD', connected=connected)\n"
)
found = discover_sources(_S.Source, global_config={}, dirs=[plugdir])
ok(len(found) == 1 and found[0].key == "acme", "drop-in plugin discovered from a dir")

_cfg.save_global({"plugins": {"prototypes": [], "paths": [str(plugdir)]}})
_S.reload_sources()
ok(_S.route_add("acme:main") and _S.route_add("acme:main")[0].key == "acme", "drop-in plugin routes")
acme_src = _S.BY_KEY["acme"]
with _store("acme-plugin") as s:
    r = acme_src.sync(s, {"boards": ["main"]}, settings={}, fetch=None)
    ok(r["changed"] == ["acme/main"] and s.get("acme", "acme/main"), "drop-in plugin syncs a document")
_cfg.save_global({})  # reset global config
_S.reload_sources()

print(f"bean: {CHECKS - FAILED}/{CHECKS} checks passed" if FAILED == 0 else f"bean: {FAILED}/{CHECKS} checks FAILED")
sys.exit(0 if FAILED == 0 else 1)
