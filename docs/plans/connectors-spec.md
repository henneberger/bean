# bean — Connector design principles

The connector contract, the shipped table, change detection, and routing live in
[../connectors.md](../connectors.md). This file is just the design rules a new connector follows —
the *why*, not a walkthrough.

## Setup is a conversation, not commands

The user never types `bean auth` or `bean add`. `/bean init` is a guided conversation: the
assistant asks for what it needs, runs every command itself, and the user only pastes a token (or
completes a browser sign-in for Google/Microsoft). A connector's job is to make that conversation
short — one credential, sensible defaults, everything readable synced unless the user narrows it.

## Auth: support both ways in, default to the one an individual can self-serve

Where a service offers more than one path, implement **both** and pick at auth time by which fields
arrive — but the default is always the path that **doesn't need an admin**:

- A personal token / app-password / user-scoped OAuth over an admin-provisioned app.
- Cloud Basic (email + token) vs Server/DC Bearer — decided by whether `--email` is present.
- A device-code public-client flow (or reusing an existing `az` / `gcloud` session) over a
  registered confidential app.

If a source *only* works with admin setup, it still ships — but the init copy says so up front.

## Everything else

- **Offline-testable.** All I/O through the injectable `fetch` seam and (for async polls) an
  injectable `sleep`; no direct `requests`. A connector that can't run against a fake is unfinished.
- **Change detection is mandatory.** A cheap `revision_id` so re-sync re-embeds only deltas; the
  content hash is the backstop. Stable doc ids that survive edits.
- **Prune by origin.** Deletion sweeps only touch docs this machine fetched
  (`origin='local'`), never docs a teammate injected via cloud sync — a prerequisite for
  [cloud-mode.md](cloud-mode.md).
- **One row to go live.** A connector is inert until its `Source(...)` row is in
  `bean/sources.py`; that row is the only integration seam — no source-specific code in `sync.py`,
  the CLI, storage, or retrieval.

## Done when

Contract callables implemented against a fake · both auth paths where the service has them,
individual-friendly default · change signal + stable ids · origin-scoped pruning · a row in the
shipped table with its auth/indexes/change-signal · offline tests for connect, sync, re-sync no-op,
prune, and `parse_add` routing.
