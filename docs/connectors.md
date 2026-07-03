# bean connectors

bean ships a small **core** set, always on. Everything else is a **prototype** (bundled, enable by
name) or a **plugin** (a module you drop into `~/.bean/plugins/`). Every connector authenticates
with the user's own credentials, runs locally, and lands documents in the DuckDB catalog; the shared
chunk → embed → hybrid-search pipeline does the rest. Where a service has more than one way in, bean
implements both and defaults to the path an individual can self-serve without an admin.

## Core (always on)

| Connector | Auth (individual-friendly default) | Indexes |
|-----------|------------------------------------|---------|
| **Slack** | user token `xoxp-…` | channels → per-week digests, threads as sections |
| **Google Drive** | gcloud sign-in | Docs as Markdown; owned docs by default |
| **Notion** | integration token `secret_…` | pages + nested blocks |
| **GitHub** | PAT `ghp_…` | issues, PRs (+comments) |
| **Confluence** | Cloud email+token **or** Server/DC PAT | space pages, storage HTML → text |
| **Jira** | Cloud email+token **or** Server/DC PAT | project issues + comments |
| **Zendesk** | subdomain + email + API token | tickets (incremental) + help-center articles |
| **Salesforce** | OAuth token + instance URL | Knowledge articles + Cases (SOQL) |
| **HubSpot** | private-app token | tickets, notes, KB articles |
| **Microsoft 365** | Graph: device-code **or** `az` CLI | OneDrive/SharePoint files, Outlook threads, Teams digests |
| **Discord** | bot token | channels → per-week digests |
| **Local files** | none | folder/file — Markdown/text, Word/ODT/RTF, PDF (+OCR) |

## Prototypes (enable on demand)

`bean plugins list` shows them; `bean plugins enable <name>` turns one on (writes the global config's
`plugins.prototypes`). Modules live in [`bean/prototypes/`](../../bean/prototypes/) and double as
worked examples for authoring your own.

| | | |
|---|---|---|
| **Trackers** | linear · gitlab · bitbucket · asana · trello · clickup · productboard · testrail · canvas | issues/tasks/cards + comments |
| **Wikis / KB** | coda · servicenow · guru · gitbook · outline · slab · bookstack · document360 · mediawiki · wikipedia · drupalwiki · axero | pages/articles (HTML → text) |
| **Support / sales** | intercom · freshdesk · gong · fireflies · highspot · loopio · discourse · xenforo | tickets, conversations, transcripts, forum threads |
| **Mail / chat** | gmail · imap · zulip | threads / per-week digests |
| **Files / tables** | gsheets · dropbox · egnyte · buckets (S3/GCS/Azure) · airtable · sqldb | files → pipeline; rows → docs |
| **Personal / web** | readwise · figma · braintrust · web+sitemap · rss · obsidian · google_site | highlights, design text, pages, feeds, vaults |

Multi-method providers: **Confluence/Jira** pick Cloud Basic when `--email` is given, else Server/DC
Bearer; **Gmail** uses the gcloud token when Google is connected, or IMAP with `--email` + an
app-password; **Microsoft** mints Graph tokens via device-code or `--method az`; **ServiceNow** picks
Basic vs Bearer by `--secret` vs `--token`.

## Authoring a connector (plugin)

For a source bean has no connector for, write one. See
[`authoring-connectors.md`](authoring-connectors.md) for the full guide + a template; the short
version:

A connector is one module exposing four callables plus a `SOURCE`, dropped into `~/.bean/plugins/`:

- `parse_add(item) -> (list_key, value) | None` — claim your refs (a `name:` prefix and/or native
  URL); return `None` for anything else so routing falls through. Never claim a filesystem path.
- `connect(**fields) -> dict` — verify against a cheap identity endpoint, then `save_credential`.
  Secrets live in `~/.bean/credentials/<name>.json` (mode 0600), never in config.
- `connected() -> dict | None` — the stored credential.
- `sync(store, config, *, settings, fetch, full, since_days, log) -> {"changed": [...], "removed": [...]}`
  — all HTTP through the injectable `fetch` seam (`from bean.http import api_json, api_json_post`),
  so it tests offline; `store.upsert(...)` hash-gates re-embedding.

Then `SOURCE = Source("name", "name", "Label", ("lists",), sync, parse_add, auth="name", …)`. The
loader picks up any file exposing `SOURCE` (or `SOURCES` / `register()`).

Non-negotiables: a cheap per-doc `revision_id`, stable doc ids, prune-by-origin (whole-collection and
chat/mail sources never prune), the `fetch` seam (never `import requests`), and an offline test
(fake `fetch` → assert `changed`, re-sync no-op, `parse_add` routing). Enable/promote paths:
`bean plugins enable <name>` for a prototype; move a module into `bean/` + add a row to
`CORE_SOURCES` in `bean/sources.py` to make it core.
