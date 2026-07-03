# bean

Bean is a local hybrid search over your work knowledge, packaged as a Claude
Code plugin.

You already pay for a Claude plan, why pay again for API calls? Or hand your docs to yet another
LLM-wrapper SaaS? Not ready to drop $100k on search? Use Bean, it has no server: bean pulls with your own credentials, embeds on your machine, and stores everything locally.

## Install

In Claude Code, add this repo as a plugin marketplace and install it:

```
/plugin marketplace add henneberger/bean
/plugin install bean@bean
```

## Use it from Claude Code

```
/bean init                     # connect sources — a guided conversation
/bean sync                     # fetch changes and re-embed only what changed
/bean how do refunds work?     # ask; answers cite doc titles and URLs
/bean status                   # what's connected, indexed, and which embedding model
```

`/bean` is not one search. Claude picks from a toolbox — hybrid `search`, `recent`, whole-`thread`
or `doc` pull, graph `related`, `neighbors` — and composes them. Ask *"I had a convo in the product
channel, what's the impact on my docs?"* and it grabs the recent Slack conversation, pulls the
topics out, then searches your Google Docs for what those topics touch.

## Try asking

`/bean` takes plain questions — Claude figures out which tools to run. Some things to try:

- **"what's new this week?"** — recent activity across every source, newest first.
- **"where did I write about WAL?"** — finds the doc even if you called it a "write-ahead log"
  elsewhere; exact terms and identifiers land too.
- **"what did we decide about pricing in #product?"** — pulls the Slack thread and summarizes.
- **"who last touched the billing runbook, and when?"** — author + recency come back with the hit.
- **"find ticket ZQ-9001"** — an identifier lands its chunk even with nothing semantically near it.
- **"what else relates to the launch doc?"** — graph hop to the same project/channel/author.
- **"summarize the deploy thread and link the doc it references."** — multi-step: thread → search → cite.
- **"what changed since Monday?"** — date-filtered recency (`--since`).

Under the hood these map to `search` (with `--variant`, `--author`, `--since`, `--before`), `recent`,
`thread` / `doc`, and `related`; Claude runs them for you and cites every source by title and URL.

## Connectors

bean ships **11 core connectors**, always on:

| Source | Auth | What it indexes |
|--------|------|-----------------|
| **Slack** | user token (`xoxp-…`) | channels, cut into per-week digests with threads as sections |
| **Google Drive** | gcloud sign-in | Docs as Markdown and PDFs (extracted); whole Drive folders |
| **GitHub** | personal access token | issues and pull requests (body + comments) |
| **Confluence** | Cloud (email + API token) or Server/DC (PAT) | space pages (storage HTML → text) |
| **Jira** | Cloud (email + API token) or Server/DC (PAT) | project issues + comments |
| **Zendesk** | subdomain + email + API token | tickets + help-center articles |
| **Salesforce** | OAuth token + instance URL | Knowledge articles + Cases |
| **HubSpot** | private-app token | tickets, notes, and knowledge-base articles |
| **Microsoft 365** | device-code or `az` CLI | OneDrive/SharePoint files, Outlook threads, Teams week-digests |
| **Discord** | bot token | channels, cut into per-week digests like Slack |
| **Local files** | none | a folder (crawled recursively) or file — Markdown/text, office docs (**Word**, OpenDocument, RTF), and **PDF** |

Where a service offers more than one way in, bean supports both and prefers the path an individual
can set up without an admin (Atlassian Cloud tokens or Server PATs; Microsoft device-code or `az`).

### More connectors: drop-in plugins

**Need a source bean doesn't have?** Author a connector — a single offline-testable module dropped
into `~/.bean/plugins/`, live with no core edits. [`docs/authoring-connectors.md`](docs/authoring-connectors.md)
walks Claude through the contract, helpers, a test recipe, and a template; the 12 core connectors in
[`bean/`](bean/) are worked examples across every API shape. `bean plugins list` shows what's loaded.
See also [docs/connectors.md](docs/connectors.md).

