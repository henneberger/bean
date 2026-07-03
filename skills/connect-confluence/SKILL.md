---
name: connect-confluence
description: Guided setup for connecting Confluence to bean — pick scope, choose Cloud or Server/DC, get a token, authenticate, track spaces, and sync. Use when the user wants to connect or add Confluence as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Confluence setup)
allowed-tools: Bash
---

# Connect Confluence to bean

Guide the user through connecting **Confluence**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Confluence from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **local** — a Confluence space usually maps to the project/team you're working in.

## 2. Connection method

Two methods, auto-detected by whether you pass `--email`:
- **Cloud** — `--url https://you.atlassian.net/wiki --email you@co.com --token <api-token>`.
- **Server / Data Center** — `--url https://wiki.you.com --token <PAT>` (no `--email`).

Ask which the user has.

## 3. Get the credential

- **Cloud** token: **id.atlassian.com/manage-profile/security/api-tokens**.
- **Server/DC** PAT: your profile → Personal Access Tokens. Ask for the URL, token (and email for Cloud).

## 4. Connect, scope, sync

1. `bean.py auth confluence --url … --token … [--email …]` — or hand the user that line (privacy), or write `{"method":"cloud|dc","url":…,"email":…,"token":…}` to the credential file from `bean.py init`.
2. `bean.py scope confluence global|local` — set the scope from step 1.
3. Track spaces/pages: append `confluence:SPACEKEY`, `confluence:page:ID`, or a page/space URL into the `confluence.[spaces]` / `[pages]` lists in the config file from `bean.py init`.
4. `bean.py sync confluence` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source confluence`.
