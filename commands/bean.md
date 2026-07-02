---
description: Search your Google Docs / Slack knowledge base (init | sync | status | <question>)
argument-hint: init | sync | status | add <url|#channel> | <question>
allowed-tools: Bash
---

You are driving **bean**, a local search index over the user's Google Docs and Slack. The CLI
lives in this plugin; run it as:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py <subcommand> …
```

(the wrapper creates the plugin's virtualenv and installs dependencies on first use: slower
once, instant afterwards. All index data lives under `~/.bean/<repo>-<hash>/`, per repo.)

Route on the arguments: `$ARGUMENTS`

## No arguments, or a question

Treat the arguments as a question. Run:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py search "<the question>" --json
```

Answer the user's question from the returned chunks, citing each source you used by its title
and URL (Google Doc links and Slack channel links come back in the results). If the results
don't answer it, say so rather than inventing an answer. If the index is empty or the command
reports nothing synced, point the user at `/bean init`.

## `init`

Setup is a conversation; the CLI never prompts. First run:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py init
```

It prints what is and isn't connected. Then walk the user through whatever is missing, one
step at a time:

1. **Google**: the user must run the browser sign-in themselves (it's interactive). Tell them
   to type `! python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py auth google` (the `!` prefix
   runs it in this session). It signs in through gcloud, so there is no Google Cloud setup; if
   gcloud isn't installed the command says how to get it (`brew install google-cloud-sdk`).
2. **Slack**: ask the user to paste a Slack user token (`xoxp-…`; a workspace admin creates a
   minimal Slack app with user scopes `channels:history`, `channels:read`, `users:read` and
   shares the install link). Then run:
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py auth slack --token <token>`
3. **Sources**: ask which Google Docs, Drive folders, or Slack channels matter. For each, run:
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py add <url-or-#channel>`
4. Finish with a sync (below) and confirm with a test search.

## `sync`

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py sync
```

Fetches changed docs/messages and re-embeds them. The first ever sync also downloads the
embedding model (~100 MB); warn the user it takes a few minutes. Report what changed. Slack rate-limits
new apps hard, so a first sync of busy channels can be slow; the CLI prints progress.

## `status`

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py status
```

Report connections, tracked sources, and index counts.

## `add <item>` / `remove <item>`

Pass through directly: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/beanw.py add <item>` (Google Doc
URL, Drive folder URL, or `#channel`), then suggest `/bean sync`.
