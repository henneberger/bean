---
name: bean
description: Search the user's connected knowledge base — Slack, Google Docs, Notion, GitHub, and local files (including PDFs) — with local hybrid (semantic + keyword) retrieval. Use when the user asks a question that their work docs/messages would answer, wants to connect or sync a source, or types /bean. Also for "what's in my docs about X", "what did we decide in #channel", or reconstructing context across their tools.
version: 0.1.0
user-invocable: true
argument-hint: init | sync | status | plugins | config | <question>
allowed-tools: Bash
---

# bean — local knowledge retrieval

You are driving **bean**, a local hybrid search index over the user's Slack, Google Docs, Notion,
GitHub, and local files. It runs entirely on this machine (their credentials, DuckDB + Lance under
`~/.bean/<repo>-<hash>/`). **You** run every bean command yourself via Bash — as:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <subcommand> …
```

(the wrapper builds the plugin's virtualenv on first use — slower once, instant after.)

**Setup is assistant-guided; the user never has to run anything.** By default, ask the user only
for the token string and set the source up yourself. Two exceptions: interactive sign-ins (Google
via gcloud, Microsoft device-code) open a browser/prompt the user completes; and a **privacy-minded
user may prefer their token never reach you** — in that case hand them the exact `bean auth …`
command (or the credential-file path) to run themselves, and continue once they say it's done.

**Route on what the user typed after `/bean`** (or, if the skill auto-triggered on a question,
treat their message as the question). Empty or a plain question → the retrieval flow below.

## A question — retrieve intelligently, then answer

bean gives you a **toolbox of retrieval commands**. Don't just run one search — decide what
context the question needs, then compose calls. Every command prints human-readable text — read its
output. All accept `--source {slack|gdocs|notion|github|localfiles}` to scope by connector.

- **`search "<q>"`** — hybrid semantic + keyword (the default), fused with weighted RRF. Keyword
  fusion means exact tokens (identifiers, error strings, ticket numbers, `#channels`) are found even
  when not semantically close. Flags:
  - `--variant "<q2>"` (repeatable) — **fuse extra query variants** with the main one. This is your
    lever: pass a paraphrase *and* the raw identifiers you spotted (e.g. main `"how billing works"`
    plus `--variant "ZQ-9001"`); each variant adds a ranking, weighted-RRF merges them. You are the
    query-expansion step — bean doesn't call an LLM for it, you do.
  - `--author <substr>` `--since YYYY-MM-DD` `--before YYYY-MM-DD` — narrow by who/when.
  - `--doc <substr>` (id/title contains), `--expand N` (neighbouring chunks per hit), `--k N`.
- **`recent [--source S] [--doc <substr>] [--author <substr>] [--since …] [--before …]`** — most
  recently changed docs/messages. For "lately", "this week", "what did Ada change".
- **`related <ref>`** — documents one hop away in the graph: same repo/project/channel or same
  author, and directly linked docs. Each hit says *why* (`reason`). Use to widen from one doc to its
  neighbourhood ("what else touches this ticket's project?").
- **`thread <ref>` / `doc <ref>`** — a whole Slack thread / week digest / document as one block,
  matched by id or title substring. Use when a snippet isn't enough.
- **`neighbors <chunk-id>`** — the chunks surrounding a specific hit (each hit has an `id`).

Ranking is config-driven (`config set search.*`): `recency_decay` (time-bias toward newer docs),
`merge_sections` (coalesce adjacent chunks; on by default), `auto_weight` (identifier queries lean
keyword, questions lean vector), and an optional local `rerank.enabled` cross-encoder. Index-shape
knobs `chunking.title_prefix` / `chunking.large_chunks` and enabling the reranker take effect after
a `bean sync --rebuild`.

### Worked example — "I had a convo in the product channel, what's the impact on my docs?"

1. Pull the conversation: `bean.py recent --source slack --doc product`
2. Read it; extract the concrete topics/decisions/identifiers.
3. Find affected docs: `bean.py search "<topics>" --source gdocs --source notion --expand 1`
4. If a hit looks central, pull the whole thing (`doc <title>`), then answer, **citing each source
   by title and URL**. If nothing relevant comes back, say so rather than inventing; if the index is
   empty, point the user at `/bean init`.

## `init`

Setup is a conversation; the CLI never prompts. Run `bean.py init` for a detailed human listing —
one entry per source with its connection status, scope, credential file path + fields, config file
path + the config list names that hold tracked refs (e.g. `slack.[channels]`, `github.[repos]`),
whether it indexes-everything-when-connected, and (for Slack/Discord/Google Drive) the first-sync
lookback. bean ships **12 core connectors** — Slack, Google Drive, Notion, GitHub, Confluence,
Jira, Zendesk, Salesforce, HubSpot, Microsoft 365, Discord, and local files — always on. Read
`bean.py init`'s output and act on it; don't memorize the list.

