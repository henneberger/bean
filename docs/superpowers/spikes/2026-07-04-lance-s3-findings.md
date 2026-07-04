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

## S3 conditional-write concurrency — PENDING ⏳

Blocked on AWS SSO (session expired at spike time). Steps 3–5 of Task 0 to run once `aws sso login` refreshes:

1. `aws sts get-caller-identity` → confirm account/region with the user.
2. `aws s3 mb s3://bean-cloud-spike-<ts> --region <region>`.
3. Two independent `lancedb.connect(s3_uri, storage_options=...)` handles: racing appends must both survive; racing same-key `merge_insert` — record the conflict exception type + the re-open/re-apply retry recipe.
4. If this Lance version can't do lock-free S3 commits, record the min version and bump `pyproject.toml`.
5. `aws s3 rb s3://… --force`.

Phase 1 does not depend on this; Phases 2–4 do. Update this section before starting Phase 2.
