# bean — Cloud Mode (shared object-storage sync)

## Context

Today bean is a strictly local tool: each repo workspace lives at `~/.bean/<slug>-<hash>/`
with a DuckDB catalog (`bean.duckdb`), a LanceDB vector store (`lance/`), and `config.json`.
Fetching + embedding is the expensive part (CPU, model download, API calls). There is no way to
back up an index, roam it between machines, or share it across a team — and every machine that
wants the same docs pays the full fetch+embed cost independently.

**Goal:** add an opt-in "cloud mode" where writers upload their index to a shared object-storage
bucket and, on `sync`, **pull from the bucket first** so services (Google/Slack/…) are only hit for
what's genuinely new. Everything stays **local-first** — queries always run against local
DuckDB+Lance; the bucket is a sync/backup/sharing layer. Auth/connector tokens are **never**
uploaded. The store is **content-addressed and immutable** (CAS in S3), supports **multiple
writers** (a team) with no locking, and has a **cron-schedulable maintenance** job for compaction +
garbage collection.

**Decisions (confirmed with user):**
- **Hand-rolled CAS + per-writer manifests**, not Iceberg. (DuckDB iceberg writes are experimental;
  Iceberg needs a central catalog/server which breaks local-first; vectors can't live in Iceberg so
  Lance stays separate regardless. CAS gives immutability + dedup + crash-safety with no catalog and
  no locking.)
- **Team sharing:** one bucket shared by multiple users/machines. All bucket-credentialed writers
  are trusted; tombstones apply across the team. Per-user access-scoping is out of v1.
- **Rely on bucket encryption** (server-side). Blobs are gzipped plaintext; dedup stays simple.
- **v1 includes maintenance** (compaction, grace-period GC, lease).

---

## Architecture

### Bucket layout (all under a per-workspace `prefix`)
```
blobs/<body_sha256>                          # immutable body blob (gzip JSON) — source of truth
embeddings/<body_sha256>.<model>.<chunkfp>   # immutable vector sidecar (Parquet) — cache
manifests/<writer_id>/<seq>.jsonl.gz         # per-writer append-only delta logs
manifests/_compacted/<seq>.jsonl.gz          # merged snapshot (written by maintenance)
locks/maintain.json                          # maintenance lease
```
Content-addressing means two writers uploading identical content write identical bytes to the same
key — **no conflict, no locking, automatic dedup**. Writers only ever create new files under their
own `manifests/<writer_id>/` prefix, never mutating anyone else's.

### CAS object format
**Body blob** `blobs/<body_sha256>` (gzip JSON), keyed by a strong sha256 of the canonical bytes:
```json
{ "v":1, "source","doc_id","title","url","revision_id",
  "body","body_hash",                                  // body_hash = existing sha1(body)[:16] change token
  "created_at","modified_at","author","mime" }
```
`body_sha256` (the key) is used for **verification** — re-hash on download, reject on mismatch. This
is the partial-upload defense. Keep the existing `content_hash` (`store.py:41`) as the cheap change
token *inside* the blob; do not key the store on the 64-bit truncated hash.

**Embedding sidecar** `embeddings/<body_sha256>.<model>.<chunkfp>` (Parquet): columns
`id, start, end, text, vector`. `model` = slugified `embedding.model`; `chunkfp` = short hash of the
resolved `chunking` dict. Splitting body from embeddings means bodies dedup across model/chunk
changes, and writers on different models simply keep separate sidecars.

### Read-back reuses existing methods
On applying a pulled entry:
1. Download+verify body blob → `store.upsert(source, doc_id, title=…, url=…, revision_id=…,
   body=…, meta={created_at,modified_at,author,mime}, origin="remote")`. The existing hash-gate
   means an unchanged body is a metadata-only update and skips re-embedding.
2. If content changed **and** the local-compatible sidecar `embeddings/<hash>.<localmodel>.<localchunkfp>`
   exists → download it, feed straight into `index.reindex_doc(...)` + `store.replace_chunks(...)`.
   **No embedding computed.**
3. Else (sidecar embedded with a different model/chunk config than local) → fall back to the existing
   local `_embed_rows(...)` path (`sync.py:16`): re-chunk with local config, embed with local model.
   Correct by construction; wrong-dimension vectors never reach Lance.

---

## Prerequisite correctness fix — document provenance (must ship in v1)

