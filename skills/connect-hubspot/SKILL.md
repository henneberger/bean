---
name: connect-hubspot
description: Complete click-by-click setup for connecting HubSpot to bean — pick scope, create a private-app access token with CRM + Knowledge Base read scopes, authenticate, and sync tickets, CRM notes, and knowledge-base articles. Use when the user wants to connect or add HubSpot as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided HubSpot setup)
allowed-tools: Bash
---

# Connect HubSpot to bean

Walk the user through connecting **HubSpot** end to end. Run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

(shortened to `bean.py` below). Work the four steps in order. Steps 1–2 are quick questions; step 3 is a full walkthrough of the HubSpot UI you can read aloud to the user while they click; step 4 is the commands you run.

## 1. Scope — global or local?

Ask: **"Do you want to search HubSpot from every repo (global) or just this one (local)?"**

- **global** — one shared index, searchable from any repo on this machine.
- **local** — scoped to the current repo only.

Default to **global**. HubSpot is company-wide CRM content — support tickets, sales notes, and help-center articles — that you'll want to search from anywhere. Choose **local** only if this is a throwaway or repo-specific setup. You'll apply the choice in step 4.

## 2. Connection method

There is **one** method: a **HubSpot private-app access token**. The token is a string that starts with `pat-` (for example `pat-na1-xxxxxxxx-…`) and is sent as a Bearer credential on every API call.

Explain to the user: a **private app** is HubSpot's modern replacement for the old, deprecated **API keys**. Instead of one all-powerful account key, a private app is an app you create inside your own HubSpot account that is granted **exactly the scopes you check** — nothing more. The access token it issues carries only those permissions, so bean can read the data it indexes and nothing else. There is no OAuth redirect and no browser dance; you create the app once and copy its token.

bean uses the token to read three collections: support **tickets**, CRM **notes** (engagements), and **knowledge-base articles**. So the app needs **read** access to all three.

## 3. Get the credential

The user does this part in their browser. Read it to them step by step. **You must be a HubSpot super admin** to create a private app — if the user isn't, they'll need an admin to do this or grant them the role first.

1. Sign in to HubSpot at **https://app.hubspot.com**.
2. In the top-right corner, click the **Settings** icon (the **gear** ⚙︎). This opens account settings.
3. In the **left sidebar**, click **Integrations** to expand it, then click **Private Apps**.
   (In some accounts this is labeled under **Integrations → Private Apps**; if the user can't find it, the direct link is **https://app.hubspot.com/private-apps**.)
4. Click **Create a private app** (top right).
5. On the **Basic Info** tab, enter a **Name** — for example `bean knowledge search`. A description and logo are optional; leave them or fill them in.
6. Click the **Scopes** tab. This is a searchable checklist of permissions, each with **Read** and **Write** checkboxes. bean only needs **Read**. Use the **search box** at the top to find each scope and tick its **Read** box:
   - **Tickets** — search `tickets` and enable **Read** on the support **Tickets** scope. Covers support tickets.
   - **Notes / engagements** — search `notes` (or `engagements`) and enable **Read** on the CRM notes/engagements scope. This is what lets bean read CRM note bodies. If a distinct notes scope isn't offered in this account, enable **Read** on the broader **CRM** object read scope that covers contacts and engagements.
   - **Knowledge Base** — search `knowledge` or `content` and enable **Read** on the **Knowledge Base** / CMS content scope. Covers help-center articles.

   Don't worry about matching an exact scope slug — HubSpot renames these occasionally. The rule is: **Read** access covering **tickets**, **notes/engagements**, and **knowledge base**. When in doubt, check the **Read** box on each of those three areas; the token grants exactly what you check.
7. Click **Create app** (top right).
8. A dialog explains the token grants the scopes you selected. Confirm it (**Continue creating** / **Create**).
9. The app's **Access token** is now shown. Click **Show token**, then **Copy**. It starts with `pat-`.

Ask the user to paste the token to you (or, for privacy, keep it and use the hand-off option in step 4).

If the account **doesn't have the Knowledge Base product**, that's fine — you can skip that scope, and bean tolerates a missing knowledge base and simply skips articles.

## 4. Connect, scope, sync

1. **Authenticate.** The command is:

   ```
   bean.py auth hubspot --token <private-app-token>
   ```

   Offer the user three ways to run it, so the token never has to pass through chat if they'd rather it didn't:
   - **You run it** — paste the token into the command above and run it via Bash.
   - **They run it** — hand them the exact line to run in their own terminal.
   - **Write the credential file** — run `bean.py init` to print the credential file path, then write `{"token": "pat-…"}` to that path directly.

   On success bean confirms the connected **portal** id.

2. **Set the scope** from step 1:

   ```
   bean.py scope hubspot global    # or: local
   ```

3. **Sync** to build the index:

   ```
   bean.py sync hubspot
   ```

   This indexes **all three collections — support tickets, CRM notes, and knowledge-base articles** — automatically, as soon as the source is connected. There's nothing to "track" or add first, and there's no first-sync lookback window: bean re-observes the whole collection each run.

   Optionally, to **narrow** what's indexed to specific kinds, add one or more of `hubspot:tickets`, `hubspot:notes`, or `hubspot:kb`:

   ```
   bean.py add hubspot:tickets    # index only tickets, for example
   ```

   These refs only *restrict* the set — omit them and everything syncs.

## Confirm

Check it worked:

```
bean.py status
```

Then run a real search against the source:

```
bean.py search "<topic>" --source hubspot
```

Pick a topic the user knows exists in their tickets, notes, or help center. If results come back, HubSpot is connected and indexed.
