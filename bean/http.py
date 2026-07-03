"""One HTTP wrapper for every source: 429/5xx retry honoring Retry-After, 401 raises
AuthError for the caller to refresh a token once. `fetch` is injectable so the test suite
runs entirely offline.

The injectable fetch contract is `fetch(url, headers, method="GET", body=None) -> Response`.
GET-only callers (and the older 2-arg test fakes) keep working because `api_get` only ever
passes `(url, headers)`; the POST path passes all four. Give a fake `method`/`body` defaults
and it serves both — that is the whole extension over the original GET-only seam."""

from __future__ import annotations

import time
from dataclasses import dataclass


class AuthError(Exception):
    pass


@dataclass
class Response:
    status: int
    text: str
    headers: dict
    content: bytes | None = None  # raw bytes for binary downloads (e.g. PDFs); text stays lossy

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def raw(self) -> bytes:
        """The body as bytes — the real content when present, else the text re-encoded."""
        return self.content if self.content is not None else self.text.encode("utf-8", "replace")

    def json(self):
        import json
        return json.loads(self.text)


def _requests_fetch(url: str, headers: dict, method: str = "GET", body=None) -> Response:
    import requests
    kw: dict = {"headers": headers, "timeout": 60}
    if body is not None:
        if isinstance(body, (bytes, str)):
            kw["data"] = body
        else:
            kw["json"] = body  # dict/list → JSON body
    r = requests.request(method, url, **kw)
    return Response(status=r.status_code, text=r.text, headers=dict(r.headers), content=r.content)


def _send(url: str, headers, method: str, body, *, fetch, retries: int, sleep) -> Response:
    fetch = fetch or _requests_fetch
    last = None
    for attempt in range(retries + 1):
        res = fetch(url, headers or {}) if (method == "GET" and body is None) \
            else fetch(url, headers or {}, method, body)
        if res.status == 401:
            raise AuthError(f"401 from {url.split('?')[0]}")
        if res.status == 429 or res.status >= 500:
            last = res
            if attempt == retries:
                return res  # caller sees the final failing response
            retry_after = res.headers.get("Retry-After") or res.headers.get("retry-after")
            try:
                delay = max(0.0, float(retry_after))
            except (TypeError, ValueError):
                delay = float(2 ** attempt)
            sleep(delay)
            continue
        return res
    return last


def api_get(url: str, headers: dict | None = None, *, fetch=None, retries: int = 4,
            sleep=time.sleep) -> Response:
    return _send(url, headers, "GET", None, fetch=fetch, retries=retries, sleep=sleep)


def api_post(url: str, headers: dict | None = None, body=None, *, method: str = "POST",
             fetch=None, retries: int = 4, sleep=time.sleep) -> Response:
    """POST (or any non-GET verb) behind the same injectable, retrying seam as api_get.
    `body` is sent as a JSON body when it is a dict/list, or raw when bytes/str."""
    return _send(url, headers, method, body, fetch=fetch, retries=retries, sleep=sleep)


def _detail(res: "Response") -> str:
    detail = res.text[:200]
    try:
        j = res.json()
        err = j.get("error") or j.get("errors") or j.get("message")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("description") or detail
        elif isinstance(err, list) and err:
            detail = str(err[0])
        elif isinstance(err, str):
            detail = err
    except Exception:
        pass
    return detail


def api_json(url: str, headers: dict | None = None, **kw) -> dict:
    res = api_get(url, headers, **kw)
    if not res.ok:
        raise RuntimeError(f"HTTP {res.status}: {_detail(res)}")
    return res.json()


def api_json_post(url: str, headers: dict | None = None, body=None, **kw) -> dict:
    res = api_post(url, headers, body, **kw)
    if not res.ok:
        raise RuntimeError(f"HTTP {res.status}: {_detail(res)}")
    return res.json()
