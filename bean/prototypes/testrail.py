"""TestRail source. Tracks projects (by numeric id) and indexes their test cases, sections, and
runs. Each case doc carries its title, preconditions, and steps + expected results. Auth is a
base url + account email + API key sent as HTTP Basic (enable the API under TestRail →
Administration → Site Settings → API). The REST base is `/index.php?/api/v2/…`. Change detection
is the case `updated_on` (epoch) as the revision id; sections/runs fall back to a content hash.
Every tracked project is walked in full each sync, so pruning is a seen-set."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from ..html import html_to_text
from ..http import api_json
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "testrail"


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    if s.startswith("testrail:"):
        rest = s.split(":", 1)[1]
        if rest.startswith("project:"):
            rest = rest.split(":", 1)[1]
        return ("projects", rest) if rest.isdigit() else None
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    tok = token or key
    if not url or not email or not tok:
        raise RuntimeError(
            "pass --url https://you.testrail.io --email <account-email> --token <api-key> "
            "(enable the API under Administration → Site Settings → API).")
    base = url.rstrip("/")
    cred = {"url": base, "email": email, "token": tok, "name": None}
    api_json(_url(base, "get_projects"), _headers(email, tok), fetch=fetch)
    save_credential(CRED, cred)
    log(f"✓ TestRail connected ({email}).")
    return cred


def connected() -> dict | None:
    return load_credential(CRED)


def _headers(email: str, token: str) -> dict:
    raw = f"{email}:{token}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json"}


def _url(base: str, endpoint: str) -> str:
    return f"{base}/index.php?/api/v2/{endpoint}"


def _iso(epoch) -> str | None:
    # TestRail timestamps are seconds since epoch.
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _rows(resp, key: str) -> list:
    # TestRail 7.x returns {key: [...], _links: {...}}; older returns a bare list.
    if isinstance(resp, dict):
        return resp.get(key) or []
    return resp if isinstance(resp, list) else []


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth testrail --url … --email … --token …`.")
    headers = _headers(cred["email"], cred["token"])
    base = cred["url"].rstrip("/")
    projects = list(dict.fromkeys(str(p) for p in config.get("projects", [])))

    def get(endpoint):
        return api_json(_url(base, endpoint), headers, fetch=fetch)

    changed, seen = [], set()
    for pid in projects:
        try:
            suites = _rows(get(f"get_suites/{pid}"), "suites")
        except Exception:
            suites = []
        suite_ids = [s.get("id") for s in suites] or [None]
        for sid in suite_ids:
            try:
                for case in _iter_cases(get, pid, sid):
                    _emit(store, base, _case_doc(base, case), changed, seen, log)
            except Exception as err:
                log(f"testrail: cases skipped for project {pid} ({err})")
            try:
                sfx = f"&suite_id={sid}" if sid else ""
                for sec in _rows(get(f"get_sections/{pid}{sfx}"), "sections"):
                    _emit(store, base, _section_doc(base, sec), changed, seen, log)
            except Exception as err:
                log(f"testrail: sections skipped for project {pid} ({err})")
        try:
            for run in _rows(get(f"get_runs/{pid}"), "runs"):
                _emit(store, base, _run_doc(base, run), changed, seen, log)
        except Exception as err:
            log(f"testrail: runs skipped for project {pid} ({err})")

    removed = [d for d in store.doc_ids(CRED) if d not in seen]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _iter_cases(get, pid, sid):
    limit, offset = 250, 0
    while True:
        sfx = f"&suite_id={sid}" if sid else ""
        resp = get(f"get_cases/{pid}?limit={limit}&offset={offset}{sfx}")
        cases = _rows(resp, "cases")
        if not cases:
            return
        yield from cases
        if len(cases) < limit:
            return
        offset += limit


def _emit(store, base, doc, changed, seen, log) -> None:
    if not doc:
        return
    doc_id = doc["id"]
    seen.add(doc_id)
    if store.upsert(CRED, doc_id, title=doc["title"], url=doc["url"],
                    revision_id=doc["rev"], body=doc["body"], meta=doc.get("meta")):
        changed.append(doc_id)
        log(f"testrail: updated {doc_id}")


def _case_doc(base, case) -> dict | None:
    cid = case.get("id")
    if cid is None:
        return None
    title = case.get("title") or f"Case {cid}"
    lines = [f"# C{cid}: {title}"]
    pre = html_to_text(str(case.get("custom_preconds") or ""))
    if pre:
        lines += ["", f"Preconditions: {pre}"]
    steps = case.get("custom_steps_separated")
    if isinstance(steps, list) and steps:
        for i, st in enumerate(steps, 1):
            content = html_to_text(str(st.get("content") or ""))
            expected = html_to_text(str(st.get("expected") or ""))
            lines += ["", f"Step {i}: {content}"]
            if expected:
                lines.append(f"Expected: {expected}")
    else:
        body = html_to_text(str(case.get("custom_steps") or ""))
        expected = html_to_text(str(case.get("custom_expected") or ""))
        if body:
            lines += ["", f"Steps: {body}"]
        if expected:
            lines += ["", f"Expected: {expected}"]
    rev = case.get("updated_on") or case.get("created_on")
    return {"id": f"case/{cid}", "title": f"C{cid}: {title}",
            "url": f"{base}/index.php?/cases/view/{cid}", "rev": str(rev) if rev else None,
            "body": "\n".join(lines), "meta": {"modified_at": _iso(rev)}}


def _section_doc(base, sec) -> dict | None:
    sid = sec.get("id")
    if sid is None:
        return None
    name = sec.get("name") or f"Section {sid}"
    body = f"# {name}\n\n{html_to_text(str(sec.get('description') or ''))}"
    return {"id": f"section/{sid}", "title": name,
            "url": f"{base}/index.php?/sections/view/{sid}", "rev": None, "body": body}


def _run_doc(base, run) -> dict | None:
    rid = run.get("id")
    if rid is None:
        return None
    name = run.get("name") or f"Run {rid}"
    body = f"# {name}\n\n{html_to_text(str(run.get('description') or ''))}"
    rev = run.get("completed_on") or run.get("created_on")
    return {"id": f"run/{rid}", "title": name,
            "url": f"{base}/index.php?/runs/view/{rid}", "rev": str(rev) if rev else None,
            "body": body}