### Global vs local scope

A connector is **global** or **local**. Global connectors (your Slack, personal Google Drive, Gmail)
index once into a shared `~/.bean/_global/` store and are searchable from *every* repo. Local
connectors (a GitHub project, this repo's files) live in the per-repo workspace. Search unions both,
so from any repo you see that repo's local sources plus everything global. Credentials are always
shared per-user; scope only governs where the *tracked items + index* live.

```
bean scope                       # show each connector's scope
bean scope github local          # this repo only
bean scope slack global          # all repos
```

Tracked refs (repos, channels, folders) go into the source's config file lists — `bean init` prints
each source's config path and list names.

Changing scope moves the connector's config and purges its old index, so run `bean sync` afterward.

## Hybrid search

Every query runs two rankings and fuses them with **weighted** reciprocal rank fusion:

- **Vectors** (Lance) for meaning — *"how are customers billed"* finds the billing doc that never
  says "billed".
- **Keywords** (DuckDB) for exactness — an identifier like `ZQ-9001`, an error string, or a
  `#channel` lands its chunk even when nothing is semantically near it.

Fusion is tunable: pass extra `--variant` queries (a paraphrase plus the identifiers you spotted)
and they all fuse; `auto_weight` leans keyword for identifier queries and vector for questions;
`recency_decay` biases toward recently-changed docs; adjacent chunks **merge into sections**; and an
optional local cross-encoder **reranker** (`search.rerank.enabled`, fastembed — no API) polishes the
top results. Filter by `--author` / `--since` / `--before`, or widen from a doc to its graph
neighbourhood with `bean related <ref>` (same repo/project/channel/author). Turn fusion off
globally with `config set search.hybrid false`.

## Configuration

Settings resolve in three layers, later wins: built-in defaults ← global `~/.bean/config.json` ←
a repo's own `settings` block. Nothing is an environment variable; secrets never live here (tokens
stay in `~/.bean/credentials/`, mode 0600).

```
/bean config list                              # the full resolved config
/bean config get search.recency_decay          # one value
/bean config set embedding.model BAAI/bge-base-en-v1.5
/bean config set search.rerank.enabled true
/bean sync --rebuild                            # re-fetch + re-embed to apply a model/chunk change
```

