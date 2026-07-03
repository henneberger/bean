---
name: connect-salesforce
description: Guided setup for connecting Salesforce to bean — pick scope, get an access token + instance URL, authenticate, and sync. Use when the user wants to connect or add Salesforce as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Salesforce setup)
allowed-tools: Bash
---

# Connect Salesforce to bean

Guide the user through connecting **Salesforce**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Salesforce from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **global** — Salesforce is company-wide CRM content you'll want everywhere.

## 2. Connection method

One method: an **OAuth access token + instance URL**. Salesforce indexes **both articles and cases** once connected; adding `salesforce:articles` / `salesforce:cases` refs only *narrows* scope, it's optional.

## 3. Get the credential

Get an access token from an OAuth flow / connected app (or a session id) and note the instance URL (`https://you.my.salesforce.com`). Ask the user for the token and URL.

## 4. Connect, scope, sync

1. `bean.py auth salesforce --token <access-token> --url https://you.my.salesforce.com` — or hand the user that line (privacy), or write `{"token":…,"url":…}` to the credential file from `bean.py init`.
2. `bean.py scope salesforce global|local` — set the scope from step 1.
3. `bean.py sync salesforce` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source salesforce`.
