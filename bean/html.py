"""Stdlib HTML → readable text/Markdown-ish, shared by every source that stores its bodies as
HTML (Confluence storage format, Zendesk/Intercom articles, Salesforce/ServiceNow KB, arbitrary
web pages). No lxml/bs4 dependency — bean owns no extra toolchain here, mirroring office.py.

`html_to_text` flattens markup to text with block breaks and light Markdown (headings, list
bullets, links) preserved so chunks stay readable. `extract_readable` is a crude readability
pass for full web pages: it drops script/style/nav/header/footer chrome, then flattens the rest."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# Tags whose entire subtree is chrome/noise, never body text.
_DROP = {"script", "style", "head", "noscript", "svg", "nav", "header", "footer", "form",
         "aside", "button", "iframe", "template"}
_BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "ul", "ol", "blockquote",
          "pre", "figure", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th"}
_HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}


class _Flattener(HTMLParser):
    def __init__(self, drop_chrome: bool):
        super().__init__(convert_charrefs=True)
        self._drop_chrome = drop_chrome
        self._skip_depth = 0
        self._skip_tag: str | None = None
        self.out: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        drop = tag in _DROP if self._drop_chrome else tag in {"script", "style", "head", "noscript"}
        if drop:
            self._skip_depth, self._skip_tag = 1, tag
            return
        if tag in _HEADING:
            self.out.append("\n\n" + _HEADING[tag])
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in _BLOCK:
            self.out.append("\n\n" if tag in ("p", "div", "tr", "table", "blockquote", "pre",
                                              "figure", "hr", "section", "article") else "\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag == "a" and self._href and self.out and self.out[-1].strip():
            if self._href.startswith(("http://", "https://")) and self._href not in self.out[-1]:
                self.out.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(data)


def _clean(raw: str) -> str:
    text = "".join(raw)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(html: str) -> str:
    """Markup → text keeping headings, bullets, links. Keeps inline chrome (nav/etc.) since the
    caller already handed us just a body fragment (a wiki page, an article)."""
    if not html:
        return ""
    p = _Flattener(drop_chrome=False)
    try:
        p.feed(html)
    except Exception:
        return _clean(unescape(re.sub(r"<[^>]+>", " ", html)))
    return _clean(p.out)


def extract_readable(html: str) -> tuple[str | None, str]:
    """Full web page → (title, text). Drops nav/header/footer/script chrome — a crude readability
    pass, good enough to make a page searchable without a heavyweight extractor."""
    title = None
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if m:
        title = unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    p = _Flattener(drop_chrome=True)
    try:
        p.feed(html or "")
        body = _clean(p.out)
    except Exception:
        body = _clean(unescape(re.sub(r"<[^>]+>", " ", html or "")))
    return title, body
