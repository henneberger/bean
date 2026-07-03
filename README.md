# bean

bean is a local hybrid search over your work knowledge, packaged as a Claude
Code plugin.

You already pay for a Claude plan, so why pay again for API calls? Or hand your docs to yet another
LLM-wrapper SaaS? Not ready to drop $100k on search? bean has no server: it pulls with your own
credentials, embeds on your machine, and stores everything locally.

## Install

In Claude Code, add this repo as a plugin marketplace, then install the plugin. Run the two
commands one at a time (the marketplace add takes a full clone URL, not an `owner/repo` shorthand):

```bash
/plugin marketplace add https://github.com/henneberger/bean.git
/plugin install bean@bean
```

## Use it from Claude Code

```bash
/bean init                     # connect sources — a guided conversation
/bean sync                     # fetch changes and re-embed only what changed
/bean how do refunds work?     # ask; answers cite doc titles and URLs
/bean status                   # what's connected, indexed, and which embedding model
/bean sql "SELECT …"           # read-only SQL over the store (no query = print the schema)
```

`/bean` is not one search. Claude picks from a toolbox (hybrid `search`, `recent`, whole-`thread`
or `doc` pull, graph `related`, `neighbors`) and composes them. Ask *"I had a convo in the product
channel, what's the impact on my docs?"* and it grabs the recent Slack conversation and pulls the
topics out. Then it searches your Google Docs for what those topics touch.

## Try asking

`/bean` takes plain questions — Claude figures out which tools to run. Some things to try:

- **"what's new this week?"** Recent activity across every source, newest first.
- **"where did I write about WAL?"** Finds the doc even if you called it a "write-ahead log"
  elsewhere. Exact terms and identifiers land too.
- **"what did we decide about pricing in #product?"** Pulls the Slack thread and summarizes.
- **"who last touched the billing runbook, and when?"** Author and recency come back with the hit.
- **"find ticket ZQ-9001"** An identifier lands its chunk even with nothing semantically near it.
- **"what else relates to the launch doc?"** Graph hop to the same project/channel/author.
- **"summarize the deploy thread and link the doc it references."** Multi-step: thread, then search, then cite.
- **"what changed since Monday?"** Date-filtered recency (`--since`).

Under the hood these map to `search` (with `--variant`, `--author`, `--since`, `--before`), `recent`,
`thread` / `doc`, and `related`. Claude runs them for you and cites every source by title and URL.

## Connectors

bean ships **10 core connectors**, always on:

| Source | Auth | What it indexes |
|--------|------|-----------------|
| **Slack** | user token (`xoxp-…`) | channels, cut into per-week digests with threads as sections |
| **Google Drive** | gcloud sign-in | Docs, PDFs (extracted), and comments (each comment its own author-attributed entry); whole Drive folders |
| **GitHub** | personal access token | issues and pull requests (body + comments) |
| **Confluence** | Cloud (email + API token) or Server/DC (PAT) | space pages (storage HTML → text) |
| **Jira** | Cloud (email + API token) or Server/DC (PAT) | project issues + comments |
| **Zendesk** | subdomain + email + API token | tickets + help-center articles |
| **Salesforce** | OAuth token + instance URL | Knowledge articles + Cases |
| **HubSpot** | private-app token | tickets, notes, and knowledge-base articles |
| **Microsoft 365** | device-code or `az` CLI | OneDrive/SharePoint files, Outlook threads, Teams week-digests |
| **Discord** | bot token | channels, cut into per-week digests like Slack |
| **Local files** | none | a folder (crawled recursively) or file — Markdown/text, office docs (**Word**, OpenDocument, RTF, **PowerPoint**, **Excel**), **HTML**, and **PDF** |

Where a service offers more than one way in, bean supports both. It prefers the path an individual
can set up without an admin: Atlassian Cloud tokens or Server PATs, Microsoft device-code or `az`.

### More connectors: drop-in plugins

**Need a source bean doesn't have?** Author a connector — a single offline-testable module dropped
into `~/.bean/plugins/`, live with no core edits. [`docs/authoring-connectors.md`](docs/authoring-connectors.md)
walks Claude through the contract, helpers, a test recipe, and a template; the 10 core connectors in
[`bean/connectors/`](bean/connectors/) are worked examples across every API shape. `bean plugins list` shows what's loaded.

