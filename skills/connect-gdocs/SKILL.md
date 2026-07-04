---
name: connect-gdocs
description: Complete click-by-click setup for connecting Google Drive / Docs to bean — install the Google Cloud CLI, sign in through gcloud's browser flow (no token, nothing to configure in Google Cloud), set scope, and sync. Use when the user wants to connect or add Google Drive or Google Docs as a bean source.
version: 0.2.0
user-invocable: true
argument-hint: (guided Google Drive setup)
allowed-tools: Bash
---

# Connect Google Drive to bean

Walk the user through connecting **Google Drive** (Google Docs + PDFs). You run every bean command yourself via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

Shortened to `bean.py` below. Work the four parts in order and keep it a short back-and-forth — ask the scope question, get gcloud installed, run the sign-in, then sync.

## Naming quirk — read this first

Google Drive is the one source where the **auth provider name and the source key differ**. Use the right one for each command or the commands will fail:

- **Auth** uses the provider name **`google`**: `bean.py auth google`
- **Scope, config, sync, search** use the source key **`gdocs`**: `bean.py scope gdocs …`, `bean.py config set gdocs.…`, `bean.py sync gdocs`, `bean.py search "…" --source gdocs`

Rule of thumb: you sign in to **google**, but everything about the *documents* is **gdocs**.

## 1. Scope — global or local?

Ask: **"Search Google Drive from every repo (global) or just this one (local)?"**

- **global** — one shared index, searchable from any repo. **This is the default** — your Drive is a personal account you'll want to search everywhere.
- **local** — scoped to the current repo only. Use this if you want the index tied to one project.

You'll set this in part 4 with `bean.py scope gdocs global|local`.

## 2. Connection method — gcloud browser sign-in

There is exactly one method, and it's simple:

- **No token to paste.** You never copy an access token or client secret anywhere. The sign-in happens entirely in a browser window.
- **Nothing to set up in Google Cloud.** No GCP project, no OAuth consent screen, no API enablement. The `gcloud` CLI ships with Google's own pre-verified OAuth client, so signing in through gcloud is all the authorization bean needs.
- **One prerequisite:** the **Google Cloud CLI** (`gcloud`) must be installed on this machine. That's part 3.

Under the hood: `bean.py auth google` runs `gcloud auth login --enable-gdrive-access` (opens the browser), and each sync mints a short-lived Drive-scoped token via `gcloud auth print-access-token`. You don't touch any of that — it's automatic once you're signed in.

## 3. Get set up — install the Google Cloud CLI

First check whether gcloud is already installed:

```
gcloud --version
```

If that prints a version, skip to part 4. If it says "command not found," install it.

### macOS (Homebrew — easiest)

```
brew install --cask google-cloud-sdk
```

Then open a new terminal (so `PATH` picks up gcloud) and verify with `gcloud --version`.

### Any OS — official installer

Full instructions: **https://cloud.google.com/sdk/docs/install**. The gist:

- **macOS (no Homebrew):** download the archive for your chip from the install page, then:
  ```
  tar -xf google-cloud-cli-*.tar.gz
  ./google-cloud-sdk/install.sh
  ```
  Answer **Y** to add gcloud to your `PATH`. Restart your shell.
- **Linux (Debian/Ubuntu):** add Google's apt repo, then `sudo apt-get update && sudo apt-get install google-cloud-cli`.
- **Linux (RHEL/Fedora/CentOS):** add the `google-cloud-sdk` yum repo, then `sudo dnf install google-cloud-cli`.
- **Windows:** download and run **GoogleCloudSDKInstaller.exe** from the install page, then open a new command prompt.

After any install method, **open a fresh shell** and confirm:

```
gcloud --version
```

You do **not** need to run `gcloud init` or `gcloud config` — bean's auth step handles the sign-in. There is **no token to fetch**; the browser sign-in in part 4 is the credential.

## 4. Connect, scope, sync

1. **Sign in** (provider name is **google**):
   ```
   bean.py auth google
   ```
   This runs `gcloud auth login --enable-gdrive-access`, which opens a browser. Tell the user to:
   - Pick the **Google account** that can see the docs they want indexed.
   - Click **Allow** on the permission screen.

   Because it's an interactive browser flow, you (the assistant) run the command and then tell the user to complete the prompt in their browser — the token never passes through you. A privacy-minded user can instead run `bean.py auth google` themselves in their own terminal. When it finishes, bean prints `✓ Google connected`.

2. **Set scope** (source key is **gdocs**), using the choice from part 1:
   ```
   bean.py scope gdocs global
   ```
   (or `local`).

3. **First-sync lookback.** Ask: **"How many days of Drive history should the first sync backfill?"**
   - Default is **30 days**. `0` means **all time** (every doc you own, no date limit).
   - Only if they want something other than 30:
     ```
     bean.py config set gdocs.lookback_days <N>
     ```
   - The lookback only bounds discovery of *new* files on the first sync; after that a cursor tracks changes, and edits to already-indexed docs are always caught.

4. **Optional — skip Drive comments.** By default bean indexes each Drive comment as its own searchable entry (so "Eric's last comment on my launch doc" is answerable). To turn that off:
   ```
   bean.py config set gdocs.comments false
   ```

5. **Sync:**
   ```
   bean.py sync gdocs
   ```

6. **Confirm:**
   ```
   bean.py status
   bean.py search "<a topic you know is in your docs>" --source gdocs
   ```

## What gets indexed

- By default, the **Google Docs and PDFs you own** are auto-indexed (Docs exported to Markdown, PDFs run through OCR/text extraction). Each **Drive comment** is indexed as its own attributed, timestamped entry too (unless you disabled it in step 4).
- Adding a specific **Google Doc or Drive folder URL** only **narrows** the scope — it's optional and turns off the auto-index-everything behavior:
  ```
  bean.py add gdocs "<Google Doc or Drive folder URL>"
  ```
  Use this only if you want to index a specific shared doc/folder instead of your whole Drive.

## Known gotcha — sync fails on a blocked gcloud binary

If a sync fails because the sandbox blocks the `gcloud` binary (bean can't mint a token), re-run the sync with the **tool sandbox disabled**. The auth itself is fine — it's only the per-sync `gcloud auth print-access-token` call that the sandbox intercepts.
