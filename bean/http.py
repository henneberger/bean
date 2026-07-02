"""One HTTP wrapper for both sources: 429/5xx retry honoring Retry-After, 401 raises
AuthError for the caller to refresh a token once. `fetch` is injectable so the test suite
runs entirely offline."""

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

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        import json
        return json.loads(self.text)


def _requests_fetch(url: str, headers: dict) -> Response:
    import requests
    r = requests.get(url, headers=headers, timeout=60)
    return Response(status=r.status_code, text=r.text, headers=dict(r.headers))


def api_get(url: str, headers: dict | None = None, *, fetch=None, retries: int = 4,
            sleep=time.sleep) -> Response:
    fetch = fetch or _requests_fetch
    last = None
    for attempt in range(retries + 1):
        res = fetch(url, headers or {})
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


def api_json(url: str, headers: dict | None = None, **kw) -> dict:
    res = api_get(url, headers, **kw)
    if not res.ok:
        detail = res.text[:200]
        try:
            detail = res.json().get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"HTTP {res.status}: {detail}")
    return res.json()
