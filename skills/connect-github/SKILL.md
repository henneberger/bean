---
name: connect-github
description: Guided setup for connecting GitHub to bean — pick scope, get a PAT, authenticate, track repos, and sync. Use when the user wants to connect or add GitHub as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided GitHub setup)
allowed-tools: Bash
---

# Connect GitHub to bean

Guide the user through connecting **GitHub**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search GitHub from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **local** — GitHub is a project tracker usually tied to the repo you're in.

## 2. Connection method

One method: a **personal access token** (`ghp_…`). GitHub tracks specific repos — you must add at least one; it indexes each repo's issues and PRs.

## 3. Get the credential

Create a PAT at **github.com/settings/tokens** (repo read scope). Ask the user for the token string.

## 4. Connect, scope, sync

1. `bean.py auth github --token ghp_…` — or, if the user is privacy-minded, hand them that line, or write `{"token":"ghp_…"}` to the credential file from `bean.py init`.
2. `bean.py scope github global|local` — set the scope from step 1.
3. Track repos: append each `owner/name` (or a github.com URL) into the `github.[repos]` list in the config file from `bean.py init`.
4. `bean.py sync github` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source github`.