**Verified bug:** `notion.py:124` and `localfiles.py:91` prune *any store doc not seen this fetch*;
`github.py:92` and `gdocs.py:139` prune by tracked-set; `gdocs.py:138` re-stats *every* store doc
against Google. So once PULL injects a teammate's docs into the shared store, the FETCH phase would
**tombstone them** (and hammer Google for docs this machine can't access).

**Fix:** add an origin marker so each writer prunes only what it manages.
- `bean/store.py` — `ALTER TABLE documents ADD COLUMN IF NOT EXISTS origin TEXT DEFAULT 'local'`
  (mirrors the existing metadata-column migration at `store.py:61-67`). `upsert(..., origin='local')`
  default; pull writes `origin='remote'`. A remote doc that this machine later fetches itself is
  **promoted** to `'local'`.
- `store.doc_ids(source, origin=None)` — filter by origin.
- Change the four prune sites + `gdocs.py:138` retain-list to use `store.doc_ids(src, origin='local')`.
- Pulled tombstones are still allowed to delete remote docs (guarded delete in PULL), so a teammate's
  delete propagates.

---

## Sync flow (`bean/cloud.py` + `run_sync` hooks)

New module **`bean/cloud.py`**:
```python
def enabled(ws) -> bool                       # cloud block present in config?
def make_fs(ws)                               # -> (fsspec fs, prefix); creds via auth/AWS chain; lazy import s3fs
def writer_id(store) -> str                   # state["cloud.writer_id"], uuid4 on first use
def pull(ws, store, *, fs, prefix, embed_fn, chunk_cfg, model, chunkfp, log) -> dict
def push(ws, store, *, fs, prefix, writer_id, changed, removed, model, chunkfp, log) -> dict
def maintain(ws, *, fs, prefix, grace_days=7, now=None, log) -> dict
# helpers: _blob_key/_emb_key/_manifest_key, _put_verified, _read_manifests, _merge
```

**`run_sync` (`sync.py:31`)** gains `cloud=True`; inside the existing `with Store(ws)` block:
1. **PULL** (if `cloud.enabled`): read all writers' manifests, `_merge` by `(source,doc_id)` →
   winner = max `modified_at` (source's own timestamp → skew-immune; tiebreak `wall_ts,writer_id,seq`;
   ties favor *keep* over tombstone). Download missing objects, apply per read-back above.
2. **FETCH**: existing per-source loop, unchanged — now change-detection skips anything PULL made current.
3. **PUSH** (if `cloud.enabled`): for each doc in `changed`, upload body blob (skip if `fs.exists`)
   + sidecar; write one new manifest delta file listing `changed+removed`.

Return dict gains `"pulled"` / `"pushed"`. `run_sync` default `cloud=True`, but `cloud.enabled(ws)`
is False with no `cloud` config block → **every existing offline test is untouched.** `cmd_sync`
gains `--no-cloud`.

### Manifest delta entry
```json
{ "source","doc_id","body_hash","body_sha256","revision_id",
  "modified_at","deleted":false,"writer_id","seq","wall_ts" }
```
Bootstrap (fresh machine) = the same merge from an empty local store; no special case.

---

## Maintenance (`bean cloud maintain`, cron-safe, idempotent)

1. **Acquire lease** `locks/maintain.json = {writer_id, expires_at}` — S3 conditional PUT
   (`If-None-Match: *`) where supported, else lease-with-expiry + re-read race check. Idempotent +
   grace-GC make a rare double-run harmless.
2. **Compact manifests:** read all deltas, run `_merge`, write `manifests/_compacted/<seq>.jsonl.gz`
   (winner set), delete superseded per-writer deltas. Readers union `_compacted/*` with any per-writer
   deltas newer than the snapshot high-water seq.
3. **GC orphans:** `referenced = {body_sha256 + emb keys of live winners}`. **Re-list manifests
   immediately before deleting.** Delete `blobs/`+`embeddings/` objects that are unreferenced **and**
   older than `grace_days` (grace prevents deleting an object a concurrent writer just uploaded).
4. **Prune tombstones** older than a retention window from the compacted snapshot.
5. **Release lease.**

Cron: user schedules `bean cloud maintain` on one machine (hourly/daily). Safe to run on several —
lease serializes, grace-GC tolerates the gap. bean does not build a scheduler; the command is just
cron-friendly (exit codes, `--json`).

---

## Config / auth
- `bean/config.py` — `DEFAULTS["cloud"] = {"enabled": False, "endpoint":"", "bucket":"", "prefix":"",
  "region":"", "grace_days":7}` so `bean config` documents it. **No secrets here.**
- Credentials: `bean auth cloud --token …` (or access-key/secret) → `~/.bean/credentials/cloud.json`
  (0600) via the existing `save_credential`; else fall back to the standard AWS credential chain.
- `writer_id` persisted in local `state` (`state["cloud.writer_id"]`).

---

## Files to create / modify
**Create**
- `bean/cloud.py` — `enabled/make_fs/writer_id/pull/push/maintain` + serialization/key helpers +
  `_put_verified` (tmp→hash-verify→atomic).
- Tests: extend `tests/test_bean.py` (see below).

**Modify**
- `bean/store.py` — `origin` column + migration; `upsert(..., origin=)`; `doc_ids(source, origin=)`;
  a promote/`set_origin` helper.
- `bean/sync.py` — PULL/PUSH hooks + `cloud` param in `run_sync`; thread `model`/`chunkfp`.
- `bean/gdocs.py` (138,139), `bean/notion.py` (124), `bean/localfiles.py` (91), `bean/github.py` (92)
  — prune/retain against `origin='local'`. **Correctness-critical.**
- `bean/cli.py` — `bean cloud {enable,status,maintain}` subparser + `cmd_cloud`; `bean auth cloud`
  (extend `AUTH`); `--no-cloud` on `sync`; register via existing `set_defaults(fn=…)` pattern.
- `bean/config.py` — `DEFAULTS["cloud"]` block.
- `pyproject.toml` — optional `[cloud]` extra: `s3fs`.

---

## Dependencies
- **Zero new deps for offline mode + Parquet sidecars:** `fsspec`, `pyarrow`, `numpy` are already
  transitive via `lancedb`. Local `file://`/`memory://` cloud runs need nothing new.
- **New optional extra `[cloud]`:** `s3fs` (real S3/R2/MinIO/B2). Import lazily inside
  `cloud.make_fs`. GCS/Azure (`gcsfs`/`adlfs`) later.

---

## Verification

**Offline tests** (mirror the existing fake-`fetch`/`fake_embed` pattern; back the object store with
fsspec `memory://` — deterministic, no network):
1. **Roaming:** `wsA`,`wsB` in separate BEAN_HOMEs sharing one `memory://` prefix. Sync A (push) →
   sync B (pull) with `fake_embed`+`gfetch`. Assert B sees A's doc **without** re-calling `gfetch`
   for it, and B's prune does **not** delete A's doc (the §provenance regression test).
2. **Model mismatch:** A pushes 64-dim `fake_embed`; B has a different `chunkfp` → assert B
   re-embeds locally (sidecar miss) and Lance stays consistent.
3. **Tombstone/race:** A deletes + B has newer re-add → assert keep; A deletes + B stale → assert
   delete applied.
4. **Maintain:** orphan blob older than grace deleted; referenced blob kept; unreferenced-but-fresh
   blob kept (inject `now`).
5. **Crash sim:** push objects but skip manifest append → next pull is a no-op; GC later reclaims.

Run: `/Users/henneberger/bean/.venv/bin/python /Users/henneberger/bean/tests/test_bean.py`
(current baseline: 81/81).

**Live smoke** (real bucket, after `bean auth cloud` + `bean cloud enable`): on machine A
`bean sync` then inspect bucket has `blobs/` + `manifests/<A>/`; wipe a second workspace, `bean sync`,
confirm docs reconstruct locally **without** re-fetching from Google (log shows pulls, not fetches);
`bean cloud maintain --json` compacts and reports GC counts.

---

## Correctness risks (addressed)
- **Cross-writer prune wipeout** → provenance column, local-only prune. *Blocker, in v1.*
- **Partial upload** → content-addressed; reader re-hashes and rejects; `_put_verified` tmp→verify.
- **Crash between object + manifest** → order objects first, manifest last; orphan reclaimed by GC.
  A manifest referencing a missing object → pull logs + falls back to local fetch for that doc.
- **Non-atomic Lance+DuckDB write** (pre-existing, `_embed_rows`) → on apply, order Lance → chunks →
  documents(origin) last, so a crash leaves the doc looking un-applied → re-pulled (idempotent).
  Follow-up: a `reconcile` that rebuilds Lance for any `documents` row lacking chunks.
- **Clock skew** → primary axis is the source's own `modified_at` (identical everywhere); skew only
  touches the last-resort tiebreak.
- **Delete races re-add** → tombstone must strictly out-rank; ties keep; a locally re-added doc is
  `origin='local'` and re-pushed same sync.

## Out of scope (follow-ups)
Per-user access-scoping/attribution; client-side encryption (convergent); selective per-source
cloud sharing; GCS/Azure backends; conditional-PUT lease everywhere; the `reconcile` command.
