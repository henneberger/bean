# bean cloud — S3 storage backend (subsystem #1)

**Status:** design, approved to plan
**Date:** 2026-07-04
**Scope:** the shared-storage foundation only. Cloud automation (Lambda poller, EventBridge cron,
scheduled Lance compaction) is **subsystem #2** and gets its own spec; this spec only guarantees an
on-disk format that #2 can build on.

## Goal

Let a bean index live on S3 as the shared source of truth, so:

- multiple **writers** (laptops, a work desktop, later a cloud job) can `bean sync` into one index;
- any machine can be a **consumer** — point it at the bucket and query, without running the
  indexing/embedding pipeline;
- a local `bean sync` picks up new material immediately;
- everything stays consistent under concurrent writers.

Local-only mode (today's behaviour) stays the **default and is untouched**. Cloud is opt-in per
workspace.

## Decisions (settled during brainstorming)

1. **All shared data is Lance.** Four Lance datasets hold the shared index: `documents`,
   `revisions`, `edges`, `chunks`. No Iceberg, no Delta, no pyiceberg. One storage technology, one
   commit protocol. (Chunks are already Lance; this extends the format to the three relational
   tables that currently live in the local DuckDB file.)
2. **No DynamoDB / no catalog service.** Multi-writer safety rides **S3 conditional writes**
   (`If-None-Match` / put-if-absent) via Lance's lock-free commit path. Serverless; the only cloud
   resource is the bucket.
3. **Full local replica; all reads are local.** Every machine mirrors all four datasets under
   `~/.bean/<repo>/`. Queries never read S3 mid-request — DuckDB and lancedb run over the local
   mirror. `cache_httpfs` is unnecessary (full replication beats partial block caching here).
   Because Lance datasets are versioned with immutable fragments, keeping the mirror current is a
   fast-forward file copy (`bean pull`).
4. **DuckDB stays — read-only, off the write path.** SQL is a required feature (`bean sql`), so
   DuckDB remains as the read/SQL engine: it registers the four local Lance datasets as views and
   serves every existing retrieval query plus arbitrary `bean sql`. Writes never go through DuckDB.
   Retrieval SQL is essentially unchanged (today it already runs SQL over a registered Lance chunk
   dataset); the three relational tables just become registered Lance datasets too. Vector search
   stays on lancedb.
5. **Immutable writes only.** Every mutation is a new Lance version, never an in-place edit:
   upsert → `merge_insert`, delete → deletion vector, revisions → append. This is the multi-writer
   safety mechanism: nobody overwrites bytes, so a racing commit fails the conditional write and the
   writer re-pulls to the latest version and re-applies its idempotent op.
6. **Only write after embed — no `embedded_hash`.** A document lands in the shared store *only once
   its chunks are embedded*, so "exists in the shared store" means "embedded" by construction. The
   `embedded_hash` column and the whole embed-checkpoint / `embed_queue` machinery are **removed**.
   `documents.hash` is the sole change authority. Resumability comes from private cursors (advanced
   only after commit) plus content-hash idempotency.
7. **Private plane stays local, never shared.** Sync cursors and credentials are per-writer bookkeeping
   (they encode each writer's *visibility*, which differs). They stay in a small local store.

## Architecture

### Two planes

**Shared plane** — authoritative, on S3, immutable, multi-writer. Four Lance datasets under
`s3://<bucket>/<prefix>/`:

| dataset | key | mutation | holds |
|---|---|---|---|
| `documents` | `(source, doc_id)` | `merge_insert` upsert | title, url, revision_id, **hash**, body, created_at, modified_at, author, mime, fetched_at |
| `revisions` | append-only | append | source, doc_id, revision_id, hash, fetched_at |
| `edges` | `(source, src_doc)` group | delete-predicate + add | source, src_doc, rel, dst_kind, dst |
| `chunks` | `(source, doc_id)` group | delete-predicate + add | id, source, doc_id, title, url, start, end, text, **ord**, vector |

Note: `documents` loses `embedded_hash` (removed) and `chunks` gains a stored **`ord`** column
(computed at embed time), which removes the `row_number() OVER (…)` window function from the read
path.

**Private plane** — per-writer, local under `~/.bean/<repo>/`, never uploaded:

- **sync cursors** (`slack.cursor.*`, `github.since.*`, `slack.users`, …) — the state that would
  corrupt across writers if shared. Stays in a small local store (a private DuckDB file or JSON;
  the existing `state` table, kept local).
- **credentials** — already local today; unchanged.
- **the local replica / mirror** of the four Lance datasets.

### Roles

- **Writer** — runs `bean sync`: `pull → fetch → embed → commit`. Needs source credentials.
- **Consumer** — runs `bean pull` + queries. No source credentials, no indexing pipeline. May still
  load the embedder to encode its *own queries* for semantic search (that's cheap and optional; a
  consumer *can* embed, it just doesn't *have to* run the document pipeline).

Both use the identical local read path.

### Coordination

Lance commits to S3 use conditional writes for atomicity. Concurrent writers:

- **append** (revisions) auto-rebases — no conflict.
- **`merge_insert` / delete** that races another commit fails the conditional write; the writer
  re-pulls to the new version and retries the op (bounded retries). Because upserts are keyed by
  `(source, doc_id)` and content is hash-identical between writers who fetched the same doc, the
  rebased result is the same.

**Implementation validation (call out in the plan):** confirm the pinned Lance version wires up S3
conditional-write commits for concurrent writers. Recent Lance does; if the pin lags, upgrading Lance
is the target. Do not ship assuming it — verify against a real (or moto/minio) S3 in a smoke test.

## Data flow

### Writer — `bean sync`

1. **Pull** — fast-forward the local mirror from S3 (copy immutable fragments/manifests newer than
   the local version).
2. **Fetch** — each active source fetches using its **private local cursor**, producing candidate
   doc snapshots (in-run staging, not persisted to the shared store yet).
3. **Diff** — a candidate is *changed* when the shared `documents` dataset has no row for
   `(source, doc_id)` or its stored `hash` differs. Unchanged docs are skipped (no embed, no commit;
   a metadata-only refresh may still update non-content fields, matching today's `upsert` returning
   `False`).
4. **Embed** — chunk + embed each changed doc; compute and store `ord` per chunk.
5. **Commit-together** — write `chunks` then `documents` (+ append `revisions`, replace `edges`)
   as immutable Lance commits. Post-embed only, so no un-embedded doc ever lands.
6. **Advance cursor** — the private cursor for that source advances **only after its commit
   succeeds**.

Deletions: coarse and config-driven (dropping a repo/channel/source from config removes its docs),
applied to the shared store as delete-predicate commits. See Limitations.

### Consumer — query

`pull` (auto, before the query) → DuckDB registers the four local datasets as views and serves
retrieval SQL / `bean sql`; lancedb serves vector search over the local `chunks` mirror. RRF fusion
in `search.py` is engine-agnostic and unchanged. **No S3 access during a query.**

### Consistency

The four datasets commit independently (no cross-dataset transaction). The only observable
intermediate state is a crash *between* the `chunks` commit and the `documents` commit, leaving a
momentary orphan (chunks with no doc row, or vice-versa). **Self-healing:** the cursor didn't
advance, so the next sync re-embeds and re-commits both (idempotent); maintenance (subsystem #2) can
sweep stragglers. Because docs are only written post-embed, there is no steady-state "doc without
chunks" — that state is gone with `embedded_hash`.

## Components (code seam)

- **`bean/remote.py`** (new) — S3↔local replication and provisioning:
  - `pull(ws)` — fast-forward the local mirror from S3 (immutable additive file copy).
  - commit helpers — perform Lance immutable commits against the S3-backed datasets with
    conditional-write concurrency + bounded retry-on-conflict.
  - `cloud_init(ws, bucket, prefix, region)` — provision a writer: create the datasets, upload any
    existing local index as the first commit (migration, below).
  - `cloud_connect(ws, bucket, prefix, region)` — provision a read-only consumer.
- **`store.py`** (refactor) — `documents` / `revisions` / `edges` move from DuckDB tables to Lance
  datasets. The `Store` **method surface is unchanged** (`get`, `upsert`, `delete`, `recent`,
  `find_docs`, `keyword_search`, `neighbors`, `related`, `counts`, `revisions`, `edges_of`, …).
  Under it:
  - **writes** become Lance ops (`merge_insert`, delete-predicate + add, append).
  - **reads** register all four local datasets into a DuckDB connection as views; existing SQL runs
    unchanged. `_BASE`'s window-function `ord` is replaced by the stored `ord` column.
  - `embed_queue` / `mark_embedded` / `embedded_hash` are **removed**; `run_sync` embeds the run's
    changed docs directly.
  - `state` (cursors) stays in a small **private local** store.
- **`index.py`** (small change) — Lance path resolves to the local mirror dir; `reindex_doc` writes
  `ord`; otherwise unchanged.
- **`workspace.py`** — `Workspace` learns cloud config (role, bucket, prefix, region), exposes the
  mirror dir and remote URIs. AWS credentials via the standard chain (env / `~/.aws` / IAM role),
  **never stored in bean config**.
- **`config.py`** — a `cloud` block:
  ```json
  "cloud": {
    "enabled": false,
    "role": "writer",
    "bucket": "",
    "prefix": "",
    "region": ""
  }
  ```
- **`sync.py`** — `run_sync` gains an implicit **pull** first (writer), drops the `embed_queue`
  indirection (embeds the run's changed set), and commits post-embed. Cursor advance moves to
  after-commit.
- **CLI (`cli.py`)**:
  - `bean cloud init --bucket … --prefix … --region …` — become a writer; provision + migrate.
  - `bean cloud connect --bucket … --prefix … [--region …]` — become a read-only consumer.
  - `bean pull` — fast-forward the local mirror.
  - `bean sync` — writer path (now pull → fetch → embed → commit).
  - `bean status` — show role (local / writer / consumer), bucket/prefix, local vs remote version,
    last sync / pull.
  - `bean sql` — unchanged surface, now over the registered Lance datasets.

## Error handling

- **Commit race** — conditional-write failure → re-pull to latest version → retry the idempotent op,
  bounded retries; surface a clear error if retries exhaust.
- **Partial / interrupted sync** — cursor advanced only post-commit; docs written only post-embed →
  next sync re-fetches from the last committed cursor and re-embeds. Idempotent, resumable.
- **Orphan across datasets** — self-healing on next sync (above); maintenance sweep in subsystem #2.
- **Network / S3 unavailable** — writer aborts cleanly with the mirror unchanged (or fast-forwarded);
  retry later. A consumer keeps serving its current mirror; `pull` retries.
- **AWS auth / permissions** — fail loudly with the missing capability (bucket read/write, conditional
  write support), consistent with bean's "fail loudly, don't silently degrade" stance.

## Testing (offline-first, as today)

- **"S3" is a local temp dir** — Lance takes local paths; the remote is a temp directory. Two
  "machines" are two mirror dirs replicating through that temp remote.
- **Multi-writer conflict tests** — simulate concurrent commits against the temp remote; assert
  conditional-write retry converges and results are identical regardless of writer order.
- **Idempotency / resume tests** — interrupt between chunks-commit and doc-commit; assert next sync
  heals with no duplicate or missing rows.
- **Injectable fake embedder** — unchanged; no model load in tests.
- **Retrieval parity** — the existing retrieval suite runs unchanged over Lance-backed datasets
  (behaviour must match the DuckDB-table version); `bean sql` over the registered datasets is
  covered.
- **Real-S3 smoke path** — optional (moto/minio), used to validate the conditional-write commit
  assumption; not required for the core suite.

## Migration

`bean cloud init` reads the existing local `bean.duckdb` (`documents` / `revisions` / `edges`) and
`lance/` chunks and writes them into the S3 Lance datasets as the initial commit (computing `ord`,
dropping `embedded_hash`). Opt-in per workspace; local-only users are unaffected. A workspace stays
local until `bean cloud init` / `bean cloud connect` is run.

## Limitations (v1)

- **Deletions are coarse and best-effort.** They are config-driven (drop a source/repo/channel →
  its docs removed) and applied globally. There is **no per-writer provenance**, so a writer that
  drops a source it shared with another writer removes docs the other still sees; re-adding the
  source re-fetches them. A provenance/tombstone model is **future work**.
- **No cross-dataset atomicity** — momentary orphans possible on crash, self-healing (above).
- **Full replication** — every machine stores the whole index locally (same as today's local mode).
  Appropriate at personal/team scale; not a design for very large corpora that can't fit a laptop.

## Explicitly out of scope (→ subsystem #2)

- Lambda source poller + embedder.
- EventBridge / cron scheduling.
- Scheduled Lance compaction / maintenance job (this spec only guarantees the immutable-commit
  format such a job can compact).
