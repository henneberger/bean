---
name: connect-zendesk
description: Guided setup for connecting Zendesk to bean — pick scope, get an API token, authenticate, and sync. Use when the user wants to connect or add Zendesk as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Zendesk setup)
allowed-tools: Bash
---

# Connect Zendesk to bean

Guide the user through connecting **Zendesk**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Zendesk from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **global** — Zendesk is company-wide support content you'll want everywhere.

## 2. Connection method

One method: **subdomain + email + API token**. Zendesk indexes **both tickets and articles** once connected; adding `zendesk:tickets` / `zendesk:articles` refs only *narrows* scope, it's optional.

## 3. Get the credential

Create an API token in **Admin Center → Apps and integrations → APIs → Zendesk API → add token**. Ask for the subdomain (`acme` from `acme.zendesk.com`), your agent email, and the token.

## 4. Connect, scope, sync

1. `bean.py auth zendesk --subdomain acme --email you@acme.com --token <api-token>` — or hand the user that line (privacy), or write `{"subdomain":…,"email":…,"token":…}` to the credential file from `bean.py init`.
2. `bean.py scope zendesk global|local` — set the scope from step 1.
3. `bean.py sync zendesk` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source zendesk`.
