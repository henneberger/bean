---
name: connect-slack
description: Complete click-by-click setup for connecting Slack to bean — pick scope, create a Slack app, get a User OAuth token (xoxp-, sees your DMs + private channels) or a Bot token (xoxb-), authenticate, and sync. Use when the user wants to connect or add Slack as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Slack setup)
allowed-tools: Bash
---

# Connect Slack to bean

You are driving this setup for the user. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below.) Work the four parts in order. This is a longer walkthrough than bean's other skills on purpose — go step by step, confirm each part before moving on, and don't skip the Slack-app screens.

## Part 1 — Scope: global or local?

Ask the user: **"Do you want to search Slack from every repo (global) or just this one (local)?"**

- **global** — one shared index, searchable from any repo on this machine.
- **local** — scoped to the current repo only.

**Default to global.** Slack is almost always a personal account the user will want to reach from everywhere, so unless they say otherwise, plan on `global`. You'll actually set this in Part 4 after auth.

## Part 2 — Connection method: User token or Bot token?

bean accepts either kind of Slack token. Explain the trade-off and let the user pick:

**(A) User OAuth token — `xoxp-…` (recommended).**
- Acts as *you*. bean searches everything your Slack account can already see: all public channels you're in, your private channels, your DMs, and group DMs.
- No need to invite anything into channels.
- Best when the goal is "make my Slack searchable."

**(B) Bot token — `xoxb-…`.**
- Acts as a bot user. The bot only sees channels it has been **explicitly invited into**, and never sees your DMs.
- Good when you want a narrow, auditable, shared-workspace integration rather than personal access.

Both come from the same Slack app you'll create in Part 3 — the only difference is whether you add the scopes under **User Token Scopes** or **Bot Token Scopes**, and which token you copy at the end.

Once connected, bean indexes **all channels that token can see** — one document per thread (a root message and its replies) and one per standalone message. You do **not** need to list channels. Adding `#channel` references later only *narrows* the sync to those channels; it's optional.

## Part 3 — Get the credential

### 3.0 — Create a Slack app (both methods)

1. Go to **https://api.slack.com/apps** and sign in.
2. Click **Create New App**.
3. Choose **From scratch**.
4. Enter an **App Name** (e.g. `bean-search`) and pick the **workspace** you want to index under **Pick a workspace to develop your app in**.
5. Click **Create App**. You land on the app's **Basic Information** page.
6. In the left sidebar, click **OAuth & Permissions**.

Now follow **3A** (user token) or **3B** (bot token) depending on the choice from Part 2.

### 3A — User OAuth token (`xoxp-…`)

1. On **OAuth & Permissions**, scroll to **Scopes**.
2. Under **User Token Scopes** (not Bot Token Scopes), click **Add an OAuth Scope** and add each of these, one at a time:
   - `channels:history` — read messages in public channels
   - `groups:history` — read messages in private channels
   - `im:history` — read your direct messages
   - `mpim:history` — read group direct messages
   - `channels:read` — list public channels
   - `groups:read` — list private channels
   - `users:read` — resolve `@user` IDs to display names
3. Scroll back to the top of **OAuth & Permissions** and click **Install to Workspace**.
4. On the consent screen, review the access and click **Allow**.
5. You're returned to **OAuth & Permissions**. Copy the value under **User OAuth Token** — it starts with **`xoxp-`**.

That token is all bean needs. Give it to the assistant (or keep it private — see Part 4).

### 3B — Bot token (`xoxb-…`)

1. On **OAuth & Permissions**, scroll to **Scopes**.
2. Under **Bot Token Scopes**, click **Add an OAuth Scope** and add the same set:
   - `channels:history`, `groups:history`, `im:history`, `mpim:history`, `channels:read`, `groups:read`, `users:read`
3. Scroll up and click **Install to Workspace**, then **Allow**.
4. Copy the value under **Bot User OAuth Token** — it starts with **`xoxb-`**.
5. **Invite the bot into every channel you want indexed.** In Slack, open a channel and type `/invite @bean-search` (use your app's name). A bot token sees **only** channels it's been invited to — nothing is indexed until you invite it somewhere.

### Gotcha: private channels

Private channels require both `groups:read` **and** `groups:history`. If those scopes are missing, bean quietly falls back to indexing **public channels only** (it logs `token lacks groups:read — indexing public channels only`) instead of failing. If you expect private-channel results and don't get them, re-check those two scopes and reinstall the app.

## Part 4 — Connect, scope, sync

### 4.1 — Authenticate

Run (substitute the real token; `xoxp-…` for a user token or `xoxb-…` for a bot token):

```
bean.py auth slack --token xoxp-…
```

bean calls Slack's `auth.test` to validate the token and stores it, then prints `✓ Slack connected as <user> in <team>`.

**Offer the privacy path** — a token is a sensitive credential, so give the user three ways to provide it:
1. **You run it** — paste the token into the `bean.py auth slack --token …` command above (default).
2. **They run it** — hand the user the exact `bean.py auth slack --token …` line to run in their own terminal, so the token never passes through the chat.
3. **They write the file** — run `bean.py init` to print the credential file path, and have the user write `{"token":"xoxp-…"}` there directly.

### 4.2 — Set scope

Apply the choice from Part 1:

```
bean.py scope slack global
```

(or `bean.py scope slack local`).

### 4.3 — Choose the first-sync lookback

Ask: **"How many days of history should the first sync backfill? (default 14, 0 = all time)"**

Only if they pick something other than 14, set it before syncing:

```
bean.py config set slack.lookback_days <N>
```

The lookback applies to the *first* sync of each channel. Later syncs re-scan a trailing 7-day window (to catch edits and late replies) plus anything newer than the last cursor.

### 4.4 — Sync

```
bean.py sync slack
```

This lists every channel the token can see and indexes each thread and standalone message. This can take a while on a busy workspace or a large lookback.

### 4.5 — Confirm

```
bean.py status
```

Then run a real test search on a topic you know is discussed:

```
bean.py search "<topic>" --source slack
```

If results look right, Slack is connected.

### Narrowing to specific channels (optional)

If the user wants to index only certain channels instead of everything, add them:

```
bean.py add slack "#channel-name"
```

With no channels added, bean syncs **all** channels the token can see — that's the intended default.
