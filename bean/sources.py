"""The connector registry. Each Source gives the sync pipeline a uniform surface — where its
config lives, whether it needs auth, how `bean add` routes a reference to it, and one `sync()`
callable with the same signature — so `sync.py` and the CLI never special-case a provider.

bean ships a small, verified **core** set. Everything else is a user plugin (a drop-in module under
~/.bean/plugins/), discovered at import by `plugins.py`. `localfiles` is always registered LAST so it
stays the path catch-all. Adding a connector is: write its module, then either append a core row here
or ship a plugin — see `docs/authoring-connectors.md`."""

from __future__ import annotations

from dataclasses import dataclass

from .connectors import (confluence, discord, gdocs, github, hubspot, jira, localfiles, microsoft,
                         salesforce, slack, zendesk)
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
    auth_help: str = ""         # extra `bean auth` flags this provider needs, for the usage line
    connect: object = None      # callable(**fields) for `bean auth <key>`
    connected: object = None    # callable() -> dict|None
    interactive_auth: bool = False  # browser / device-code flow (no token on the command line)
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


# Sources with a lookback window (first-sync reach-back; smart cursor after). Default days; the
# resolved `<source>.lookback_days` setting overrides. Drives the setup prompt (init --json) too.
LOOKBACK_DEFAULTS = {"slack": 14, "discord": 14, "gdocs": 30}


def _lookback(key, cfg, settings):
    """Connector-level lookback: the source's tracked-config block wins, then the resolved
    `<key>.lookback_days` setting, then the built-in default."""
    return cfg.get("lookback_days", settings.get(key, {}).get("lookback_days", LOOKBACK_DEFAULTS[key]))


# -- adapters (normalize each module's native signature to the registry contract) ---------------
def _gdocs_sync(store, cfg, *, settings, fetch, full, since_days, log):
    return gdocs.sync(store, cfg, fetch=fetch, full=full, lookback_days=_lookback("gdocs", cfg, settings),
                      ocr=settings.get("ocr", {}), log=log)


def _discord_sync(store, cfg, *, settings, fetch, full, since_days, log):
    cfg = {**cfg, "lookback_days": _lookback("discord", cfg, settings)}
    return discord.sync(store, cfg, settings=settings, fetch=fetch, full=full,
                        since_days=since_days, log=log)


def _slack_sync(store, cfg, *, settings, fetch, full, since_days, log):
    cred = load_credential("slack")
    if not cred:
        raise RuntimeError("not connected — run `bean auth slack --token xoxp-…`.")
    cfg = {**cfg, "lookback_days": _lookback("slack", cfg, settings)}
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


# -- core connectors (the small, verified, always-on set) ---------------------------------------
CORE_SOURCES: list[Source] = [
    Source("slack", "slack", "Slack", ("channels",), _slack_sync, _slack_parse,
           auth="slack", add_help="#channel-name (optional; all your channels sync by default)",
           auth_help="--token xoxp-…",
           connect=slack.connect, connected=slack.connected, always_when_connected=True),
    Source("gdocs", "google", "Google Drive", ("docs", "folders"), _gdocs_sync, _gdocs_parse,
           auth="google", add_help="a Google Doc or Drive folder URL (optional; docs you own sync by default)",
           auth_help="rides gcloud sign-in (no token)",
           connect=gdocs.connect, connected=gdocs.connected, interactive_auth=True,
           always_when_connected=True),
    Source("github", "github", "GitHub", ("repos",), github.sync, github.parse_add,
           auth="github", add_help="a repo as owner/name or a github.com URL", auth_help="--token ghp_…",
           connect=github.connect, connected=github.connected),
    Source("confluence", "confluence", "Confluence", ("spaces", "pages"),
           confluence.sync, confluence.parse_add, auth="confluence",
           add_help="a Confluence page/space URL, confluence:SPACEKEY, or confluence:page:ID",
           auth_help="--url https://you.atlassian.net/wiki --token … [--email … for Cloud]",
           connect=confluence.connect, connected=confluence.connected),
    Source("jira", "jira", "Jira", ("projects",), jira.sync, jira.parse_add, auth="jira",
           add_help="jira:PROJ or a /browse/PROJ-123 URL",
           auth_help="--url https://you.atlassian.net --token … [--email … for Cloud]",
           connect=jira.connect, connected=jira.connected),
    Source("zendesk", "zendesk", "Zendesk", ("include",), zendesk.sync, zendesk.parse_add,
           auth="zendesk", add_help="zendesk:tickets or zendesk:articles (optional; both sync by default)",
           auth_help="--subdomain acme --email you@acme.com --token <api-token>",
           connect=zendesk.connect, connected=zendesk.connected, always_when_connected=True),
    Source("salesforce", "salesforce", "Salesforce", ("objects",), salesforce.sync,
           salesforce.parse_add, auth="salesforce",
           add_help="salesforce:articles or salesforce:cases (optional; both sync by default)",
           auth_help="--token <access-token> --url https://you.my.salesforce.com",
           connect=salesforce.connect, connected=salesforce.connected, always_when_connected=True),
    Source("hubspot", "hubspot", "HubSpot", ("include",), hubspot.sync, hubspot.parse_add,
           auth="hubspot", add_help="hubspot:tickets, hubspot:notes, or hubspot:kb",
           auth_help="--token <private-app-token>",
           connect=hubspot.connect, connected=hubspot.connected, always_when_connected=True),
    Source("microsoft", "microsoft", "Microsoft 365", ("drives", "mail", "teams"),
           microsoft.sync, microsoft.parse_add, auth="microsoft",
           add_help="ms:file:<itemId>, ms:mail:inbox, or ms:teams:<teamId>/<channelId>",
           auth_help="(device-code by default, or --method az to reuse the az CLI)",
           connect=microsoft.connect, connected=microsoft.connected, interactive_auth=True),
    Source("discord", "discord", "Discord", ("channels", "guilds"), _discord_sync, discord.parse_add,
           auth="discord", add_help="a discord.com/channels/<guild>/<channel> URL, discord:<channelId>, or discord:guild:<guildId>",
           auth_help="--token <bot-token>",
           connect=discord.connect, connected=discord.connected),
]

# Always LAST — the path catch-all. Kept out of CORE_SOURCES so discovered drop-in plugins
# (which may claim bare URLs) still route before the filesystem fallback.
LOCALFILES = Source("localfiles", "localfiles", "Local files", ("paths",), localfiles.sync,
                    localfiles.parse_add, auth=None, add_help="a file or folder path")


def build_sources() -> list[Source]:
    """Core cloud connectors + discovered drop-in plugins, then localfiles last."""
    from .plugins import discover_sources  # lazy: avoids an import cycle with the registry
    return CORE_SOURCES + discover_sources(Source) + [LOCALFILES]


SOURCES: list[Source] = build_sources()
BY_KEY = {s.key: s for s in SOURCES}
BY_CONFIG_KEY = {s.config_key: s for s in SOURCES}


def reload_sources() -> None:
    """Rebuild the registry after dropping in a plugin (used by tests)."""
    global SOURCES, BY_KEY, BY_CONFIG_KEY
    SOURCES = build_sources()
    BY_KEY = {s.key: s for s in SOURCES}
    BY_CONFIG_KEY = {s.config_key: s for s in SOURCES}


def route_add(item: str):
    """First source that claims `item` → (source, list_key, value). None if nothing matches."""
    for src in SOURCES:
        hit = src.parse_add(item)
        if hit:
            return src, hit[0], hit[1]
    return None
