---
name: connect-slack
description: Guided setup for connecting Slack to bean — pick scope, get a user token, authenticate, and sync. Use when the user wants to connect or add Slack as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Slack setup)
allowed-tools: Bash
---

# Connect Slack to bean

Guide the user through connecting **Slack**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Slack from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **global** — Slack is a personal account you'll want everywhere.

## 2. Connection method

One method: a **Slack user token** (`xoxp-…`). Slack indexes **all your channels** once connected; adding `#channel` refs only *narrows* scope, it's optional.

## 3. Get the credential

Create a user token at **api.slack.com/apps** → your app → OAuth & Permissions (user token, `xoxp-…`). Ask the user for the token string.

## 4. Connect, scope, sync

1. `bean.py auth slack --token xoxp-…` — or, if the user is privacy-minded, hand them that exact line to run, or write `{"token":"xoxp-…"}` to the credential file from `bean.py init`.
2. `bean.py scope slack global|local` — set the scope from step 1.
3. First-sync lookback: ask how many days of history to backfill (0 = all); if non-default, `bean.py config set slack.lookback_days <days>` (default 14).
4. `bean.py sync slack` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source slack`.
