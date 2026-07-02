---
name: bean
description: Search the user's connected knowledge base — Slack, Google Docs, Notion, GitHub, and local files (including PDFs) — with local hybrid (semantic + keyword) retrieval. Use when the user asks a question that their work docs/messages would answer, wants to connect or sync a source, or types /bean. Also for "what's in my docs about X", "what did we decide in #channel", or reconstructing context across their tools.
version: 0.1.0
user-invocable: true
argument-hint: init | sync | status | config | add <ref> | <question>
allowed-tools: Bash
---

# bean — local knowledge retrieval

You are driving **bean**, a local hybrid search index over the user's Slack, Google Docs, Notion,
GitHub, and local files. It runs entirely on this machine (their credentials, DuckDB + Lance under
`~/.bean/<repo>-<hash>/`). **You** run every bean command yourself via Bash — as:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py <subcommand> …
```

(the wrapper builds the plugin's virtualenv on first use — slower once, instant after.)

**Never ask the user to run a script, a `python3 …` line, or any command themselves.** For auth,
ask them only for the token string and then run the auth command yourself. The one unavoidable
exception is Google's browser sign-in (it must open interactively) — you still launch it; the user
just completes the browser flow.

**Route on what the user typed after `/bean`** (or, if the skill auto-triggered on a question,
treat their message as the question). Empty or a plain question → the retrieval flow below.

## A question — retrieve intelligently, then answer

bean gives you a **toolbox of retrieval commands**. Don't just run one search — decide what
context the question needs, then compose calls. Every command takes `--json`; prefer it. All accept
`--source {slack|gdocs|notion|github|localfiles}` to scope by connector.

- **`search "<q>"`** — hybrid semantic + keyword (the default). Keyword fusion means exact tokens
  (identifiers, error strings, ticket numbers, `#channels`) are found even when not semantically
  close. Flags: `--doc <substr>` (restrict to docs whose id/title contains it), `--expand N` (pull
  N neighbouring chunks around each hit), `--k N`.
- **`recent [--source S] [--doc <substr>]`** — most recently changed docs/messages. For "lately",
  "this week", "the conversation in #product".
- **`thread <ref>` / `doc <ref>`** — a whole Slack thread / week digest / document as one block,
  matched by id or title substring. Use when a snippet isn't enough.
- **`neighbors <chunk-id>`** — the chunks surrounding a specific hit (each hit has an `id`).

### Worked example — "I had a convo in the product channel, what's the impact on my docs?"

1. Pull the conversation: `beanw.py recent --source slack --doc product --json`
2. Read it; extract the concrete topics/decisions/identifiers.
3. Find affected docs: `beanw.py search "<topics>" --source gdocs --source notion --expand 1 --json`
4. If a hit looks central, pull the whole thing (`doc <title>`), then answer, **citing each source
   by title and URL**. If nothing relevant comes back, say so rather than inventing; if the index is
   empty, point the user at `/bean init`.

## `init`

Setup is a conversation; the CLI never prompts. Run `beanw.py init` — it prints what's connected.
Then walk the user through what's missing, one source at a time. **You run every command**; the user
only supplies token strings.

- **Slack** — ask for a **user token** (`xoxp-…`), then run `beanw.py auth slack --token …`. Once
  connected, bean indexes **all channels the account is a member of** automatically — do NOT ask the
  user to add channels one by one. (`bean add #name` exists only to *narrow* to specific channels.)
  How the user gets a token: at https://api.slack.com/apps → **Create New App** → **From scratch** →
  pick the workspace. Open **OAuth & Permissions**, add **User Token Scopes** `channels:history`,
  `channels:read`, `users:read`, then click **Install to `<workspace>`** at the top of that page and
  **Allow**. The **User OAuth Token** (`xoxp-…`) is what they paste to you.
- **Google** — run `beanw.py auth google` yourself; it opens a browser for the user to sign in
  through gcloud (no Google Cloud setup). If gcloud is missing, the command says how to install it.
  Then add specific docs/folders with `beanw.py add <Doc or Drive-folder URL>`.
- **Notion** — ask for an internal-integration token (`secret_…`), run `beanw.py auth notion
  --token …`, then add pages with `beanw.py add <page URL>` (the user shares those pages with the
  integration in Notion).
- **GitHub** — ask for a PAT (`ghp_…`) with repo read, run `beanw.py auth github --token …`, then
  add repos with `beanw.py add owner/name`.
- **Local files** — no auth; add a path with `beanw.py add <file-or-folder>`. A folder is crawled
  recursively for Markdown/text, office docs (Word `.docx`, OpenDocument `.odt`, RTF), and PDFs.

Finish with `beanw.py sync` and a test `search`.

## `sync`

`beanw.py sync [source] [--full] [--since N]` — fetches changes and re-embeds **only what
changed**. The very first sync downloads the embedding model once (a few minutes; warn them). Add a
`source` argument to sync just one connector.

## `status` / `config` / `reembed`

- `beanw.py status [--json]` — connections, tracked sources, index counts, embedding model (warns
  if the index was built with a different model than configured).
- `beanw.py config list` — resolved settings. `config get <path>` / `config set <path> <value>` for
  `embedding.model`, `chunking.lines`, `search.hybrid`, `ocr.backend`, etc. Changing the embedding
  model or chunking prints a reminder to `reembed`.
- `beanw.py reembed` — re-chunk and re-embed everything with current settings. Fetches nothing.

## `add <ref>` / `remove <ref>`

Pass through: `beanw.py add <ref>` then suggest `/bean sync`. Routing detects the source from the
ref, so you don't specify it.
