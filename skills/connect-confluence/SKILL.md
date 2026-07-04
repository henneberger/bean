---
name: connect-confluence
description: Complete click-by-click setup for connecting Confluence to bean — pick scope, choose Atlassian Cloud or Server/Data Center, create an API token or Personal Access Token, authenticate, track spaces and pages, and sync. Use when the user wants to connect or add Confluence as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Confluence setup)
allowed-tools: Bash
---

# Connect Confluence to bean

Guide the user through connecting **Confluence** end to end. You run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order. This is a deliberately thorough walkthrough — read the exact button and field names to the user, confirm each credential before moving on, and don't skip steps.

Confluence comes in two flavors and bean supports both:
- **Atlassian Cloud** — hosted by Atlassian, URL looks like `https://YOURSITE.atlassian.net/wiki`. Authenticates with your **email + an API token** (HTTP Basic).
- **Server / Data Center** — self-hosted by your company, URL looks like `https://wiki.yourcompany.com`. Authenticates with a **Personal Access Token** (Bearer), no email.

bean auto-detects which one from whether you pass `--email`: supply `--email` and it uses Cloud; omit it and it uses Server/Data Center.

---

## 1. Scope — global or local?

Ask: **"Search Confluence from every repo (global) or just this one (local)?"**

- **local** (default) — the index lives with the current repo and is searchable only from here. Pick this when the space maps to the project or team you're working in.
- **global** — one shared index searchable from any repo. Pick this for a company-wide wiki you want reachable everywhere.

Default to **local** unless the user says otherwise.

---

## 2. Choose the connection method

Ask: **"Is your Confluence on Atlassian Cloud (a `*.atlassian.net` address) or a self-hosted Server / Data Center instance?"**

If the user isn't sure, have them look at the URL in their browser when they're in Confluence:
- Contains `.atlassian.net/wiki` → **Cloud** (go to step 3A).
- A company domain like `wiki.acme.com` or `confluence.acme.com` → **Server / Data Center** (go to step 3B).

---

## 3. Get the credential

### 3A. Atlassian Cloud — create an API token

Walk the user through this:

1. Go to **https://id.atlassian.com/manage-profile/security/api-tokens** (Account settings → **Security** → **Create and manage API tokens**). Sign in if prompted.
2. Click **Create API token**.
3. In the **Label** field, enter a name you'll recognize later, e.g. `bean`.
4. Click **Create**.
5. Click **Copy** (or the copy icon) to copy the token. **It is shown only once** — if they lose it they must create a new one.
6. Have the user paste the token to you, along with:
   - their **Atlassian account email** (the one they log in with), and
   - their **site URL**, which is `https://YOURSITE.atlassian.net/wiki` — **include the `/wiki` suffix**. YOURSITE is the subdomain they see in the browser.

You now have `--url`, `--email`, and `--token`. Continue to step 4.

### 3B. Server / Data Center — create a Personal Access Token

Walk the user through this (their admin must have PATs enabled; most modern Data Center instances do):

1. In Confluence, click your **profile avatar** (top-right corner).
2. Choose **Settings**.
3. In the left sidebar, open **Personal Access Tokens**.
4. Click **Create token**.
5. Give it a **name** (e.g. `bean`) and set an **expiry** (or leave it non-expiring if the admin allows).
6. Click **Create**, then copy the token that appears.
7. Have the user paste the token to you, along with their **site base URL**, e.g. `https://wiki.yourcompany.com` (the part before `/display/…` or `/spaces/…` in their Confluence links). Do **not** add `/wiki` unless that's literally part of their base URL.

You now have `--url` and `--token` (no email). Continue to step 4.

---

## 4. Connect, scope, track, sync

### 4.1 Authenticate

Pick one of three paths depending on how the user wants to handle the credential:

- **You run it** (default) — run the appropriate line yourself:
  - Cloud: `bean.py auth confluence --url https://you.atlassian.net/wiki --email you@company.com --token <api-token>`
  - Server/DC: `bean.py auth confluence --url https://wiki.yourcompany.com --token <PAT>`
- **User runs it** (privacy — token never passes through you) — hand them the exact line above to paste into their own terminal.
- **Write the credential file directly** — run `bean.py init` to get the credential file path, then write the JSON there:
  - Cloud: `{"method":"cloud","url":"https://you.atlassian.net/wiki","email":"you@company.com","token":"<api-token>","name":null}`
  - Server/DC: `{"method":"dc","url":"https://wiki.yourcompany.com","email":null,"token":"<PAT>","name":null}`

A successful `auth` prints `✓ Confluence connected (…)`. On Cloud it verifies the token by fetching the current user; on Server/DC the identity endpoint may 404, which is fine — the credential is still saved.

### 4.2 Set the scope

```
bean.py scope confluence global|local
```

Use the scope chosen in step 1.

### 4.3 Track at least one space or page

Nothing syncs until you track content. Run `bean.py init` to see the config file path and the `confluence` block, then add entries to its lists:

- **Track a whole space** — put the space key in the `spaces` list. Every page in the space is indexed.
- **Track individual pages** — put page ids in the `pages` list.

Each entry may be written as any of these accepted forms (as shown by `bean.py init`):
- a Confluence **page or space URL** (paste straight from the browser),
- `confluence:SPACEKEY` (e.g. `confluence:ENG`), or
- `confluence:page:ID` (e.g. `confluence:page:123456`).

You **must** track at least one space or page — a connected-but-empty Confluence syncs nothing.

### 4.4 Sync

```
bean.py sync confluence
```

This lists every page in each tracked space and fetches individually tracked pages, then indexes each page as **title + body** (the storage-format HTML flattened to text). Change detection uses each page's version number, so a re-sync only re-embeds pages that actually changed, and removing a space from the config prunes its pages. There is **no first-sync lookback window** — the entire tracked space is indexed on the first sync regardless of age.

### 4.5 Confirm

```
bean.py status
bean.py search "<topic>" --source confluence
```

`status` should show Confluence connected with the pages it indexed; the test search should return real hits from a topic you know is in the wiki.