**Need a source that isn't core?** Author a connector — read
`${CLAUDE_PLUGIN_ROOT}/docs/authoring-connectors.md`, which walks you through writing an
offline-tested plugin dropped into `~/.bean/plugins/`. `bean.py plugins list` shows what's loaded.

**Scope — ask this for every connector you set up.** A connector is either **global** (indexed once,
searchable from every repo — e.g. Slack, your personal Google Drive, Gmail) or **local** (scoped to
the repo you're in — e.g. a GitHub project, this repo's files). Global connectors live in a shared
`~/.bean/_global` index; local ones in the per-repo workspace; search unions both. When connecting a
source, **ask the user "global (all repos) or local (just this repo)?"** and set it with
`bean.py scope <source> global|local`. Each source's scope + config path is in `bean.py init`'s
output. Changing scope purges the old index — tell the user to `sync` afterward.

Walk the user through what's missing, one source at a time. For each, get the token (ask where to
create it — the error message from a failed `auth` names the exact page), then set it up **one of
three ways**, matching the user's comfort:

1. **You run it** — `bean.py auth <provider> <fields>` (the failed `auth` error names the fields).
   Simplest; the token passes through you.
2. **The user runs it** (privacy) — hand them the same `bean.py auth …` line to run themselves so
   the token never reaches you; continue once they confirm.
3. **Write files directly** — write the credential JSON to the credential file path shown by
   `bean.py init` (keys mirror the listed fields, e.g. `{"token": "…"}`, or
   `{"method":"cloud","url":…,"email":…,"token":…}` for Atlassian Cloud) and append tracked refs
   into that source's config file under the config list names `bean.py init` prints (e.g.
   `slack.[channels]`). Writing refs into those config lists is the **only** way to track refs.

Notes: interactive sources (Google, Microsoft) open a browser/device prompt instead of taking a
token. Whole-collection sources (Slack, Zendesk, Salesforce…) index everything once connected;
tracked lists only *narrow* scope — `bean.py init` flags which sources index-everything-when-
connected. Then finish with `bean.py sync` and a test `search`.

**Lookback — ask when a source has one.** Sources with a first-sync lookback line in `bean.py init`
(Slack, Discord, Google Drive) reach back a bounded window on the *first* sync, then track a cursor
and only re-scan changes after that. When setting one up, ask the user how many days of history to
index on the first sync (0 = all) and, if they give a non-default number, set it with
`bean.py config set <source>.lookback_days <days>` (e.g. `config set slack.lookback_days 30`) before
the first `sync`. It only bounds the initial backfill; later syncs are incremental regardless.

## `sync`

`bean.py sync [source] [--rebuild] [--since N]` — fetches changes and re-embeds **only what
changed**. `--rebuild` ignores cursors and re-fetches back `--since`, re-embedding every doc — run
it to apply a chunking or embedding-model change to already-indexed docs.

**Never run `sync` on your own.** It is the one command you do not run unprompted — it hits the
user's live services and can take minutes. Run it only when the user explicitly asks. When a
read command prints `⚠ bean: last synced N days ago …` (or `status` reports `"stale": true`),
**tell the user their index looks stale and suggest they run `/bean sync`** — then wait for them to
ask. Still answer their question from the current index; just flag that it may be behind.

## `status` / `config`

- `bean.py status` — connections, tracked sources, index counts, embedding model (warns
  if the index was built with a different model than configured).
- `bean.py config list` — resolved settings. `config get <path>` / `config set <path> <value>` for
  `embedding.model`, `chunking.lines`, `search.hybrid`, `ocr.backend`, etc. Chunking is
  **per-source**: a global `chunking` block plus optional `<source>.chunking` overrides merged on
  top (Slack ships smaller defaults for short chat) — set one with e.g.
  `bean.py config set slack.chunking.lines 15`. Changing the embedding model or chunking prints a
  reminder to run `bean sync --rebuild`.

## Tracking refs

There is no routing command. Write the ref directly into the source's config file, under the config
list names `bean.py init` prints (e.g. `slack.[channels]`, `github.[repos]`), then suggest
`/bean sync`.

## `plugins` — connectors beyond the core 12

- `bean.py plugins list` — the core set plus any drop-in plugin files loaded from `~/.bean/plugins/`.
- **A source with no bundled connector?** Author one: read
  `${CLAUDE_PLUGIN_ROOT}/docs/authoring-connectors.md` for the contract, helpers, an offline test
  recipe, and a template. It produces a self-contained module you drop into `~/.bean/plugins/` —
  bean loads anything there exposing a `SOURCE`. No core edits.
