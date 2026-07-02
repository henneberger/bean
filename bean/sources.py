"""The connector registry. Each Source gives the sync pipeline a uniform surface — where its
config lives, whether it needs auth, how `bean add` routes a reference to it, and one `sync()`
callable with the same signature — so `sync.py` and the CLI never special-case a provider.
Adding a connector is: write its module, then append one Source here."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import gdocs, github, localfiles, notion, slack
from .workspace import load_credential


@dataclass
class Source:
    key: str            # index/source name used in the store and Lance (e.g. "gdocs")
    config_key: str     # top-level key holding this source's config in the workspace file
    label: str
    lists: tuple        # config sub-keys that hold tracked-item lists
    _sync: object
    _parse_add: object
    auth: str | None = None  # credential name; None = no auth (e.g. local files)
    add_help: str = ""
    connect: object = None      # callable(token/…) for `bean auth <key>`
    connected: object = None    # callable() -> dict|None
    interactive_auth: bool = False  # Google's browser flow
    always_when_connected: bool = False  # sync once authed even with no explicit items (Slack: all channels)

    def has_sources(self, cfg: dict) -> bool:
        return any(cfg.get(name) for name in self.lists)

    def is_active(self, cfg: dict) -> bool:
        if self.has_sources(cfg):
            return True
        return bool(self.always_when_connected and self.connected and self.connected())

    def sync(self, store, cfg, **kw):
        return self._sync(store, cfg, **kw)

    def parse_add(self, item: str):
        """-> (list_key, value) this source claims, or None."""
        return self._parse_add(item)


# -- adapters (normalize each module's native signature to the registry contract) ---------------
def _gdocs_sync(store, cfg, *, settings, fetch, full, since_days, log):
    lookback = cfg.get("lookback_days", settings.get("gdocs", {}).get("lookback_days", 30))
    return gdocs.sync(store, cfg, fetch=fetch, full=full, lookback_days=lookback, log=log)


def _slack_sync(store, cfg, *, settings, fetch, full, since_days, log):
    cred = load_credential("slack")
    if not cred:
        raise RuntimeError("not connected — run `bean auth slack --token xoxp-…`.")
    cfg = {**cfg, "lookback_days": cfg.get("lookback_days", settings.get("slack", {}).get("lookback_days", 14))}
    return slack.sync(store, cfg, token=cred["token"], team_url=cred.get("url"),
                      fetch=fetch, full=full, since_days=since_days, log=log)


def _gdocs_parse(item: str):
    ref = gdocs.parse_ref(item)
    if not ref:
        return None
    kind, gid = ref
    return ("folders" if kind == "folder" else "docs", gid)


def _slack_parse(item: str):
    return ("channels", item.lstrip("#")) if item.startswith("#") else None


SOURCES: list[Source] = [
    Source("slack", "slack", "Slack", ("channels",), _slack_sync, _slack_parse,
           auth="slack", add_help="#channel-name (optional; all your channels sync by default)",
           connect=slack.connect, connected=slack.connected, always_when_connected=True),
    Source("gdocs", "google", "Google Docs", ("docs", "folders"), _gdocs_sync, _gdocs_parse,
           auth="google", add_help="a Google Doc or Drive folder URL (optional; docs you own sync by default)",
           connect=gdocs.connect, connected=gdocs.connected, interactive_auth=True,
           always_when_connected=True),
    Source("notion", "notion", "Notion", ("pages",), notion.sync, notion.parse_add,
           auth="notion", add_help="a Notion page URL or id",
           connect=notion.connect, connected=notion.connected),
    Source("github", "github", "GitHub", ("repos",), github.sync, github.parse_add,
           auth="github", add_help="a repo as owner/name or a github.com URL",
           connect=github.connect, connected=github.connected),
    Source("localfiles", "localfiles", "Local files", ("paths",), localfiles.sync,
           localfiles.parse_add, auth=None, add_help="a file or folder path"),
]

BY_KEY = {s.key: s for s in SOURCES}
BY_CONFIG_KEY = {s.config_key: s for s in SOURCES}


def route_add(item: str):
    """First source that claims `item` → (source, list_key, value). None if nothing matches."""
    for src in SOURCES:
        hit = src.parse_add(item)
        if hit:
            return src, hit[0], hit[1]
    return None