### Global vs local scope

A connector is **global** or **local**. Global connectors (your Slack, personal Google Drive, Gmail)
index once into a shared `~/.bean/_global/` store and are searchable from *every* repo. Local
connectors (a GitHub project, this repo's files) live in the per-repo workspace. Search unions both,
so from any repo you see that repo's local sources plus everything global. Credentials are always
shared per-user; scope only governs where the *tracked items + index* live.

```bash
bean scope                       # show each connector's scope
bean scope github local          # this repo only
bean scope slack global          # all repos
```

Tracked refs (repos, channels, folders) go into the source's config file lists — `bean init` prints
each source's config path and list names.

Changing scope moves the connector's config and purges its old index, so run `bean sync` afterward.

## Hybrid search

Every query runs two rankings and fuses them with **weighted** reciprocal rank fusion:

- **Vectors** (Lance) for meaning: *"how are customers billed"* finds the billing doc that never
  says "billed".
- **Keywords** (DuckDB) for exactness: an identifier like `ZQ-9001`, an error string, or a
  `#channel` lands its chunk even when nothing is semantically near it.

Fusion is tunable. Pass extra `--variant` queries (a paraphrase plus the identifiers you spotted)
and they all fuse. `auto_weight` leans keyword for identifier queries and vector for questions.
`recency_decay` biases toward recently changed docs. Adjacent chunks **merge into sections**. An
optional local cross-encoder **reranker** (`search.rerank.enabled`, fastembed, no API) polishes the
top results. Filter by `--author` / `--since` / `--before`, or widen from a doc to its graph
neighbourhood with `bean related <ref>` (same repo/project/channel/author). Turn fusion off
globally with `config set search.hybrid false`.

## Configuration

Settings resolve in three layers, later wins: built-in defaults ← global `~/.bean/config.json` ←
a repo's own `settings` block. Nothing is an environment variable; secrets never live here (tokens
stay in `~/.bean/credentials/`, mode 0600).

```bash
/bean config list                              # the full resolved config
/bean config get search.recency_decay          # one value
/bean config set embedding.backend fastembed     # switch to the higher-accuracy ONNX embedder
/bean config set search.rerank.enabled true
/bean sync --rebuild                            # re-fetch + re-embed to apply a model/chunk change
```

