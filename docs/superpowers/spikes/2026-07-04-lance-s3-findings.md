# Spike findings — Lance-as-catalog + Lance-on-S3 (Task 0)

**Date:** 2026-07-04
**Env:** `lance` 8.0.0, `lancedb` 0.34.0, `duckdb` 1.5.4 (well past the `>=0.15` pin; no bump needed for the local mechanics — S3 concurrency version check pending).

## Local mechanics — CONFIRMED ✅

All Phase 1 signatures in the plan match the installed Lance. Verified end-to-end:

- **Upsert by composite key:**
  ```python
  (tbl.merge_insert(["source", "doc_id"])
      .when_matched_update_all().when_not_matched_insert_all()
      .execute(pa.Table.from_pylist(rows, schema=SCHEMA)))
  ```
- **Create empty typed table, then merge_insert into it** (fresh-catalog path): works. `db.create_table(name, schema=pa_schema)` then `merge_insert(...).execute(...)`.
- **Delete by predicate:** `tbl.delete("source = '…' AND doc_id IN ('…')")`.
- **DuckDB read views:** `con.register(name, tbl.to_lance())` then arbitrary SQL — confirmed for both populated and empty (schema-only) tables (`SELECT count(*)` returns 0, no error).
- **Timestamps:** `pa.timestamp("us")` column round-trips Python `datetime` and `None` correctly through merge_insert and DuckDB.

Implication: `bean/lancecat.py` (Task 1.1) can be written exactly as the plan shows.

## S3 conditional-write concurrency — CONFIRMED ✅

Ran against a throwaway bucket (`s3://bean-cloud-spike-386318010728`, us-east-1, account 386318010728; bucket torn down after). Lance **8.0.0** on S3, `storage_options={"region": "us-east-1"}`, credentials via env.

**Results (all with NO DynamoDB, NO commit lock, NO explicit retry):**

- **Racing appends** from two independent handles opened at the same base version → **both commit** (final row count reflects both). Appends auto-rebase.
- **Concurrent `merge_insert` on DISJOINT keys** (writer C upserts key X; stale writer D — which never saw X — upserts key Y) → **both survive** (`X` and `Y` both present). No lost update. This is the load-bearing multi-writer guarantee for bean, and it holds: `merge_insert` re-reads and rebases against the latest version at commit time.
- **Concurrent `merge_insert` on the SAME key** → **last-writer-wins**, no exception (acceptable per spec — same doc, idempotent-ish).

**Conclusion:** the architecture's core premise holds on Lance 8.0.0 + modern S3. Multi-writer commits are safe for bean's model with just `storage_options={"region": ...}` — no DynamoDB catalog/commit-store, no version bump. `merge_insert`/append auto-resolve conflicts, so the plan's `commit_with_retry` (re-open + re-apply) is a belt-and-suspenders wrapper for any op that *does* surface a `CommitConflict`, not a hard requirement for the common paths.

**Operational detail for Phase 2/CLI (important):** Lance's Rust object-store reads AWS credentials from the **environment / instance role**, and does **NOT** resolve `aws sso` cached profiles directly. For an SSO user it needed `eval "$(aws configure export-credentials --format env)"` first (sets `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`). So bean's cloud mode should rely on the standard AWS *env/instance-role* chain and, for SSO users, either export creds or document that requirement — not assume `~/.aws` SSO profiles just work. Record for Task 2.1/3.1.

**Caveat (final confirmation deferred to Task 4.2):** these tests were sequential-in-one-process, which proves `merge_insert`'s auto-rebase logic. True-simultaneity CAS (two OS processes racing the same next-version manifest) relies on Lance using S3 conditional writes under the hood; the real-S3 smoke in Task 4.2 (truly parallel processes) is the final confirmation. Premise is validated for Phase 2 to proceed.
