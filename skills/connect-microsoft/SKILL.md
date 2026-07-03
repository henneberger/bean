---
name: connect-microsoft
description: Guided setup for connecting Microsoft 365 to bean — pick scope, choose device-code or az CLI sign-in, authenticate, track drives/mail/teams, and sync. Use when the user wants to connect or add Microsoft 365 as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Microsoft 365 setup)
allowed-tools: Bash
---

# Connect Microsoft 365 to bean

Guide the user through connecting **Microsoft 365**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Microsoft 365 from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **global** — Microsoft 365 is a personal account you'll want everywhere.

## 2. Connection method

Two interactive methods (no token on the command line):
- **Device code** (default) — `bean auth microsoft` prints a code + URL for the user to enter in a browser.
- **az CLI** — `bean auth microsoft --method az` reuses an existing `az login` session.

Ask which the user prefers.

## 3. Get the credential

No token to fetch — the sign-in is the credential. For device code the user opens the shown URL and enters the code; for `az` they must already be logged in via `az login`.

## 4. Connect, scope, sync

1. `bean.py auth microsoft` (or `--method az`) — the user completes the interactive sign-in. (No token passes through you.)
2. `bean.py scope microsoft global|local` — set the scope from step 1.
3. Track items: append `ms:file:<itemId>`, `ms:mail:inbox`, or `ms:teams:<teamId>/<channelId>` into the `microsoft.[drives]` / `[mail]` / `[teams]` lists in the config file from `bean.py init`.
4. `bean.py sync microsoft` — build the index.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source microsoft`.
