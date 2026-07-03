---
name: connect-jira
description: Guided setup for connecting Jira to bean — pick scope, choose Cloud or Server/DC, get a token, authenticate, track projects, and sync. Use when the user wants to connect or add Jira as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Jira setup)
allowed-tools: Bash
---

# Connect Jira to bean

Guide the user through connecting **Jira**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Jira from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **local** — a Jira project usually maps to the repo/team you're working in.

## 2. Connection method

Two methods, auto-detected by whether you pass `--email`:
- **Cloud** — `--url https://you.atlassian.net --email you@co.com --token <api-token>`.
- **Server / Data Center** — `--url https://jira.you.com --token <PAT>` (no `--email`).

Ask which the user has.

## 3. Get the credential

- **Cloud** token: **id.atlassian.com/manage-profile/security/api-tokens**.
- **Server/DC** PAT: your profile → Personal Access Tokens. Ask for the URL, token (and email for Cloud).

## 4. Connect, scope, sync

1. `bean.py auth jira --url … --token … [--email …]` — or hand the user that line (privacy), or write `{"method":"cloud|dc","url":…,"email":…,"token":…}` to the credential file from `bean.py init`.
2. `bean.py scope jira global|local` — set the scope from step 1.
3. Track projects: append `jira:PROJ` or a `/browse/PROJ-123` URL into the `jira.[projects]` list in the config file from `bean.py init`.
4. `bean.py sync jira` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source jira`.
