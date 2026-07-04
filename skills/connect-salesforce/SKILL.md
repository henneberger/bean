---
name: connect-salesforce
description: Complete click-by-click setup for connecting Salesforce to bean — pick scope, mint an OAuth access token + instance URL (Salesforce CLI, a Connected App, or an existing session), authenticate, and sync Knowledge articles and Cases. Use when the user wants to connect or add Salesforce as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided Salesforce setup)
allowed-tools: Bash
---

# Connect Salesforce to bean

This skill walks the user all the way from nothing to a searchable Salesforce index. Salesforce
auth is not a single paste-a-key step — you have to obtain a short-lived OAuth **access token** and
your org's **instance URL**, and there are a few ways to do that. Take the user through it slowly.

Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four parts in order. Ask, wait for the answer, then act —
don't dump every command at once.

---

## Part 1 — Scope: global or local?

Ask the user: **"Do you want to search Salesforce from every repo (global), or only from this
project (local)?"**

- **global** — one shared index, searchable from any directory on this machine. This is the right
  choice for almost everyone: Salesforce is company-wide CRM content (support Knowledge base,
  customer Cases) that you'll want to reach from anywhere.
- **local** — the index lives with the current repo only. Choose this if this Salesforce org is
  specific to one project and you don't want it surfacing elsewhere.

**Default to global.** You'll set this in Part 4 with `bean.py scope salesforce global|local`.

---

## Part 2 — How the connection works

bean connects to Salesforce with two pieces of information:

1. an **OAuth access token** — a bearer token that proves you're logged in, and
2. your org's **instance URL** — the base address of your Salesforce org, like
   `https://mycompany.my.salesforce.com` (some older orgs look like
   `https://na123.salesforce.com`).

bean stores exactly the two values you hand it and verifies the token against the Salesforce REST
API at connect time.

### Important: access tokens are short-lived

Salesforce OAuth access tokens **expire** (session timeouts are commonly a few hours, sometimes
less). bean does not refresh them — it stores what you give it and nothing more. So expect this:

> A sync that used to work suddenly fails with an authentication / 401 error. That means the token
> expired. The fix is to **get a fresh token** (repeat Part 3) and re-run
> `bean.py auth salesforce --token <new-token> --url <same-url>`. Nothing else changes; the index
> stays put.

Tell the user this up front so the re-auth step later isn't a surprise. If they want the least
friction long-term, the Salesforce CLI (method A) is the easiest to re-run.

### Three ways to get the token + URL (easiest first)

- **A. Salesforce CLI (`sf`)** — recommended. One browser login, then a single command prints both
  the access token and the instance URL. Easiest to repeat when the token expires.
- **B. Connected App OAuth flow** — create a Connected App in Setup, then run an OAuth flow against
  it to mint a token. More setup, but it's the "proper" path for a dedicated integration.
- **C. An access token you already have** — if you already hold a valid session/access token and
  instance URL from another tool (e.g. Workbench, a browser session, another integration), just
  paste them. Advanced; skip unless the user brings this up.

Ask the user which they'd like. If they're unsure, steer them to **A**.

---

## Part 3 — Get the credential

Do the walkthrough for whichever method they picked.

### Method A — Salesforce CLI (recommended)

1. **Install the Salesforce CLI** if it isn't already there. Check with `sf --version`. To install:
   - download the installer from **https://developer.salesforce.com/tools/salesforcecli**, or
   - if they have Node: `npm install -g @salesforce/cli`.
2. **Log in through the browser.** Have the user run:
   ```
   sf org login web
   ```
   This opens their default browser to the Salesforce login page. They log in with their normal
   Salesforce username/password (and MFA if enabled) and click **Allow** to authorize the CLI. The
   browser shows a success page; the terminal confirms the org is authorized.
   - If their org uses a custom login domain, point them at:
     `sf org login web --instance-url https://mycompany.my.salesforce.com`
3. **Print the token and instance URL.** Have the user run:
   ```
   sf org display --json
   ```
   In the JSON output, the two values you need are under `result`:
   - `result.accessToken` → the **access token**
   - `result.instanceUrl` → the **instance URL**

   (`sf org display` without `--json` prints them in a table as **Access Token** and **Instance
   Url** if the user prefers reading it that way.)
