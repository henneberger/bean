---
name: connect-discord
description: Complete click-by-click setup for connecting Discord to bean — pick scope, create and invite a Discord bot, get its token, track channels/guilds, and sync. Use when the user wants to connect or add Discord as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Discord setup)
allowed-tools: Bash
---

# Connect Discord to bean

Guide the user through connecting **Discord**, end to end. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four parts in order. This is a longer walkthrough than most bean skills on purpose — Discord requires creating and inviting a bot, so don't skip the click-by-click detail. Read the exact steps to the user, wait where they need to act, and confirm before moving on.

## 1. Scope — global or local?

Ask: **"Search Discord from every repo (global) or just this one (local)?"**

- **global** (default) — one shared index, searchable from any repo. Right for a personal or community Discord account you'll want everywhere.
- **local** — scoped to the current repo only. Use this only if this Discord content belongs to one project.

Default to **global** unless the user says otherwise.

## 2. Connection method — a Discord bot token

Discord has **no user-token API** for reading message history the way bean needs, so bean uses a **bot** that you create and invite to the server(s) you want indexed. The bot reads message text through the Discord API using a **bot token**.

Key facts to tell the user up front:

- The bot only sees channels it has access to. If a channel is private, the bot must be given access to it (add the bot's role, or grant it **View Channels** on that channel).
- You need **Manage Server** permission on any server you want to invite the bot into.
- bean indexes **one document per message** in the channels/guilds you track that the bot can access. It does not index a whole server automatically — you list what to track in part 4.

## 3. Get the credential — create the bot and copy its token

Walk the user through this in a browser. **Bold** names below are the exact button, menu, and field labels.

### 3a. Create the application

1. Go to **https://discord.com/developers/applications**.
2. Click **New Application** (top right).
3. Enter a name (e.g. `bean`) → check the terms box → click **Create**.

### 3b. Create the bot and copy its token

1. In the left sidebar, click **Bot**.
2. Click **Reset Token** → confirm with **Yes, do it!** (you may be asked for your Discord password / 2FA).
3. Click **Copy** to copy the token. **Discord shows this token only once** — if they lose it they must **Reset Token** again. Have them paste it somewhere safe for the next step.

### 3c. Enable the Message Content intent

On the same **Bot** page, scroll to **Privileged Gateway Intents**:

1. Turn **ON** the **Message Content Intent** toggle. This is **required** — without it the bot receives empty message text and nothing useful gets indexed.
2. Click **Save Changes**.

### 3d. Invite the bot to the server

1. In the left sidebar, click **OAuth2**, then **URL Generator**.
2. Under **Scopes**, check **bot**.
3. A **Bot Permissions** section appears. Check **View Channels** and **Read Message History**.
4. Copy the **Generated URL** at the bottom of the page.
5. Open that URL in a browser tab.
6. In the **Add to Server** dropdown, pick the **server** you want indexed → click **Continue** → **Authorize** (complete the CAPTCHA if shown).
7. Repeat this step for each additional server you want the bot in.

Reminder: they need **Manage Server** permission on the server to complete the invite.

## 4. Connect, scope, track, and sync

### 4a. Authenticate

Command:

```
bean.py auth discord --token <bot-token>
```

Offer the user a privacy choice for the token — pick one:

- **You run it** — they paste the token to you and you run the command above.
- **They run it** — hand them the exact line to run in their own terminal, so the token never passes through the chat.
- **Write the file** — run `bean.py init` to get the credential file path, then write `{"token": "<bot-token>"}` to that path directly.

On success bean prints `✓ Discord connected as <name>.`

### 4b. Set the scope

```
bean.py scope discord global|local
```

Use the scope chosen in part 1.

### 4c. Track channels and/or guilds

The bot indexes nothing until you tell it what to track. Run `bean.py init` to see the Discord config block, then add references to the `channels` and `guilds` lists. Accepted reference forms (from `bean.py init`):

- a **`discord.com/channels/<guild>/<channel>`** URL — the URL in your browser when viewing a channel
- **`discord:<channelId>`** — a single channel by ID
- **`discord:guild:<guildId>`** — a whole guild (every text channel the bot can access in it)

**Getting IDs.** To copy a channel or server ID, enable **Developer Mode** in Discord first: **User Settings → Advanced → Developer Mode** (toggle ON). Then right-click a channel and choose **Copy Channel ID**, or right-click the server icon and choose **Copy Server ID**.

Add at least one channel or guild. A guild reference is the quickest way to cover a whole server.

### 4d. Set first-sync lookback

Ask: **"How many days of history should the first sync backfill?"**

- Default is **14** days. `0` means **all** history.
- Only if they want something other than 14:

  ```
  bean.py config set discord.lookback_days <N>
  ```

After the first sync, bean tracks a per-channel cursor and only pulls new messages, so this setting matters mainly for the initial backfill.

### 4e. Sync

```
bean.py sync discord
```

This resolves the tracked channels (and every text channel in tracked guilds the bot can access), then indexes one document per message back to the lookback floor.

### 4f. Confirm

```
bean.py status
bean.py search "<topic>" --source discord
```

`status` should show the Discord source with a document count; the search should return real messages. If counts are zero, check that the **Message Content Intent** is on, the bot was actually invited to the server, and the bot has **View Channels** on the tracked (especially private) channels.