Every leaf below is settable with `config set <path> <value>` (values coerce to the default's type).
Changing an **index-shape** knob (embedding model, any `chunking.*`, enabling `rerank`) needs a
`bean sync --rebuild`; `status` warns if the index was built with a different embedding model than
configured.

| Path | Default | What it does |
|------|---------|--------------|
| `embedding.model` | `BAAI/bge-small-en-v1.5` | any fastembed model (⟳ sync --rebuild) |
| `embedding.batch_size` | `64` | embed batch size |
| `chunking.lines` / `overlap` | `40` / `8` | window height and shared lines (⟳) |
| `chunking.max_chars` / `min_chars` | `2000` / `40` | per-chunk cap; drop windows shorter than this (⟳) |
| `chunking.title_prefix` | `true` | embed the doc title into each chunk for recall (⟳) |
| `chunking.large_chunks` / `large_chunk_ratio` | `false` / `4` | coarse doc-level vectors for broad questions (⟳) |
| `search.hybrid` | `true` | fuse vector + keyword (false = vector only) |
| `search.k` | `8` | results returned |
| `search.rrf_k` / `keyword_pool` | `60` / `200` | RRF constant; keyword candidate pool |
| `search.expand` | `1` | neighbouring chunks pulled around each hit |
| `search.vector_weight` / `keyword_weight` | `1.0` / `1.0` | fusion weights per ranking |
| `search.auto_weight` | `true` | lean keyword for identifier queries, vector for questions |
| `search.recency_decay` / `recency_floor` | `0.0` / `0.75` | time-bias toward newer docs (0 = off) |
| `search.merge_sections` | `true` | coalesce adjacent same-doc chunks into one section |
| `search.rerank.enabled` / `model` / `pool` | `false` / `Xenova/ms-marco-MiniLM-L-6-v2` / `40` | local cross-encoder rerank, no API (⟳ to warm) |
| `graph.enabled` | `true` | build the `related` edge index during sync |
| `sync.stale_days` | `7` | warn (never auto-sync) when the index is older than this; 0 = off |
| `ocr.backend` / `model` / `dpi` | `auto` / `baidu/Unlimited-OCR` / `200` | PDF text backend (below) |
| `slack.lookback_days` | `14` | initial backfill: how far the **first** Slack sync reaches back; later syncs continue from the cursor |
| `discord.lookback_days` | `14` | initial backfill for Discord channel digests (first sync only) |
| `gdocs.lookback_days` | `30` | initial backfill for auto-indexed Drive files; later syncs discover only files changed since (cursor). 0 = all |

**Per-source chunking.** Any `chunking.*` leaf can be overridden per source as `<source>.chunking.*`
(e.g. `bean config set slack.chunking.lines 15` or `notion.chunking.max_chars 1500`). A source's
effective chunking is the global `chunking` block with its own `chunking` sub-block merged on top.
Slack ships smaller defaults (`slack.chunking` = lines 15, overlap 3, max_chars 1000, min_chars 20)
since chat is short.

`lookback_days` is a one-time choice: `/bean init` prompts for it per source and it bounds only the
**first** sync's backfill. After that each source tracks a cursor and pulls just what's new, so you
never re-scan a window on every sync. `sync --rebuild` ignores the cursor to re-pull within `--since`.

The embedding model downloads automatically the first time you actually sync or search — not at
setup — and is cached afterward.

## PDF parsing

bean reads PDFs in local folders and native PDFs in Google Drive — both go through the same
extractor and honor the `ocr.backend` setting below. Born-digital PDFs use embedded text (pymupdf, a
base dependency). For scans, handwriting, or complex layouts, set `ocr.backend` to `unlimited-ocr` and
bean runs pages through [baidu/Unlimited-OCR](https://github.com/baidu/Unlimited-OCR), a
vision-language OCR model. You install nothing: bean provisions the OCR toolchain (torch,
transformers) into its own venv the first time OCR runs, the same way the embedding model
downloads itself on first use, and runs on CUDA, Apple MPS, or CPU, whichever the machine has.
The default `auto` backend takes embedded text where it exists and OCRs only the pages that have
none.

## How it works

- **Sources.** Each connector has a cheap change signal — a revision id, an `updated_at`, a git
  blob sha, or a file mtime — with the content hash as the final authority. `sync` re-embeds only
  what actually changed; deletions revoke their vectors.
- **Storage.** One DuckDB catalog per workspace holds document snapshots, revision history, sync
  cursors, and the relationship edges; a Lance table alongside it holds the chunks (text + vectors)
  as the single copy. There is no chunk mirror — keyword search, neighbours, and section-merge run
  as DuckDB SQL **directly over the Lance dataset** (register + query), so DuckDB stays the
  relational engine while chunk data lives once. Workspaces live at `~/.bean/<repo-name>-<hash>/`
  (global connectors share `~/.bean/_global/`). Credentials follow scope: a **global** connector's
  is shared at `~/.bean/credentials/`; a **local** connector's lives in that repo's workspace (so a
  different GitHub token per project just works), with the shared dir as a fallback. All mode 0600,
  never inside a repo.
- **Auth.** Google rides on gcloud's own pre-verified OAuth client, so nobody sets up a GCP
  project. Slack and GitHub take a token you paste once.

## Limits worth knowing

- Google's Markdown export drops images, drawings, and smart chips; docs that refuse Markdown fall
  back to plain text.
- After the first sync, Slack/Discord continue from a per-channel cursor and re-render the current
  week, so edits to that week land but edits to older weeks are missed by design; `sync --rebuild`
  re-fetches everything within `--since` days (default 90).
- GitHub syncs issues/PRs incrementally by `updated_at`; a removed repo prunes everything under it.