4. Hand you (or you read) those two values and move to Part 4.

### Method B — Connected App OAuth flow

Set up a Connected App once, then mint a token from it.

1. In Salesforce, click the **gear icon** (top right) → **Setup**.
2. In the left-hand **Quick Find** box, type `App Manager` and click **App Manager**.
3. Click **New Connected App** (top right). If prompted to choose, pick the classic **Create a
   Connected App** option (not "Create an External Client App").
4. Fill in the basics: **Connected App Name**, **API Name** (auto-fills), and **Contact Email**.
5. Check **Enable OAuth Settings**.
6. Set a **Callback URL**. If you're going to mint the token with a local script or the CLI, a
   standard value is `http://localhost:1717/OauthRedirect` (the Salesforce CLI's default) or
   `https://login.salesforce.com/services/oauth2/callback`.
7. Under **Selected OAuth Scopes**, add at least:
   - **Manage user data via APIs (api)**, and
   - **Perform requests at any time (refresh_token, offline_access)**.
   Move them to the **Selected** column.
8. Click **Save**, then **Continue**. It can take a few minutes for the app to become active.
9. On the app's page click **Manage Consumer Details** (or the copy icons) to get the **Consumer
   Key** and **Consumer Secret** — these identify the app in the OAuth flow.
10. Mint an access token against that app. The simplest route is still the CLI pointed at the app:
    `sf org login web --client-id <ConsumerKey>` and then `sf org display --json` (as in Method A).
    Alternatively run a standard OAuth **web-server** or **username-password** flow against
    `https://login.salesforce.com/services/oauth2/token` using the Consumer Key/Secret; the
    response's `access_token` and `instance_url` are the two values bean needs.

Whichever way you mint it, you end with an **access token** and an **instance URL** — continue to
Part 4.

### Method C — An existing token you already have

If the user already has a valid access token and instance URL (from Workbench, a browser dev-tools
session, or another integration), just collect those two values directly. No new setup. Note the
token still expires like any other — Part 2's re-auth caveat applies.

---

## Part 4 — Connect, scope, sync

You now have an **access token** and an **instance URL**. Finish the connection.

### 1. Authenticate

The command is:

```
bean.py auth salesforce --token <access-token> --url https://YOURDOMAIN.my.salesforce.com
```

Offer the user the way that fits their comfort with sharing the token. Access tokens are sensitive:

- **You run it** — paste the token into the command above and run it via Bash. Easiest.
- **They run it** — hand the user the exact line to paste into their own terminal, so the token
  never passes through you.
- **Write the credential file** — run `bean.py init` to see the credential file path, then write
  `{"token": "<access-token>", "url": "https://YOURDOMAIN.my.salesforce.com"}` to it directly.

On success bean verifies the token and prints `✓ Salesforce connected`.

### 2. Set the scope

Apply the choice from Part 1:

```
bean.py scope salesforce global
```

(or `local`).

### 3. Sync

```
bean.py sync salesforce
```

This indexes the org and builds the search index.

### 4. Confirm

```
bean.py status
bean.py search "<topic>" --source salesforce
```

Pick a `<topic>` you'd expect to appear (a product name, an error message, a customer). If results
come back, it's working.

---

## What gets indexed

Once connected, bean indexes **both** Salesforce **Knowledge articles** and **Cases** via SOQL —
you don't track anything, there are no channels/repos/projects to add. It's a whole-collection
source: it indexes everything the token can see, re-embeds records when they change
(`LastModifiedDate` advances), and never prunes.

- You do **not** need to add anything for a normal setup. Just connect and sync.
- `salesforce:articles` or `salesforce:cases` in the `objects` list only **narrows** the sync to
  one of the two. Leave it empty to get both. (Use `bean.py add salesforce salesforce:articles` if
  the user explicitly wants Knowledge only, for example.)
- There's **no first-sync lookback window** — the whole collection is indexed regardless of age.

Remember Part 2: when a future sync fails with an auth error, the token expired — get a fresh one
and re-run the `bean.py auth salesforce …` line with the same `--url`.
