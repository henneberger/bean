---
name: connect-github
description: Complete click-by-click setup for connecting GitHub to bean — pick scope, create a fine-grained or classic personal access token, authenticate, track repos, and sync issues and PRs. Use when the user wants to connect or add GitHub as a bean source.
version: 0.1.0
user-invocable: true
argument-hint: (guided GitHub setup)
allowed-tools: Bash
---

# Connect GitHub to bean

Guide the user through connecting **GitHub** as a knowledge source. bean indexes the **issues and pull requests** (title, body, and comments) of the repos you track, so you can search them locally.

You (the assistant) run every bean command via Bash:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bean.py <cmd>
```

Written as `bean.py <cmd>` below. Work the four steps in order. This is a walkthrough — be thorough, use exact button names, and confirm each step before moving on.

---

## 1. Scope — global or local?

Decide where the GitHub index lives before authenticating.

- **local** (default for GitHub) — the index belongs to the current repo/directory only. A GitHub repo is usually tied to one project, so its issues and PRs are most relevant while you're working in that project.
- **global** — one shared index, searchable from any directory on this machine. Choose this if you want to search a repo's issues/PRs from anywhere (e.g. an org-wide tracker, or a repo you reference across many projects).

Ask the user: **"Track this GitHub repo just for the current project (local), or make it searchable everywhere (global)?"** Default to **local** unless they say otherwise. You'll set the scope in step 4.

---

## 2. Connection method

bean authenticates with a GitHub **personal access token (PAT)**. Any token that grants **read access to the target repos' issues and pull requests** works. There are two kinds of PAT — both are accepted by the same `bean.py auth github --token <token>` command:

- **(A) Fine-grained personal access token** *(recommended)* — scoped to specific repositories and specific read-only permissions. Least privilege: the token can only read what bean needs. Prefix `github_pat_…`.
- **(B) Classic personal access token** — coarser, scope-based. Simpler to create but grants broader access (the `repo` scope covers all your private repos). Prefix `ghp_…`.

Recommend **(A) fine-grained** unless the user prefers the classic flow. Ask which they want, then walk them through the matching section in step 3.

Note: GitHub has **no auto-indexing** — bean only indexes repos you explicitly track. You must add at least one repo (step 4) or a sync does nothing. There is no first-sync lookback window; the first sync pulls all issues/PRs, then incremental syncs pull only what changed.

---

## 3. Get the credential

### (A) Fine-grained personal access token — recommended

1. Go to **https://github.com/settings/personal-access-tokens/new** (GitHub → your profile photo → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**). Sign in if prompted.
2. Under **Token name**, enter a name, e.g. `bean`.
3. Under **Expiration**, pick an expiry. Shorter is safer; you'll re-run `bean.py auth github` with a fresh token when it expires. (Optionally add a **Description**.)
4. Under **Resource owner**, select the account or organization that owns the repos you want to index. If it's an org repo, the org must allow fine-grained tokens (an org admin may need to approve the token afterward).
5. Under **Repository access**, choose:
   - **Only select repositories** — then pick the exact repos you want bean to index (recommended, least privilege), **or**
   - **All repositories** — every repo the resource owner has.
6. Expand **Permissions → Repository permissions** and set:
   - **Contents** → **Read-only**
   - **Issues** → **Read-only**
   - **Pull requests** → **Read-only**
   - **Metadata** → **Read-only** (this is selected automatically and is required; leave it on).
   Leave every other permission at **No access**.
7. Click **Generate token**.
8. Copy the token — it starts with `github_pat_…` and is **shown only once**. If the resource owner is an org that requires approval, the token won't work until an admin approves it.

Ask the user to paste the `github_pat_…` string.

### (B) Classic personal access token

1. Go to **https://github.com/settings/tokens/new** (GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)** → **Generate new token** → **Generate new token (classic)**). Sign in if prompted.
2. In the **Note** field, enter a name, e.g. `bean`.
3. Under **Expiration**, pick an expiry.
4. Under **Select scopes**, choose the minimum that covers your repos:
   - **repo** — full control of private repositories. Required to read **private** repos' issues and PRs. (This scope is broad; if you only need public repos, use the next option instead.)
   - **public_repo** — read/write access to **public** repositories only. Enough if every repo you'll track is public.
5. Click **Generate token**.
6. Copy the token — it starts with `ghp_…` and is **shown only once**.

Ask the user to paste the `ghp_…` string.

---

## 4. Connect, scope, sync

1. **Authenticate.** The same command works for both token types:
   ```
   bean.py auth github --token <token>
   ```
   On success it prints `✓ GitHub connected as <login>.` (it calls the GitHub API to resolve the token's username).

   If the user is privacy-minded and doesn't want to paste the token into the chat, offer either:
   - hand them the exact line above to run in their own terminal, or
   - run `bean.py init` to get the credential file path, then write the credential JSON there directly:
     ```json
     {"token": "<token>"}
     ```

2. **Set the scope** from step 1:
   ```
   bean.py scope github global
   ```
   or
   ```
   bean.py scope github local
   ```

3. **Track at least one repo.** Run `bean.py init` to find the GitHub config block, and add each repo to the github **`repos`** list. Each entry is a repo as **`owner/name`** (e.g. `octocat/Hello-World`) **or** a full **github.com URL** (e.g. `https://github.com/octocat/Hello-World`). Add every repo the user wants indexed. Removing a repo from this list later prunes everything indexed under it on the next sync.

4. **Build the index.**
   ```
   bean.py sync github
   ```
   This fetches every issue and pull request for each tracked repo — title, body, and comments — and embeds them. bean keeps a per-repo `github.since` cursor, so later syncs only pull items that changed (nothing is re-fetched or re-embedded unnecessarily).

5. **Confirm.**
   ```
   bean.py status
   ```
   then a test query:
   ```
   bean.py search "<topic>" --source github
   ```
   Pick a `<topic>` you know appears in one of the tracked repos' issues or PRs to verify results come back.
