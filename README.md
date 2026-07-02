# bean

Bean is a local hybrid search over your work knowledge, packaged as a Claude
Code plugin.

You already pay for a Claude plan, why pay again for API calls? Or hand your docs to yet another
LLM-wrapper SaaS? Not ready to drop $100k on search? Use Bean, it has no server: bean pulls with your own credentials, embeds on your machine, and stores everything locally.

## Use it from Claude Code

Install the plugin (this repo), then:

```
/bean init                     # connect sources — a guided conversation
/bean sync                     # fetch changes and re-embed only what changed
/bean how do refunds work?     # ask; answers cite doc titles and URLs
/bean status                   # what's connected, indexed, and which embedding model
```

`/bean` is not one search. Claude picks from a toolbox — hybrid `search`, `recent`, whole-`thread`
or `doc` pull, `neighbors` — and composes them. Ask *"I had a convo in the product channel, what's
the impact on my docs?"* and it grabs the recent Slack conversation, pulls the topics out, then
searches your Google Docs and Notion for what those topics touch.

## Connectors

| Source | Auth | What it indexes |
|--------|------|-----------------|
bean ships **12 core connectors**, always on:

| Source | Auth | What it indexes |
|--------|------|-----------------|
| **Slack** | user token (`xoxp-…`) | channels, cut into per-week digests with threads as sections |
| **Google Drive** | gcloud sign-in | Docs as Markdown; whole Drive folders |
| **Notion** | integration token | pages and their nested blocks |
| **GitHub** | personal access token | issues, pull requests (body + comments), and repo Markdown |
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

### More connectors: prototypes + plugins

Another **~45 connectors** — Linear, GitLab, Bitbucket, Gmail (gcloud/IMAP), Google Sheets, Asana,
Trello, ClickUp, Zulip, Intercom, Freshdesk, ServiceNow, Coda, Notion-style wikis (Guru, GitBook,
Outline, Slab, BookStack, Document360, MediaWiki, Wikipedia), Gong, Fireflies, Figma, Readwise,
Airtable, Dropbox, Egnyte, cloud storage (S3/GCS/Azure), web/sitemap, RSS, SQL, Obsidian, and more —
ship as **prototypes**. They're off by default; turn one on with:

```
bean plugins list
bean plugins enable linear
```

**Need a source bean doesn't have?** Author a connector — a single offline-testable module dropped
into `~/.bean/plugins/`. The **`bean-connector` skill** walks Claude through the contract, helpers,
a test recipe, and a template; the ~45 prototypes in [`bean/prototypes/`](bean/prototypes/) are
worked examples across every API shape. See [docs/connectors.md](docs/connectors.md).

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
neighbourhood with `bean related <ref>` (same repo/project/channel/author). Turn fusion off per
query with `--no-hybrid`, or globally with `config set search.hybrid false`.

## Configuration

Global settings live in
`~/.bean/config.json`, and any repo can override them in its own `settings` block.

```
/bean config list                              # resolved settings
/bean config set embedding.model BAAI/bge-base-en-v1.5
/bean config set chunking.lines 60
/bean config set ocr.backend unlimited-ocr
/bean reembed                                   # apply a model/chunk change to existing docs
```

- **Embedding model** — any fastembed model. Change it and `reembed`; `status` warns when the
  index was built with a different model than the one configured.
- **Chunking** — window height, overlap, size caps, `title_prefix` (embed the doc title into each
  chunk), and `large_chunks` (coarse doc-level vectors). Changing these needs a `reembed`.
- **Search** — hybrid on/off, result count, fusion constant, context expansion, `vector_weight` /
  `keyword_weight` / `auto_weight`, `recency_decay` / `recency_floor`, `merge_sections`, and a local
  `rerank` cross-encoder.
- **Graph** — `graph.enabled` builds the `related` edge index during sync.
- **OCR** — the PDF backend (below).

The embedding model downloads automatically the first time you actually sync or search — not at
setup — and is cached afterward.

## PDF parsing

bean reads PDFs in local folders. Born-digital PDFs use embedded text (pymupdf, a base
dependency). For scans, handwriting, or complex layouts, set `ocr.backend` to `unlimited-ocr` and
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
  cursors, and a chunk mirror that powers keyword search and the recent/thread/neighbor tools. A
  Lance table alongside it holds the chunk vectors. Workspaces live at `~/.bean/<repo-name>-<hash>/`,
  keyed by the repo you run bean in. Credentials stay per user at `~/.bean/credentials/` (mode
  0600), never inside a repo.
- **Auth.** Google rides on gcloud's own pre-verified OAuth client, so nobody sets up a GCP
  project. Slack, Notion, and GitHub take a token you paste once.

## Limits worth knowing

- Google's Markdown export drops images, drawings, and smart chips; docs that refuse Markdown fall
  back to plain text.
- Slack edits older than the lookback window (14 days) are missed by design; `sync --full`
  re-fetches everything within `--since` days (default 90).
- Notion database *queries* need a POST endpoint bean doesn't use yet — add the individual pages
  for now (see the connector backlog).
- GitHub syncs issues/PRs incrementally by `updated_at`; a removed repo prunes everything under it.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python tests/test_bean.py    # offline: fake HTTP, fake embedder, real DuckDB + Lance
```

The test suite fakes every network call and the embedder, so it runs offline and touches no model.
`set_bean_home()` points all state at a temp dir — there is no `BEAN_HOME` environment variable.