Every leaf below is settable with `config set <path> <value>` (values coerce to the default's type).
Changing an **index-shape** knob (embedding model, any `chunking.*`, enabling `rerank`) needs a
`bean sync --rebuild`. `status` warns if the index was built with a different embedding model than
configured.

| Path | Default | What it does |
|------|---------|--------------|
| `embedding.backend` | `model2vec` | `model2vec` (fast static/CPU embedder) or `fastembed` (ONNX transformer, higher accuracy) (⟳ sync --rebuild) |
| `embedding.model` | `minishlab/potion-retrieval-32M` | model for the backend; for fastembed use e.g. `BAAI/bge-small-en-v1.5` (⟳ sync --rebuild) |
| `embedding.plugin` | `null` | path/import path to a `.py` exposing `embed(texts)` (and optional `embed_query`); overrides backend/model — any library/API that returns vectors (⟳ sync --rebuild) |
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
(e.g. `bean config set slack.chunking.lines 15` or `gdocs.chunking.max_chars 1500`). A source's
effective chunking is the global `chunking` block with its own `chunking` sub-block merged on top.
Slack ships smaller defaults (`slack.chunking` = lines 15, overlap 3, max_chars 1000, min_chars 20)
since chat is short.

`lookback_days` is a one-time choice: `/bean init` prompts for it per source and it bounds only the
**first** sync's backfill. After that each source tracks a cursor and pulls just what's new, so you
never re-scan a window on every sync. `sync --rebuild` ignores the cursor to re-pull within `--since`.

**The embedder is pluggable.** The default `model2vec` backend (`minishlab/potion-retrieval-32M`) is
a static CPU embedder that runs ~100× faster than the old fastembed default. Keyword fusion and
refusable results absorb the small accuracy gap. Switch
`embedding.backend` to `fastembed` for an ONNX transformer (e.g. `BAAI/bge-small-en-v1.5`), or point
`embedding.plugin` at a `.py` exposing `embed(texts)` to bring any library or API. The model
downloads automatically the first time you sync or search (not at setup), and bean caches it after.

## PDF parsing

bean reads PDFs in local folders and PDF files stored in Google Drive. Both go through the same
extractor and honor the `ocr.backend` setting below. Born-digital PDFs use embedded text (pymupdf, a
base dependency). For scans, handwriting, or complex layouts, set `ocr.backend` to `unlimited-ocr` and
bean runs pages through [baidu/Unlimited-OCR](https://github.com/baidu/Unlimited-OCR), a
vision-language OCR model. You install nothing: bean provisions the OCR toolchain (torch,
transformers) into its own venv the first time OCR runs, the same way the embedding model downloads
itself. It runs on CUDA, Apple MPS, or CPU, whichever the machine has. The default `auto` backend
takes embedded text where it exists and OCRs only the pages that have none. OCR stays opt-in because
it's slow: Unlimited-OCR is high quality but ~40s/page on CPU.

## Indexing speed

Everything runs locally on CPU, so the first sync of a big backlog takes real time. Rough numbers on
a 2024 laptop (Apple M3 Pro, no GPU), with the default `model2vec` embedder:

| Work | Throughput | So a first sync of… |
|------|-----------|---------------------|
| **Text/office docs** (Slack, Docs, wikis, Markdown, `.docx`/`.pptx`/`.xlsx`, comments) | **~70–110 docs/sec** (≈300k/hour) | 50,000 docs ≈ **8–12 min** |
| **Born-digital PDFs** (embedded text, pymupdf, the default) | **~350 pages/sec** | basically instant; a 300-page PDF ≈ 1 sec |
| **Scanned PDFs** (`ocr.backend = unlimited-ocr`, opt-in) | **~40 sec/page** (~1.5 pages/min) | 700 scanned pages ≈ **8 hours** |

**Scanned PDFs are the slow path.** With OCR on, plan on ~40 seconds per page and **leave the laptop
running overnight**. A few hundred pages is an evening. A few thousand is a couple of nights. Sync is
resumable, so an interrupted run picks up where it left off. (One-time downloads on first use,
excluded above: the embedding model ~30 MB, and the OCR model ~6 GB the first time you enable it.)

## How it works

- **Sources.** Each connector has a cheap change signal (a revision id, an `updated_at`, a git
  blob sha, or a file mtime), with the content hash as the final authority. `sync` re-embeds only
  what changed; deletions revoke their vectors. Sync is resumable: the embed phase
  checkpoints per document (oldest first), so an interrupted run picks up where it left off without
  re-embedding what's done or skipping anything.
- **Storage.** One DuckDB catalog per workspace holds document snapshots, revision history, sync
  cursors, and the relationship edges. A Lance table alongside it holds the chunks (text + vectors)
  as the single copy. Nothing mirrors that chunk data: keyword search, neighbours, and section-merge
  run as DuckDB SQL **directly over the Lance dataset** (register + query). DuckDB stays the
  relational engine; the chunks live once. Workspaces live at `~/.bean/<repo-name>-<hash>/`
  (global connectors share `~/.bean/_global/`). Credentials follow scope: a **global** connector's
  is shared at `~/.bean/credentials/`. A **local** connector's lives in that repo's workspace (so a
  different GitHub token per project works), with the shared dir as a fallback. All mode 0600,
  never inside a repo. `bean sql "SELECT …"` runs read-only queries (SELECT/WITH) straight over this
  store (tables `documents`, `edges`, `state`, and the Lance `_chunks` dataset) for structured
  questions like counts by author. `bean sql` with no query prints the schema; `--global` targets
  the shared store.
- **Auth.** Google rides on gcloud's own pre-verified OAuth client, so nobody sets up a GCP
  project. Slack and GitHub take a token you paste once.

## Limits worth knowing

- Google's Markdown export drops images, drawings, and smart chips; docs that refuse Markdown fall
  back to plain text.
- After the first sync, Slack/Discord continue from a per-channel cursor and re-render the current
  week, so edits to that week land but older-week edits don't (by design). `sync --rebuild`
  re-fetches everything within `--since` days (default 90).
- GitHub syncs issues/PRs incrementally by `updated_at`; a removed repo prunes everything under it.
