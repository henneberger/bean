"""Stdlib HTML → readable text, shared by every source that stores its bodies as HTML (Confluence
storage format, Zendesk articles, Salesforce/HubSpot KB, Microsoft Graph message bodies). No
lxml/bs4 dependency — bean owns no extra toolchain here, mirroring office.py.

`html_to_text` flattens markup to text with block breaks and light Markdown (headings, list
bullets, links) preserved so chunks stay readable. Connectors hand it a body fragment they got
from an API, not a whole web page, so there is no nav/chrome-stripping pass — just flattening."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# Tags whose entire subtree is never body text, even inside an API-provided fragment.
_DROP = {"script", "style", "head", "noscript"}
_BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "ul", "ol", "blockquote",
          "pre", "figure", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th"}
_HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}


class _Flattener(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._skip_tag: str | None = None
        self.out: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _DROP:
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


def _clean(raw) -> str:
    text = "".join(raw)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(html: str) -> str:
    """Markup → text keeping headings, bullets, links. The caller already handed us a body fragment
    (a wiki page, an article, a message body), so we flatten rather than run page-level readability."""
    if not html:
        return ""
    p = _Flattener()
    try:
        p.feed(html)
    except Exception:
        return _clean(unescape(re.sub(r"<[^>]+>", " ", html)))
    return _clean(p.out)
