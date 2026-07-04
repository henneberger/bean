---
name: connect-jira
description: Complete click-by-click setup for connecting Jira to bean — pick scope, choose Atlassian Cloud or Server/Data Center, walk through creating the exact credential, authenticate, track projects, and sync. Use when the user wants to connect or add Jira as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Jira setup)
allowed-tools: Bash
---

# Connect Jira to bean

Guide the user through connecting **Jira** end to end. You run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order. Keep it a back-and-forth: ask, wait for the answer, then act. Don't dump all four steps at once.

What gets indexed: for each project you track, bean pulls every issue and stores **one document per issue** — its **summary**, **description**, and all **comments**. Search then hits issue text directly. You must track at least one project or a sync indexes nothing. There is no first-sync lookback window; the first sync reaches the full history of each tracked project, and later syncs are incremental (bean keeps a per-project `jira.since.{PROJ}` cursor and only re-pulls issues updated since).

## 1. Scope — global or local?

Ask: **"Search Jira from every repo (global) or just this one (local)?"**

- **local** — the index lives with the current repo and is searchable only from here.
- **global** — one shared index, searchable from any repo on this machine.

**Default to local.** Jira is usually a project-scoped tool tied to the repo/team you're working in. Pick **global** only if this is a company-wide Jira instance you'll want to search from everywhere.

Remember the answer; you set it with `bean.py scope jira …` in step 4.

## 2. Connection method — Cloud or Server/Data Center?

Jira comes in two flavors and bean supports both. Ask the user which they have — the fastest tell is the URL they use to open Jira in a browser:

- **(A) Atlassian Cloud** — the URL looks like `https://YOURSITE.atlassian.net`. Hosted by Atlassian. Auth is **email + API token** (sent as HTTP Basic).
- **(B) Server / Data Center** — self-hosted at your company's own domain, e.g. `https://jira.yourcompany.com`. Auth is a **Personal Access Token (PAT)** sent as a Bearer token, with **no email**.

bean decides which flow to use purely by whether you pass `--email`: supply it and you get the Cloud (Basic) flow; omit it and you get the Server/DC (Bearer) flow. So getting this right matters.

If the user isn't sure, ask them to open Jira and read you the domain in the address bar. `*.atlassian.net` → Cloud. Anything else → Server/Data Center.

## 3. Get the credential

Walk the user through the path for their flavor. Have them paste the token back to you (or hold it for the privacy path in step 4). Also confirm the exact **site URL** — you need it verbatim for `--url`.

### (A) Atlassian Cloud — API token

1. Go to **https://id.atlassian.com/manage-profile/security/api-tokens** (Atlassian account settings → **Security** → **API tokens**).
2. Click **Create API token**.
3. Give it a **Label** (e.g. `bean`) so you can recognize it later.
4. Click **Create**.
5. Click **Copy** in the dialog and paste it somewhere safe. **The token is shown only once** — if it's lost you delete it and create a new one.
6. The **site URL** is your Jira base, `https://YOURSITE.atlassian.net` (no trailing path).
7. The **email** is the Atlassian account email you just logged in with.

### (B) Server / Data Center — Personal Access Token

1. In Jira, click your **profile avatar** in the top-right corner.
2. Choose **Profile**.
3. Open **Personal Access Tokens** (in the left/side menu of your profile).
4. Click **Create token**.
5. Give it a **name** (e.g. `bean`) and set an **expiry** if prompted.
6. Click **Create**, then copy the token value shown.
7. The **site URL** is your self-hosted base, e.g. `https://jira.yourcompany.com` (no trailing path). There is **no email** for this flow.

## 4. Connect, scope, track projects, sync

### 4a. Authenticate

Build the command for the user's flavor:

- **Cloud:**
  ```
  bean.py auth jira --url https://YOURSITE.atlassian.net --email you@company.com --token <api-token>
  ```
- **Server / Data Center:**
  ```
  bean.py auth jira --url https://jira.yourcompany.com --token <PAT>
  ```

Offer the user three ways to do this, so a token they'd rather not paste to you never has to be:

1. **You run it** — paste their token into the command above and run it via Bash. Simplest.
2. **They run it** — hand them the exact line to run in their own terminal.
3. **They write the file** — run `bean.py init`, read out the **credential file path** it prints for Jira, and have them write the JSON directly:
   - Cloud: `{"method":"cloud","url":"https://YOURSITE.atlassian.net","email":"you@company.com","token":"<api-token>"}`
   - Server/DC: `{"method":"dc","url":"https://jira.yourcompany.com","token":"<PAT>"}`

On success, `auth` prints `✓ Jira connected` (with your display name if the instance returned it).

### 4b. Set the scope

Apply the choice from step 1:

```
bean.py scope jira global
```
or
```
bean.py scope jira local
```

### 4c. Track at least one project

bean only indexes projects you list. Add each by its **project key** (the uppercase prefix on issue keys — the `ENG` in `ENG-1234`).

Run `bean.py init` to get the config file path, then add each project into the jira **`projects`** list. Each entry accepts either form:

- `jira:PROJ` (e.g. `jira:ENG`), or
- a browse URL like `https://YOURSITE.atlassian.net/browse/PROJ-123` (bean pulls the project key out of it).

Add one line per project you want searchable. Removing a project later and re-syncing prunes that project's issues from the index.

### 4d. Sync

```
bean.py sync jira
```

This pulls every tracked project's issues and indexes each as one document (summary + description + comments). The first sync covers full history and may take a while on large projects; later syncs only fetch issues updated since the last run.

### 4e. Confirm

```
bean.py status
```
to see Jira connected, scoped, and its doc count, then a real query:
```
bean.py search "<topic>" --source jira
```
