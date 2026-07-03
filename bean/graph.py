"""A lightweight relationship graph, derived — not extracted. bean's connectors already carry the
structure: a doc's author, and a container encoded in its stable id (a GitHub issue's `owner/repo`,
a Jira key's `PROJ`, a Slack message's channel). `implied_edges` turns those into edges at sync time
with zero LLM calls — the portable essence of a knowledge graph's metadata implications. Edges power
`bean related <doc>` (same repo/project/channel/author) and graph expansion.

Adding a signal is one line in `_CONTAINER`; a connector can also emit its own edges by returning
them from a future hook, but the derived ones cover the common "what else is about this" question."""

from __future__ import annotations

import re


def _before(sep: str):
    return lambda d: d.split(sep, 1)[0]


# source -> (relationship, container-id extractor from the doc_id). Only the core sources whose id
# encodes a meaningful container appear; everything else contributes just the authored_by edge.
_CONTAINER: dict = {
    "github": ("in_repo", lambda d: re.split(r"[#:]", d, 1)[0]),      # owner/repo#12 / owner/repo:path
    "jira": ("in_project", lambda d: d.rsplit("-", 1)[0]),           # PROJ-123 -> PROJ
    "slack": ("in_channel", _before("/")),                           # channel/<ts>
    "discord": ("in_channel", _before("/")),                         # channel/<message-id>
}


def implied_edges(doc) -> list[dict]:
    """Edges {rel, dst_kind, dst} derived from a stored Doc's metadata + id. Purely deterministic."""
    edges: list[dict] = []
    if getattr(doc, "author", None):
        edges.append({"rel": "authored_by", "dst_kind": "person", "dst": str(doc.author)})
    rule = _CONTAINER.get(doc.source)
    if rule:
        rel, extract = rule
        try:
            container = extract(doc.doc_id)
        except Exception:
            container = None
        if container and container != doc.doc_id:
            edges.append({"rel": rel, "dst_kind": "container", "dst": container})
    return edges
