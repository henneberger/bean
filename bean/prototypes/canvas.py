"""Canvas LMS source. Tracks courses (by numeric id) and indexes each course's wiki pages,
syllabus, announcements, and assignment descriptions — all HTML flattened to text. Auth is a
base url (e.g. https://school.instructure.com) + a Bearer access token (generate one under
Account → Settings → New Access Token). Change detection is `updated_at` (or `posted_at` for
announcements) as the revision id. List endpoints paginate via the RFC 5988 `Link` header.
Removing a course prunes everything under it."""

from __future__ import annotations

import re

from ..html import html_to_text
from ..http import api_get
from ..store import Store
from ..workspace import load_credential, save_credential

CRED = "canvas"
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
_COURSE_URL_RE = re.compile(r"instructure\.com/courses/(\d+)", re.I)


# -- refs + auth --------------------------------------------------------------------------------
def parse_add(item: str):
    s = item.strip()
    if s.startswith("canvas:"):
        rest = s.split(":", 1)[1]
        if rest.startswith("course:"):
            rest = rest.split(":", 1)[1]
        return ("courses", rest) if rest.isdigit() else None
    if "instructure.com/courses/" in s:
        m = _COURSE_URL_RE.search(s)
        if m:
            return ("courses", m.group(1))
    return None


def connect(*, token=None, url=None, email=None, key=None, secret=None, method=None,
            fetch=None, log=print) -> dict:
    if not token or not url:
        raise RuntimeError(
            "pass --url https://school.instructure.com --token <access-token> "
            "(generate one under Account → Settings → New Access Token).")
    base = _base(url)
    who = _one(f"{base}/api/v1/users/self", _headers(token), fetch)
    save_credential(CRED, {"url": base, "token": token, "name": who.get("name")})
    log(f"✓ Canvas connected as {who.get('name')}.")
    return who


def connected() -> dict | None:
    return load_credential(CRED)


def _base(url: str) -> str:
    b = url.rstrip("/")
    return b[:-len("/api/v1")] if b.endswith("/api/v1") else b


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _one(url, headers, fetch) -> dict:
    res = api_get(url, headers, fetch=fetch)
    if not res.ok:
        raise RuntimeError(f"HTTP {res.status}")
    return res.json()


def _next_link(headers: dict) -> str | None:
    link = next((v for k, v in (headers or {}).items() if k.lower() == "link"), "")
    m = _NEXT_RE.search(link or "")
    return m.group(1) if m else None


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    cred = load_credential(CRED)
    if not cred:
        raise RuntimeError("not connected — run `bean auth canvas --url … --token …`.")
    headers = _headers(cred["token"])
    base = cred["url"].rstrip("/")
    api = f"{base}/api/v1"
    courses = list(dict.fromkeys(str(c) for c in config.get("courses", [])))

    def paged(url):
        while url:
            res = api_get(url, headers, fetch=fetch)
            if not res.ok:
                return
            data = res.json()
            if isinstance(data, list):
                yield from data
            url = _next_link(res.headers)

    changed = []
    for cid in courses:
        # Wiki pages (body included inline).
        for p in paged(f"{api}/courses/{cid}/pages?include[]=body&per_page=100"):
            _emit(store, changed, log, f"{cid}/page/{p.get('url')}",
                  p.get("title") or "Page", f"{base}/courses/{cid}/pages/{p.get('url')}",
                  p.get("updated_at"), p.get("title"), p.get("body"))
        # Syllabus.
        try:
            course = _one(f"{api}/courses/{cid}?include[]=syllabus_body", headers, fetch)
            syl = course.get("syllabus_body")
            if syl:
                _emit(store, changed, log, f"{cid}/syllabus",
                      f"{course.get('name') or cid} syllabus",
                      f"{base}/courses/{cid}/assignments/syllabus",
                      course.get("updated_at"), "Syllabus", syl)
        except Exception as err:
            log(f"canvas: syllabus skipped for {cid} ({err})")
        # Announcements.
        for a in paged(f"{api}/announcements?context_codes[]=course_{cid}&per_page=100"):
            _emit(store, changed, log, f"{cid}/announcement/{a.get('id')}",
                  a.get("title") or "Announcement", a.get("html_url"),
                  a.get("posted_at") or a.get("created_at"), a.get("title"), a.get("message"))
        # Assignments.
        for a in paged(f"{api}/courses/{cid}/assignments?per_page=100"):
            _emit(store, changed, log, f"{cid}/assignment/{a.get('id')}",
                  a.get("name") or "Assignment", a.get("html_url"),
                  a.get("updated_at"), a.get("name"), a.get("description"))

    wanted = set(courses)
    removed = [d for d in store.doc_ids(CRED) if d.split("/", 1)[0] not in wanted]
    for doc_id in removed:
        store.delete(CRED, doc_id)
    return {"changed": changed, "removed": removed}


def _emit(store, changed, log, doc_id, title, url, rev, heading, html) -> None:
    try:
        body = f"# {heading or title}\n\n{html_to_text(html or '')}"
        if store.upsert(CRED, doc_id, title=title, url=url, revision_id=rev, body=body,
                        meta={"modified_at": rev}):
            changed.append(doc_id)
            log(f"canvas: updated {doc_id}")
    except Exception as err:
        log(f"canvas: {doc_id} skipped ({err})")
