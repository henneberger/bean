---
name: connect-hubspot
description: Guided setup for connecting HubSpot to bean — pick scope, get a private-app token, authenticate, and sync. Use when the user wants to connect or add HubSpot as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided HubSpot setup)
allowed-tools: Bash
---

# Connect HubSpot to bean

Guide the user through connecting **HubSpot**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search HubSpot from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **global** — HubSpot is company-wide CRM content you'll want everywhere.

## 2. Connection method

One method: a **private-app token**. HubSpot indexes its collections once connected; adding `hubspot:tickets`, `hubspot:notes`, or `hubspot:kb` refs only *narrows* scope, it's optional.

## 3. Get the credential

In **HubSpot → Settings → Integrations → Private Apps**, create an app with CRM + Knowledge Base read scopes and copy its token. Ask the user for the token.

## 4. Connect, scope, sync

1. `bean.py auth hubspot --token <private-app-token>` — or hand the user that line (privacy), or write `{"token":…}` to the credential file from `bean.py init`.
2. `bean.py scope hubspot global|local` — set the scope from step 1.
3. `bean.py sync hubspot` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source hubspot`.
