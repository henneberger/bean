# bean

Local search over your Google Docs and Slack, packaged as a Claude Code plugin. No server:
bean pulls with your own credentials, embeds locally, and stores everything in Lance + DuckDB
under `~/.bean/` (one workspace folder per repo).

## Use it from Claude Code

Install the plugin (this repo), then:

```
/bean init                     # connect Google + Slack, pick sources — a guided conversation
/bean sync                     # fetch changes and re-embed them
/bean how do refunds work?     # ask; answers cite doc titles and URLs
/bean status                   # what's connected and indexed
```

## Use it from a terminal

```
python3 scripts/beanw.py init          # or: pip install -e . && bean init
bean auth google                       # browser sign-in via gcloud (no Google Cloud setup)
bean auth slack --token xoxp-…         # user token from your workspace's Slack app
bean add https://docs.google.com/document/d/<id>/edit
bean add "#eng-payments"
bean sync
bean search "how do refunds work?"
```

## How it works

- **Sources.** Google Docs (single docs or whole Drive folders) export as Markdown via the
  Drive API; change detection is per-doc `headRevisionId` with a content hash as the final
  authority. Slack channels are cut into per-channel per-ISO-week digest documents with
  threads as sections; a lookback window (14 days) catches recent edits and deletions, and
  cursors keep re-syncs cheap.
- **Storage.** One DuckDB catalog per workspace (document snapshots, revision history, sync
  cursors) plus a Lance table of chunk embeddings. Workspaces live at
  `~/.bean/<repo-name>-<hash>/`, keyed by the repo you run bean in; credentials are shared per
  user at `~/.bean/credentials/` (mode 0600) and never stored in any repo.
- **Embeddings.** [fastembed](https://github.com/qdrant/fastembed) (ONNX, CPU-only) with
  `BAAI/bge-small-en-v1.5` by default; override with `BEAN_EMBED_MODEL`. The model downloads
  once on first sync.
- **Auth.** Google rides on gcloud: `gcloud auth login --enable-gdrive-access` uses Google's
  own pre-verified OAuth client, so nobody creates a GCP project or consent screen. Slack
  takes a user token from a minimal workspace app (user scopes `channels:history`,
  `channels:read`, `users:read`).

## Limits worth knowing

- Google's Markdown export drops images, drawings, and smart chips; docs that refuse Markdown
  fall back to plain text.
- Slack edits older than the lookback window are missed by design; `bean sync --full`
  re-fetches everything within `--since` days (default 90). New non-Marketplace Slack apps are
  rate-limited hard, so first syncs of busy channels are slow.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python tests/test_bean.py    # offline: fake HTTP, fake embedder, real DuckDB + Lance
```
