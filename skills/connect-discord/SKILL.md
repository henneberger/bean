---
name: connect-discord
description: Guided setup for connecting Discord to bean — pick scope, get a bot token, authenticate, track channels/guilds, and sync. Use when the user wants to connect or add Discord as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Discord setup)
allowed-tools: Bash
---

# Connect Discord to bean

Guide the user through connecting **Discord**. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order — keep it a short back-and-forth.

## 1. Scope — global or local?

Ask: **"Search Discord from every repo (global) or just this one (local)?"**
- **global** — one shared index, searchable from any repo.
- **local** — scoped to the current repo only.

Default **global** — Discord is a personal/community account you'll want everywhere.

## 2. Connection method

One method: a **bot token**. Discord tracks specific channels/guilds — you must add at least one; the bot must be invited to the server with the Message Content intent enabled.

## 3. Get the credential

In the **Discord Developer Portal → your app → Bot → Reset Token**, enable the Message Content intent, invite the bot to the server, and copy the token. Ask the user for it.

## 4. Connect, scope, sync

1. `bean.py auth discord --token <bot-token>` — or hand the user that line (privacy), or write `{"token":…}` to the credential file from `bean.py init`.
2. `bean.py scope discord global|local` — set the scope from step 1.
3. Track channels/guilds: append a `discord.com/channels/<guild>/<channel>` URL, `discord:<channelId>`, or `discord:guild:<guildId>` into the `discord.[channels]` / `[guilds]` lists in the config file from `bean.py init`.
4. First-sync lookback: ask how many days of history to backfill (0 = all); if non-default, `bean.py config set discord.lookback_days <days>` (default 14). Then `bean.py sync discord`.

Confirm with `bean.py status`, then a test `bean.py search "<topic>" --source discord`.
