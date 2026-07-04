---
name: connect-zendesk
description: Complete click-by-click setup for connecting Zendesk to bean — pick scope, create a Zendesk API token in Admin Center, authenticate with subdomain + email + token, and sync tickets and help-center articles. Use when the user wants to connect or add Zendesk as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Zendesk setup)
allowed-tools: Bash
---

# Connect Zendesk to bean

This guide walks the user through connecting **Zendesk** to bean end to end. You (the assistant) run
every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

Written as `bean.py` below. Work the four parts in order. Keep it a short back-and-forth: ask the
scope question, walk the user through creating the token, then run the commands for them.

---

## 1. Scope — global or local?

Decide where the Zendesk index lives before authenticating.

Ask: **"Do you want to search Zendesk from every project (global), or only from this one project (local)?"**

- **global** (default) — one shared index, searchable from any repo or directory on this machine.
  A company helpdesk is knowledge you'll want to reach from anywhere, so pick this unless the user
  says otherwise.
- **local** — the index is scoped to the current project directory only. Choose this if the Zendesk
  content is only relevant to one repo, or the user wants to keep it isolated.

You set the scope in Part 4, after auth. Just settle on **global** or **local** now.

---

## 2. Connection method

There is **one** way to connect Zendesk: a **Zendesk API token** used with HTTP Basic auth. bean
sends `{email}/token:{api_token}` as the credential — this is Zendesk's standard token scheme, no
OAuth app or admin approval flow required.

bean needs **three** pieces:

1. **Subdomain** — the `acme` in `acme.zendesk.com`. It's the first label of your Zendesk URL. (You
   can also hand bean the full URL like `https://acme.zendesk.com` and it will extract `acme` for
   you.)
2. **Agent email** — the email address of a Zendesk user who has **agent or admin** access with read
   permission to tickets and articles. The token is bound to this account; whatever this user can
   read, bean can index.
3. **API token** — the secret you create in Part 3.

Ask the user for the subdomain and which agent/admin email they'll use. Then walk them through
creating the token.

---

## 3. Get the credential (create the API token)

Guide the user through the Zendesk **Admin Center**. The token is shown **only once**, so have them
copy it immediately.

1. Sign in to Zendesk as an **admin** (creating API tokens requires admin rights, even though the
   token can be paired with any agent's email).
2. Open the **Admin Center** at `https://YOURSUBDOMAIN.zendesk.com/admin` (replace `YOURSUBDOMAIN`
   with their subdomain — e.g. `https://acme.zendesk.com/admin`). You can also reach it from the
   Zendesk product tray (the grid icon) → **Admin Center**.
3. In the left sidebar click **Apps and integrations**.
4. Under it, click **APIs** → **Zendesk API**.
5. Select the **Settings** tab.
6. Turn **Token access** to **ON** (Enabled) if it isn't already.
7. Click **Add API token** (the plus/add button next to **Active API tokens**).
8. Optionally type an **API token description** (a label like `bean` so it's identifiable later).
9. **Copy** the token value that appears — it is displayed **once and never again**. Have the user
   paste it somewhere safe or straight to you.
10. Click **Save** to store the token. (If they navigate away without saving, the token is discarded.)

Direct link to the token settings page: `https://YOURSUBDOMAIN.zendesk.com/admin/apps-integrations/apis/zendesk-api/settings`

Now collect the three values from the user: **subdomain**, **agent email**, **API token**.

---

## 4. Connect, scope, sync

### Authenticate

The command is:

```
bean.py auth zendesk --subdomain acme --email you@acme.com --token <api-token>
```

You may pass `--url https://acme.zendesk.com` instead of `--subdomain acme` — bean extracts the
subdomain from the URL.

Because the token is a secret, offer the user three ways to run this (their choice):

- **You run it** — paste their values into the command above and run it via Bash. Simplest.
- **They run it** — hand them the exact line with their values filled in, and they run it in their
  own terminal so the token never passes through you.
- **Write the credential file** — run `bean.py init` to get the credential file path, then write
  `{"subdomain": "acme", "email": "you@acme.com", "token": "<api-token>"}` directly to that path.

On success bean verifies the token against `/users/me` and prints `✓ Zendesk connected as <name>`.

### Set scope

Apply the decision from Part 1:

```
bean.py scope zendesk global      # or: bean.py scope zendesk local
```

### Sync

```
bean.py sync zendesk
```

This indexes **both** kinds of content, with **no** tracking step and **no** first-sync lookback
window:

- **Tickets** — subject, description, and every public and internal comment. Pulled incrementally
  via a cursor, so each later sync only fetches tickets touched since last time.
- **Help-center articles** — full article bodies (HTML converted to text).

Everything the connected account can read is indexed automatically once connected. You do **not**
need to add anything to make a sync happen.

**Optional narrowing:** if the user wants only one kind, add `zendesk:tickets` or `zendesk:articles`
to the `include` list. This *narrows* the sync to that one kind — it is never required to enable
syncing, and omitting it means both kinds sync.

### Confirm

```
bean.py status
bean.py search "<topic>" --source zendesk
```

`status` should show Zendesk connected with an indexed doc count; the search returns matching
tickets/articles. Pick a `<topic>` you'd expect in their helpdesk (a product name, a common issue).
