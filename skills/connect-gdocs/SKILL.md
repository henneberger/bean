---
name: connect-gdocs
description: Guided setup for connecting Google Drive / Docs to bean — pick scope, sign in through gcloud, and sync. Use when the user wants to connect or add Google Drive or Google Docs as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Google Drive setup)
allowed-tools: Bash
---

# Connect Google Drive to bean

Guide the user through connecting **Google Drive** (Docs). Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Google Drive from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **global** — your Drive is a personal account you'll want everywhere.

## 2. Connection method

One method: **interactive gcloud sign-in** — no token. `bean auth google` opens a browser; the user signs in and clicks Allow. Needs the `gcloud` CLI installed (`brew install google-cloud-sdk`). Drive indexes **docs you own** once connected; adding doc/folder URLs only *narrows* scope, it's optional.

## 3. Get the credential

No token to fetch — the browser sign-in is the credential. Make sure the user signs in with the Google account that can see the docs they want indexed.

## 4. Connect, scope, sync

1. `bean.py auth google` — the user completes the browser sign-in. (No token passes through you.)
2. `bean.py scope gdocs global|local` — set the scope from step 1.
3. First-sync lookback: ask how many days of history to backfill (0 = all); if non-default, `bean.py config set gdocs.lookback_days <days>` (default 30).
4. `bean.py sync gdocs` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source gdocs`.
