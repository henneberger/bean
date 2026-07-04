# bean cloud — S3 storage backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a bean index live on S3 as an immutable, multi-writer shared source of truth, with every machine holding a full local replica it reads from — while preserving today's local-only behavior and the `bean sql` feature.

**Architecture:** All shared data becomes four Lance datasets (`documents`, `revisions`, `edges`, `chunks`); DuckDB stays read-only over the local replica for retrieval SQL and `bean sql`; writes are immutable Lance commits; multi-writer safety rides S3 conditional writes; per-writer cursors/credentials stay local. See the spec: `docs/superpowers/specs/2026-07-04-cloud-s3-storage-backend-design.md`.

**Tech Stack:** Python ≥3.10, `pylance` (the `lance` module) + `lancedb`, DuckDB, `requests`; AWS S3 via Lance's object-store (`storage_options`); the bespoke offline test harness in `tests/test_bean.py`.

## Global Constraints

- **Python ≥ 3.10**, dependencies pinned in `pyproject.toml` (`lancedb>=0.15`, `pylance>=0.15`, `duckdb>=1.0`). Any Lance version bump needed for S3 conditional-write commits is raised in Task 0 and applied there.
- **Tests are offline and run via `make test`** (`.venv/bin/python tests/test_bean.py`). No network, no model load — HTTP is faked, the embedder is `fake_embed`. New tests append to `tests/test_bean.py` using the existing `ok(cond, msg)` / `Workspace(repo("…"))` idioms. The suite prints `bean: N/N checks passed` and exits non-zero on any failure.
- **No environment variables for config** — everything is a config value (`bean/config.py`). AWS credentials are the one exception and use the standard AWS chain (env / `~/.aws` / IAM role), never bean config.
- **Fail loudly, never silently degrade** — bean's house stance; new error paths raise actionable messages.
- **Dense idiom** — match the surrounding code's style (semicolon-joined statements, long lines to the 110 col limit, late imports). Lint gate: `ruff` with the `F,E4,E7,E9,W6` subset (`make check`).
- **Commit straight to `main`** (repo convention); no feature branches.
- **Local-only mode stays the default and behavior-identical.** Cloud is opt-in per workspace.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `bean/lancecat.py` | **NEW.** The Lance-backed catalog: the four datasets, their upsert/append/delete-predicate writes, and the "register all four into a DuckDB connection as views" read path. The storage substrate `Store` sits on. | create |
| `bean/store.py` | Keeps its public method surface; delegates storage to `lancecat`. Loses `embed_queue`/`mark_embedded`/`embedded_hash`. `state` (cursors) moves to a small private local store. | rewrite internals |
| `bean/index.py` | `reindex_doc` writes a stored `ord` column; Lance dir resolves to the local replica path. Vector `search` unchanged. | modify |
| `bean/sync.py` | `run_sync` drops the embed-queue indirection: embeds the run's changed set, commits post-embed (per source), advances cursor after commit; writer path adds an implicit `pull`. | modify |
| `bean/remote.py` | **NEW.** S3↔local replication (`pull`), S3 commit with conditional-write + retry, and `cloud_init`/`cloud_connect` provisioning. | create |
| `bean/workspace.py` | `Workspace` learns cloud config (role, bucket, prefix, region), exposes the replica dir + remote URIs. | modify |
| `bean/config.py` | Add the `cloud` defaults block. | modify |
| `bean/cli.py` | `bean cloud init` / `bean cloud connect` / `bean pull`; `bean status` cloud fields; `bean sync` implicit pull. | modify |
| `tests/test_bean.py` | Update embed-checkpoint tests to the new model; add Lance-catalog, migration, replication, and multi-writer tests. | modify |
| `docs/superpowers/spikes/2026-07-04-lance-s3-findings.md` | **NEW.** Task 0 output: verified Lance APIs + version + S3 conditional-write mechanics. | create |

---

## Phase 0 — De-risk spike (gates everything)

### Task 0: Validate Lance-as-catalog + Lance-on-S3 conditional-write commits

**Files:**
- Create: `docs/superpowers/spikes/2026-07-04-lance-s3-findings.md`
- Scratch (throwaway, not committed): `scratchpad/spike_lance.py`

**Interfaces:**
- Produces (for all later tasks): the confirmed call signatures for — (1) upsert into a Lance dataset (`merge_insert`), (2) delete-by-predicate, (3) append, (4) register a Lance dataset into DuckDB and run SQL over it, (5) open an `s3://` Lance dataset with `storage_options`, (6) two concurrent writers committing to one S3 dataset where the loser retries. Plus the Lance/lancedb version that supports (6).

- [ ] **Step 1: Local relational-Lance spike**

Write `scratchpad/spike_lance.py` proving the catalog mechanics locally (no S3):

```python
import lance, lancedb, duckdb, pyarrow as pa, tempfile, os
d = tempfile.mkdtemp()
db = lancedb.connect(d)
rows = [{"source": "gdocs", "doc_id": "d1", "hash": "h1", "body": "alpha", "author": "Ada"}]
tbl = db.create_table("documents", rows)
# upsert by (source, doc_id): update body/hash on match, insert on miss
new = [{"source": "gdocs", "doc_id": "d1", "hash": "h2", "body": "beta", "author": "Ada"},
       {"source": "gdocs", "doc_id": "d2", "hash": "h9", "body": "gamma", "author": "Bob"}]
(tbl.merge_insert(["source", "doc_id"])
    .when_matched_update_all().when_not_matched_insert_all().execute(new))
# delete-by-predicate (edges/chunks replace pattern)
tbl.delete("source = 'gdocs' AND doc_id = 'd2'")
# register into DuckDB and run the kind of SQL store.py uses
con = duckdb.connect()
con.register("documents", tbl.to_lance())
print(con.execute("SELECT doc_id, hash, body FROM documents ORDER BY doc_id").fetchall())
assert con.execute("SELECT hash FROM documents WHERE doc_id='d1'").fetchone()[0] == "h2"
print("LOCAL OK")
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python scratchpad/spike_lance.py`
Expected: prints the rows with `d1` at `h2`/`beta`, `d2` deleted, then `LOCAL OK`. If `merge_insert` / `to_lance` differ in this pinned version, record the actual working call in the findings doc — those signatures become authoritative for Phase 1.

- [ ] **Step 3: Create the S3 test bucket (uses the user's AWS CLI)**

Run: `aws s3 mb s3://bean-cloud-spike-$(date +%s) --region us-east-1` (record the exact name). Confirm the account/region with `aws sts get-caller-identity`.

- [ ] **Step 4: S3 single-writer + concurrent-writer spike**

Extend the scratch script to open the dataset on S3 and prove concurrent commits are safe:

```python
BUCKET = os.environ["SPIKE_BUCKET"]  # s3://bean-cloud-spike-…/cat
so = {"aws_region": "us-east-1"}     # credentials via the standard AWS chain
db = lancedb.connect(BUCKET, storage_options=so)
tbl = db.create_table("documents", rows, mode="overwrite")
# Two independent handles emulate two writers racing a commit on the same dataset.
a = lancedb.connect(BUCKET, storage_options=so).open_table("documents")
b = lancedb.connect(BUCKET, storage_options=so).open_table("documents")
a.add([{"source": "s", "doc_id": "A", "hash": "1", "body": "x", "author": "z"}])
b.add([{"source": "s", "doc_id": "B", "hash": "1", "body": "y", "author": "z"}])  # must not silently drop A
n = lancedb.connect(BUCKET, storage_options=so).open_table("documents").count_rows()
print("S3 rows after two racing appends:", n)  # expect original+2
```

- [ ] **Step 5: Run the S3 spike and record the concurrency verdict**

Run: `SPIKE_BUCKET=s3://<name>/cat .venv/bin/python scratchpad/spike_lance.py`
Expected: both appended rows survive (append auto-rebases). **Then test the harder case:** two racing `merge_insert`s on the same key — confirm one wins and the other raises a commit-conflict that a retry (re-open + re-apply) resolves. Record the exact exception type and the retry recipe. **If this pinned Lance version cannot do lock-free S3 commits,** record the minimum version that can and bump `pylance`/`lancedb` in `pyproject.toml` as part of this task, then re-run.

- [ ] **Step 6: Write findings + clean up**

Write `docs/superpowers/spikes/2026-07-04-lance-s3-findings.md` with: the working signatures for merge_insert/delete/append/to_lance/DuckDB-register, the S3 `storage_options` shape, the concurrency exception type + retry recipe, and the confirmed Lance version. Delete the spike bucket: `aws s3 rb s3://<name> --force`.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/spikes/2026-07-04-lance-s3-findings.md pyproject.toml
git commit -m "spike: validate Lance-as-catalog + S3 conditional-write commits"
```

> **Gate:** Phases 1–4 use the signatures recorded in the findings doc. Where a code block below calls `merge_insert(...)` / `storage_options=...`, substitute the exact verified form if the spike found a difference.

---

## Phase 1 — Lance-backed local Store (no S3 yet)

Goal: move `documents`/`revisions`/`edges` from the local DuckDB file into local Lance datasets, keep every existing retrieval query and `bean sql` working, remove the embed-checkpoint machinery, and adopt only-write-after-embed — all while `make test` stays green. No S3 in this phase.

### Task 1.1: `lancecat` — the Lance catalog with DuckDB read views

**Files:**
- Create: `bean/lancecat.py`
- Create tests: append a "Lance catalog" block to `tests/test_bean.py`

**Interfaces:**
- Produces:
  - `class Catalog(root: Path)` — opens/creates the four Lance datasets under `root` (`documents/`, `revisions/`, `edges/`, `chunks/` as lancedb tables in one lancedb dir).
  - `Catalog.upsert_documents(rows: list[dict]) -> None` — `merge_insert` on `(source, doc_id)`.
  - `Catalog.delete_documents(source: str, doc_ids: list[str]) -> None`.
  - `Catalog.append_revisions(rows: list[dict]) -> None`.
  - `Catalog.replace_edges(source: str, src_doc: str, rows: list[dict]) -> None` — delete-predicate + add.
  - `Catalog.replace_chunks(source: str, doc_id: str, rows: list[dict]) -> None` — delete-predicate + add.
  - `Catalog.duck() -> duckdb.DuckDBPyConnection` — a connection with `documents`/`revisions`/`edges`/`chunks` registered as views (via `to_lance()`), for read SQL. Returns `None`-safe empty views when a dataset doesn't exist yet.
  - `Catalog.SCHEMAS` — the pyarrow schema per dataset (so empty datasets are created with correct typed columns, incl. `chunks.ord INT`, `chunks.vector` fixed-size list, `documents` WITHOUT `embedded_hash`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bean.py`:

```python
# == Lance catalog: relational tables on Lance, queried via DuckDB =============================
from bean.lancecat import Catalog  # noqa: E402
_catdir = Path(tempfile.mkdtemp(prefix="bean-cat-"))
_cat = Catalog(_catdir)
_cat.upsert_documents([{"source": "gdocs", "doc_id": "d1", "title": "T", "url": "u", "revision_id": "r1",
                        "hash": "h1", "body": "alpha body", "created_at": None, "modified_at": None,
                        "author": "Ada", "mime": None, "fetched_at": None}])
_cat.upsert_documents([{"source": "gdocs", "doc_id": "d1", "title": "T2", "url": "u", "revision_id": "r2",
                        "hash": "h2", "body": "beta body", "created_at": None, "modified_at": None,
                        "author": "Ada", "mime": None, "fetched_at": None}])
_rows = _cat.duck().execute("SELECT title, hash, body FROM documents WHERE doc_id='d1'").fetchall()
ok(_rows == [("T2", "h2", "beta body")], "Lance upsert updates in place, queried via DuckDB")
_cat.delete_documents("gdocs", ["d1"])
ok(_cat.duck().execute("SELECT count(*) FROM documents").fetchone()[0] == 0, "Lance delete removes the row")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `make test`
Expected: FAIL — `ModuleNotFoundError: No module named 'bean.lancecat'`.

- [ ] **Step 3: Implement `bean/lancecat.py`**

Use the spike-confirmed calls. Skeleton (fill signatures from findings if they differ):

```python
"""Lance-backed catalog: the four shared datasets (documents, revisions, edges, chunks) as Lance
tables under one directory, plus a DuckDB read connection that registers them as views so every
existing SQL query and `bean sql` runs unchanged. Writes are immutable Lance ops (merge_insert /
delete-predicate + add / append). This is the storage substrate `Store` sits on."""
from __future__ import annotations
from pathlib import Path
import duckdb, lancedb, pyarrow as pa

_TS = pa.timestamp("us")
SCHEMAS = {
    "documents": pa.schema([("source", pa.string()), ("doc_id", pa.string()), ("title", pa.string()),
        ("url", pa.string()), ("revision_id", pa.string()), ("hash", pa.string()), ("body", pa.string()),
        ("created_at", _TS), ("modified_at", _TS), ("author", pa.string()), ("mime", pa.string()),
        ("fetched_at", _TS)]),
    "revisions": pa.schema([("source", pa.string()), ("doc_id", pa.string()),
        ("revision_id", pa.string()), ("hash", pa.string()), ("fetched_at", _TS)]),
    "edges": pa.schema([("source", pa.string()), ("src_doc", pa.string()), ("rel", pa.string()),
        ("dst_kind", pa.string()), ("dst", pa.string())]),
    # chunks vector width is set on first real write (embedding-model dependent); created lazily.
}

def _esc(s: str) -> str:
    return str(s).replace("'", "''")

class Catalog:
    def __init__(self, root: Path):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.root))

    def _table(self, name):
        return self.db.open_table(name) if name in self.db.table_names() else None

    def _ensure(self, name):
        t = self._table(name)
        if t is None and name in SCHEMAS:
            t = self.db.create_table(name, schema=SCHEMAS[name])
        return t

    def upsert_documents(self, rows):
        if not rows: return
        t = self._ensure("documents")
        (t.merge_insert(["source", "doc_id"])
          .when_matched_update_all().when_not_matched_insert_all()
          .execute(pa.Table.from_pylist(rows, schema=SCHEMAS["documents"])))

    def delete_documents(self, source, doc_ids):
        t = self._table("documents")
        if t is None or not doc_ids: return
        ids = ",".join(f"'{_esc(d)}'" for d in doc_ids)
        t.delete(f"source = '{_esc(source)}' AND doc_id IN ({ids})")

    def append_revisions(self, rows):
        if not rows: return
        self._ensure("revisions").add(pa.Table.from_pylist(rows, schema=SCHEMAS["revisions"]))

    def replace_edges(self, source, src_doc, rows):
        t = self._ensure("edges")
        t.delete(f"source = '{_esc(source)}' AND src_doc = '{_esc(src_doc)}'")
        if rows:
            t.add(pa.Table.from_pylist([{"source": source, "src_doc": src_doc, **r} for r in rows],
                                       schema=SCHEMAS["edges"]))

    def replace_chunks(self, source, doc_id, rows):
        t = self._table("chunks")
        if t is None and rows:
            t = self.db.create_table("chunks", rows)  # width from the first vector
        elif t is not None:
            t.delete(f"source = '{_esc(source)}' AND doc_id = '{_esc(doc_id)}'")
            if rows: t.add(rows)

    def duck(self):
        con = duckdb.connect()
        for name in ("documents", "revisions", "edges", "chunks"):
            t = self._table(name)
            if t is not None:
                con.register(name, t.to_lance())
            elif name in SCHEMAS:  # empty typed view so queries don't explode pre-first-write
                con.register(name, pa.Table.from_pylist([], schema=SCHEMAS[name]))
        return con
```

- [ ] **Step 4: Run tests, verify pass**

Run: `make test`
Expected: the new "Lance catalog" checks pass; the rest of the suite still passes.

- [ ] **Step 5: Commit**

```bash
git add bean/lancecat.py tests/test_bean.py
git commit -m "feat: Lance-backed catalog with DuckDB read views"
```

### Task 1.2: Re-seat `Store` on `Catalog` (documents/revisions/edges), keep the method surface

**Files:**
- Modify: `bean/store.py`
- Modify: `bean/workspace.py` (add `catalog_dir` property; keep `db_path` for the private `state` store)

**Interfaces:**
- Consumes: `Catalog` (Task 1.1).
- Produces: `Store` unchanged public surface — `get`, `upsert` (still returns `bool` "content changed"), `delete`, `doc_ids`, `recent`, `find_docs`, `keyword_search`, `chunk_by_id`, `neighbors`, `related`, `revisions`, `replace_edges`, `edges_of`, `counts`, `doc_meta_map`, `get_state`, `set_state`. `state` now lives in a small private local DuckDB at `ws.db_path` (documents/revisions/edges no longer there).

- [ ] **Step 1: Keep the existing store tests as the spec.** The store block (`tests/test_bean.py:104-128`) and all retrieval tests must pass unchanged against the new backend. No new test file — this task is "make the existing suite pass on Lance."

- [ ] **Step 2: Rewrite `Store` internals**

`Store.__init__` opens a `Catalog(ws.catalog_dir)` for the four datasets and a tiny private DuckDB (`ws.db_path`) holding only the `state` table. Reads run over `self.cat.duck()` (register once per read, or cache a connection and re-register when a dataset changed — start simple: a fresh `duck()` per read method; optimize later if `make test` is slow). Rewrite each method:

- `upsert`: compute hash (unchanged `content_hash`); read existing via a `duck()` SELECT; if same hash → `upsert_documents` with refreshed metadata (return `False`); else `upsert_documents` full row + `append_revisions` (return `True`).
- `delete`: `cat.delete_documents(source,[doc_id])` + `cat.replace_edges(source,doc_id,[])`.
- `get`, `recent`, `find_docs`, `keyword_search`, `neighbors`, `chunk_by_id`, `related`, `revisions`, `edges_of`, `counts`, `doc_meta_map`: same SQL as today, run over `cat.duck()`. The `_BASE`/`_chunk_rows` helpers now read the registered `chunks` view directly (see Task 1.3 for `ord`).
- `replace_edges`: delegate to `cat.replace_edges`.
- `get_state`/`set_state`: unchanged, against the private `state` DuckDB.

Show the changed `__init__`, `upsert`, `delete`, and `_chunk_rows` in full in the implementation (the rest are mechanical SQL-target swaps).

- [ ] **Step 3: Add `Workspace.catalog_dir`**

```python
@property
def catalog_dir(self) -> Path:
    return self.dir / "catalog"   # the four Lance datasets (documents/revisions/edges/chunks)
```

Point `lance_dir` (chunks) at `catalog_dir` so chunks live under the same lancedb dir as the catalog (one `Catalog` owns all four). Keep `db_path` for the private `state` DuckDB.

- [ ] **Step 4: Run tests**

Run: `make test`
Expected: the store + retrieval + graph + filter blocks pass on the Lance backend. (The embed-checkpoint block at 568–593 will still fail — fixed in Task 1.4. It's acceptable for this task's commit to leave that block red **only if** you run the suite up to it; prefer to land 1.3 + 1.4 before the next green `make test`. To keep commits green, temporarily skip the 568–593 block with a guard comment and remove the guard in 1.4.)

- [ ] **Step 5: Commit**

```bash
git add bean/store.py bean/workspace.py
git commit -m "refactor: re-seat Store on the Lance catalog; state stays local"
```

### Task 1.3: Store `ord` on chunks; drop the window function

**Files:**
- Modify: `bean/index.py` (`reindex_doc` writes `ord`)
- Modify: `bean/store.py` (`_BASE` reads the stored `ord`)

**Interfaces:**
- Consumes: `Catalog.replace_chunks` (Task 1.1).
- Produces: chunk rows carry `ord INT` (0-based position within a doc among base chunks; large `…-large` chunks get `ord = NULL` and stay excluded).

- [ ] **Step 1: Write the failing test**

```python
# ord is stored, not derived
_ordws = Workspace(repo("ord"))
with Store(_ordws) as store:
    store.upsert("gdocs", "o1", title="O", url=None, revision_id=None,
                 body="\n".join(f"line {i} of content here about a topic" for i in range(120)))
reembed(_ordws, embed_fn=fake_embed)
with Store(_ordws) as store:
    _ch = store.neighbors("gdocs", "o1", 0, 999)
ok([c["ord"] for c in _ch] == list(range(len(_ch))), "stored ord is a dense 0-based sequence per doc")
```

- [ ] **Step 2: Run it**

Run: `make test`
Expected: FAIL (KeyError `ord` or wrong sequence) until `reindex_doc` writes it.

- [ ] **Step 3: Implement**

In `reindex_doc`, compute `ord` while building rows: base chunks get `0,1,2,…` in `start` order; `…-large` rows get `ord=None`. Add `"ord"` to each row dict. In `store.py` change `_BASE` from the `row_number() OVER (…)` expression to selecting the stored `ord` (still filtering `id NOT LIKE '%-large'`).

- [ ] **Step 4: Run tests**

Run: `make test`
Expected: the `ord` check and existing neighbor/merge tests pass.

- [ ] **Step 5: Commit**

```bash
git add bean/index.py bean/store.py tests/test_bean.py
git commit -m "feat: store chunk ord at embed time; drop the window function"
```

### Task 1.4: Make `embedded_hash` a private local checkpoint (out of the shared catalog)

> **Resolution of a spec/plan conflict (human-decided during execution).** Spec decision 6 ("drop `embedded_hash`; resumability from cursors advanced only after commit") breaks in local mode: bean's connectors write doc rows AND advance their sync cursors DURING fetch, decoupled from embedding, so without a checkpoint an interrupted embed permanently orphans a doc row (its content hash is unchanged next sync, so it's never re-embedded). The existing suite tests interrupted-embed resume, so this is real. Chosen fix: **keep `embedded_hash` and its `embed_queue`/`mark_embedded` machinery, but move `embedded_hash` OUT of the shared `documents` dataset into a PRIVATE local table.** `embedded_hash`'s only real problem was being a *shared/multi-writer* field; as private per-writer bookkeeping it's fine and preserves resumability exactly. The "shared store contains only embedded docs" guarantee is enforced later, at the **S3 commit filter (Phase 2, Task 2.4)** — a writer commits to the shared `documents` dataset only docs whose private `embedded_hash == hash`. `run_sync` and the existing interrupted-resume test are essentially unchanged; only the checkpoint's *location* moves.

**Files:**
- Modify: `bean/lancecat.py` (remove `embedded_hash` from `SCHEMAS["documents"]` — back to Task 1.1's clean schema)
- Modify: `bean/store.py` (private `embedded` table; rework `embed_queue`/`mark_embedded`; simplify `upsert` — drop the embedded_hash juggling added in 1.2)
- Modify: `tests/test_bean.py` only if a check assumed `embedded_hash` lives in `documents` (the interrupted-resume block should keep passing as-is)

**Interfaces:**
- Consumes: `Store` (1.2), `Catalog` (1.1).
- Produces: `Store.embed_queue(sources=None, *, force=False)` and `Store.mark_embedded(source, doc_id)` — same signatures/behavior as today, but the checkpoint reads/writes a PRIVATE local table `embedded(source, doc_id, embedded_hash)` instead of a column on the shared `documents` dataset. `run_sync` keeps its current embed-queue-driven flow (unchanged return shape).

- [ ] **Step 1: Confirm the interrupted-resume test still expresses the guarantee**

The existing checkpoint block (interrupted embed → resume drains the queue → an edited doc re-enters the queue) is still the correct spec and should keep passing. Do NOT rewrite it to a "changed set" model. If any assertion referenced `embedded_hash` as a `documents` column directly, adjust only that reference. First run `make test` and note it's green at 207 before changing anything.

- [ ] **Step 2: Remove `embedded_hash` from the shared schema**

In `bean/lancecat.py`, delete the `embedded_hash` field from `SCHEMAS["documents"]` (restoring Task 1.1's schema). The shared `documents` dataset no longer carries it.

- [ ] **Step 3: Add the private `embedded` table and rework the checkpoint**

In `bean/store.py`:
- In `__init__`, create the private table in the same private DuckDB that holds `state` (`ws.db_path`):
  ```python
  self._state.execute("CREATE TABLE IF NOT EXISTS embedded ("
                      "source TEXT, doc_id TEXT, embedded_hash TEXT, PRIMARY KEY (source, doc_id))")
  ```
- `mark_embedded(source, doc_id)`: read the doc's current `hash` from the catalog, then upsert the private table:
  ```python
  h = self.get(source, doc_id).hash
  self._state.execute("INSERT INTO embedded (source, doc_id, embedded_hash) VALUES (?, ?, ?) "
                      "ON CONFLICT (source, doc_id) DO UPDATE SET embedded_hash = excluded.embedded_hash",
                      [source, doc_id, h])
  ```
- `embed_queue(sources=None, *, force=False)`: the "needs embedding" set is docs whose current `hash` has no matching private `embedded_hash`. ATTACH the private DB into the catalog read connection and join, preserving the oldest-first order:
  ```python
  con = self.cat.duck()
  con.execute(f"ATTACH '{self.ws.db_path}' AS priv (READ_ONLY)")
  where = ["1=1"]; params = []
  if sources is not None:
      marks = ",".join("?" * len(sources)) or "NULL"
      where.append(f"d.source IN ({marks})"); params += list(sources)
  join_pred = "" if force else \
      "AND (e.embedded_hash IS NULL OR e.embedded_hash <> d.hash)"
  rows = con.execute(
      f"SELECT d.source, d.doc_id FROM documents d "
      f"LEFT JOIN priv.embedded e ON d.source=e.source AND d.doc_id=e.doc_id "
      f"WHERE {' AND '.join(where)} {join_pred} "
      "ORDER BY COALESCE(d.modified_at, d.fetched_at) ASC, d.doc_id ASC", params).fetchall()
  return [(r[0], r[1]) for r in rows]
  ```
  (When `force=True`, every doc is returned — matching today's `--rebuild`. Confirm the ATTACH path is read-only and that a workspace whose private DB has an empty `embedded` table still returns all docs as needing embedding.)
- `upsert`: REMOVE the `embedded_hash` handling the 1.2 task threaded through it (the `documents` row no longer has that field). `upsert` just writes metadata (same-hash path) or the full row + revision (changed path).

- [ ] **Step 4: Run tests**

Run: `make test`
Expected: full suite green — the interrupted-resume block still passes, now driven by the private checkpoint. `bean sql` over `documents` no longer shows an `embedded_hash` column (fine — it was internal).

- [ ] **Step 5: Commit**

```bash
git add bean/lancecat.py bean/store.py tests/test_bean.py
git commit -m "refactor: move embedded_hash to a private local checkpoint (out of the shared catalog)"
```

> **Phase 2 carry-forward (Task 2.4):** the writer's S3 commit of `documents` must include only docs whose private `embedded_hash == hash`, so the shared dataset never receives an un-embedded row. Recorded so Phase 2 honors the "only write after embed" guarantee at the commit boundary.

### Task 1.5: Local format migration (bean.duckdb tables → Lance datasets)

**Files:**
- Modify: `bean/store.py` (detect a legacy `bean.duckdb` with `documents`/`revisions`/`edges` and convert once)
- Modify: `tests/test_bean.py` (migration test)

**Interfaces:**
- Consumes: `Catalog` (1.1).
- Produces: `Store.__init__` transparently migrates a pre-existing DuckDB catalog into the Lance datasets on first open, idempotently; `state` is preserved in the private DuckDB.

- [ ] **Step 1: Write the failing test**

Seed a legacy-shaped DuckDB (documents/revisions/edges tables) at `ws.db_path`, open a `Store`, assert the docs are now queryable via the Lance path and a second open is a no-op.

```python
# legacy DuckDB catalog is migrated into Lance on first open
_mig = Workspace(repo("migrate"))
import duckdb as _dd
_c = _dd.connect(str(_mig.db_path))
_c.execute("CREATE TABLE documents (source TEXT, doc_id TEXT, title TEXT, url TEXT, revision_id TEXT, "
           "hash TEXT, body TEXT, created_at TIMESTAMP, modified_at TIMESTAMP, author TEXT, mime TEXT, "
           "fetched_at TIMESTAMP, embedded_hash TEXT)")
_c.execute("INSERT INTO documents VALUES ('gdocs','dz','T','u','r','h','body here',NULL,NULL,'Ada',NULL,now(),'h')")
_c.execute("CREATE TABLE revisions (source TEXT, doc_id TEXT, revision_id TEXT, hash TEXT, fetched_at TIMESTAMP)")
_c.execute("CREATE TABLE edges (source TEXT, src_doc TEXT, rel TEXT, dst_kind TEXT, dst TEXT)")
_c.close()
with Store(_mig) as s:
    ok(s.get("gdocs", "dz") is not None and s.get("gdocs", "dz").author == "Ada", "legacy doc migrated to Lance")
with Store(_mig) as s:
    ok(s.counts().get("gdocs") == 1, "second open is a no-op (no double-migration)")
```

- [ ] **Step 2: Run it**

Run: `make test`
Expected: FAIL until migration exists.

- [ ] **Step 3: Implement migration**

In `Store.__init__`, after opening the private DuckDB and the Catalog: if the DuckDB has a `documents` table AND the Lance `documents` dataset is empty/absent, read all rows (dropping `embedded_hash`), `upsert_documents` / `append_revisions` / `replace_edges` into the Catalog, then `DROP TABLE documents/revisions/edges` from the DuckDB (leaving only `state`). Guard with a `state` flag `catalog_migrated=true` so it runs once.

- [ ] **Step 4: Run tests**

Run: `make test`
Expected: migration checks pass; suite green.

- [ ] **Step 5: Commit**

```bash
git add bean/store.py tests/test_bean.py
git commit -m "feat: migrate legacy DuckDB catalog into Lance on first open"
```

> **Phase 1 exit criteria:** `make test` green; local bean now stores all four tables as Lance; `bean sql`, retrieval, graph, filters, and sync all pass; no `embed_queue`/`embedded_hash` remain. No behavior change visible to a local-only user.

---

## Phase 2 — S3 shared plane + replication

> Exact `storage_options`, the concurrency exception type, and the retry recipe come from Task 0's findings doc. The code blocks below use the spike's placeholders — substitute verbatim.

### Task 2.1: `cloud` config + `Workspace` cloud awareness

**Files:** Modify `bean/config.py`, `bean/workspace.py`; test in `tests/test_bean.py`.

**Interfaces:**
- Produces: `config` `cloud` block (`enabled`, `role`, `bucket`, `prefix`, `region`); `Workspace.cloud` (resolved dict), `Workspace.is_cloud` (bool), `Workspace.remote_uri` (`s3://bucket/prefix`), `Workspace.replica_dir` (== `catalog_dir`; the mirror). Local-only when `cloud.enabled` is false.

- [ ] **Step 1: Test** — a workspace with a `cloud` block reports `is_cloud`, `remote_uri`; without one, `is_cloud` is False and `remote_uri` is None. (Full assertion code written against the property names above.)
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** the `cloud` defaults in `config.py`; read the block in `Workspace` (from workspace `settings.cloud`), expose the properties.
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: cloud config block + Workspace cloud awareness`.

### Task 2.2: `remote.pull` — S3→local replication (data files before manifests)

**Files:** Create `bean/remote.py`; test in `tests/test_bean.py` using a **local temp dir as the "remote"** (Lance takes local paths, so a filesystem dir stands in for S3 and exercises the same replication logic).

**Interfaces:**
- Consumes: `Catalog`, `Workspace` (2.1).
- Produces: `remote.pull(ws) -> None` — fast-forward the local replica from the remote so local reads see the latest committed version. Copies dataset files, **manifests last**, so a concurrent local reader never sees a manifest referencing not-yet-present files. Idempotent; a no-op when already current.

- [ ] **Step 1: Test** — write via a `Catalog` pointed at a "remote" temp dir; `pull` into a fresh local replica dir; assert a `Store` over the local replica sees the doc. Then a second `pull` transfers nothing new.
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** `pull`: enumerate remote dataset files newer than the local version and copy them (data/fragment files first, then `_versions`/manifest). For the local-dir remote, this is a filtered file copy; for S3, the same enumeration via the object store. Encapsulate the transfer behind a small `_copy_new(remote_uri, local_dir)` so S3 and local-dir share one code path.
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: remote.pull replicates immutable Lance files, manifests last`.

### Task 2.3: S3 commit with conditional-write + retry-on-conflict

**Files:** Modify `bean/remote.py` (commit helpers), `bean/lancecat.py` (accept a remote target + `storage_options`).

**Interfaces:**
- Produces: `Catalog(root, remote_uri=None, storage_options=None)` — when `remote_uri` is set, writes commit to the S3 dataset (Lance conditional-write concurrency); `remote.commit_with_retry(fn, retries=5)` wraps a write that may hit a commit conflict, re-opening the table and re-applying on the spike-confirmed exception.

- [ ] **Step 1: Test (local-dir remote)** — two `Catalog` handles on one "remote" dir each upsert a different doc key concurrently (sequential in-test, but via independent handles that don't share a version); assert both survive and a racing same-key upsert converges after retry. Uses the spike's exception type.
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** the conditional-write commit + `commit_with_retry` using the findings-doc recipe (re-open table → re-apply op). Wire `Catalog`'s write methods through it when `remote_uri` is set.
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: S3 conditional-write commits with retry-on-conflict`.

### Task 2.4: Writer `run_sync` — pull → fetch → embed → commit → advance cursor

**Files:** Modify `bean/sync.py`.

**Interfaces:**
- Consumes: `remote.pull`, `Catalog` remote commit (2.3), cursors in the private local `state`.
- Produces: when `ws.is_cloud` and role is writer, `run_sync` pulls first, then per source: fetch (private cursor) → embed changed → commit chunks+documents+revisions+edges to S3 (one commit set per source) → advance that source's cursor. On commit failure the cursor does **not** advance.

- [ ] **Step 1: Test** — a cloud workspace (local-dir remote) syncs a fake source; assert docs land in the remote, the local replica sees them, and a simulated commit failure leaves the cursor un-advanced so a re-sync re-embeds.
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** the writer branch in `run_sync`: `remote.pull(ws)` at the top; move `set_state(cursor)` to after the source's successful commit; batch the commit per source.
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: writer sync — pull, embed, commit-per-source, advance cursor after commit`.

---

## Phase 3 — Roles, CLI, migration to cloud

### Task 3.1: `bean cloud init` (writer provisioning + upload local index)

**Files:** Modify `bean/cli.py`, `bean/remote.py` (`cloud_init`).

**Interfaces:**
- Produces: `bean cloud init --bucket B --prefix P --region R` → writes the `cloud` block (role=writer) into the workspace config, creates the remote datasets, and uploads the existing local Lance datasets as the initial commit. `remote.cloud_init(ws, bucket, prefix, region) -> None`.

- [ ] **Step 1: Test** — `cloud_init` against a local-dir "remote": a workspace with local docs, after init, has those docs present in the remote and its config marked cloud/writer.
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** `cloud_init` (push local datasets → remote via the commit path) + the `cloud` subparser dispatch in `cli.py` (`bean cloud init|connect`).
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: bean cloud init — become a writer, upload local index`.

### Task 3.2: `bean cloud connect` (read-only consumer)

**Files:** Modify `bean/cli.py`, `bean/remote.py` (`cloud_connect`).

**Interfaces:**
- Produces: `bean cloud connect --bucket B --prefix P [--region R]` → writes the `cloud` block (role=consumer), pulls the replica, requires no source credentials.

- [ ] **Step 1: Test** — `cloud_connect` against a remote seeded by another workspace; the consumer workspace pulls and can `search`/`sql` with no credentials configured.
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** `cloud_connect` (write config + `remote.pull`) and its CLI wiring.
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: bean cloud connect — read-only consumer`.

### Task 3.3: `bean pull` + `bean status` cloud fields + `bean sync` implicit pull

**Files:** Modify `bean/cli.py`.

**Interfaces:**
- Produces: `bean pull` (fast-forward the replica), `bean status` shows role / bucket / prefix / local-vs-remote version, `cmd_sync` calls `remote.pull` first when cloud.

- [ ] **Step 1: Test** — `_status_*` helper reports cloud role + remote uri; a consumer `bean pull` picks up a doc a writer committed after `connect`.
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** the `pull` subparser + `cmd_pull`, status fields, and the implicit pull in `cmd_sync`.
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: bean pull, cloud status fields, sync auto-pull`.

### Task 3.4: Auto-pull before a query (consumer freshness)

**Files:** Modify `bean/cli.py` (or `bean/search.py` entrypoints) to `remote.pull` before read commands when cloud + consumer, with a short min-interval guard so back-to-back queries don't re-pull.

- [ ] **Step 1: Test** — two consecutive queries within the guard interval pull at most once; a query after a new remote commit + interval sees the new doc.
- [ ] **Step 2: Run** `make test` → FAIL.
- [ ] **Step 3: Implement** the guarded auto-pull (last-pull timestamp in private `state`).
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `feat: guarded auto-pull before cloud reads`.

---

## Phase 4 — Multi-writer + real-S3 validation

### Task 4.1: Offline multi-writer conflict test

**Files:** Modify `tests/test_bean.py`.

- [ ] **Step 1:** Two writer workspaces (`writer-a`, `writer-b`) share one local-dir "remote". Both sync overlapping + disjoint docs; assert the union is present, same-key writes converge to one row, and neither writer's private cursor leaks into the other.
- [ ] **Step 2: Run** `make test` → FAIL if any race is mishandled.
- [ ] **Step 3:** Fix any convergence bug surfaced.
- [ ] **Step 4: Run** `make test` → PASS.
- [ ] **Step 5: Commit** `test: multi-writer convergence over a shared remote`.

### Task 4.2: Real-S3 smoke test (opt-in, not in the default suite)

**Files:** Create `tests/smoke_s3.py` (guarded by an env var so `make test` never hits the network).

- [ ] **Step 1:** A script that, given `BEAN_SMOKE_BUCKET`, runs cloud_init from one temp workspace, commits a doc, connects a second temp workspace, pulls, and asserts the doc is searchable. Skips (prints "skipped") when the env var is unset.
- [ ] **Step 2: Run** locally with the user's AWS CLI against a real bucket: `aws s3 mb …`; `BEAN_SMOKE_BUCKET=s3://… .venv/bin/python tests/smoke_s3.py`; then `aws s3 rb … --force`.
- [ ] **Step 3:** Record the result; fix any real-S3-only issue (auth, region, conditional-write) surfaced.
- [ ] **Step 4:** Confirm `make test` still passes and does not touch the network.
- [ ] **Step 5: Commit** `test: opt-in real-S3 smoke for the cloud round-trip`.

> **Project exit criteria:** `make test` green (offline); the real-S3 smoke passes against a live bucket; a writer on machine A and a consumer on machine B share one index; `bean sql` and all retrieval work locally over the replica; local-only users are unaffected.

---

## Self-review (against the spec)

**Spec coverage:** two-plane split → Tasks 1.2 (private state) + 2.x (shared S3); all-Lance four datasets → 1.1–1.3; DuckDB read-only + `bean sql` → 1.1 `duck()` + kept `cmd_sql`; immutable writes → 1.1 write ops + 2.3 conditional commit; only-write-after-embed / drop `embedded_hash` → 1.4; private cursors → 1.2; S3 CAS, no DynamoDB → Task 0 + 2.3; full local replica → 2.2 + 3.4; roles → 3.1/3.2; migration (local format + cloud adoption) → 1.5 + 3.1; deletion v1 limitation → unchanged coarse config-driven delete (no new task; documented in spec); testing offline-first → every task uses `make test` + local-dir remote, real-S3 is opt-in (4.2); Lance-version verification → Task 0. **No uncovered spec section.**

**Placeholder scan:** the only deferred-detail points are the Lance-on-S3 call signatures, and those are deliberately gated on Task 0's findings doc rather than invented — every in-repo task (Phase 0, Phase 1) carries complete, runnable code.

**Type/name consistency:** `Catalog` methods (`upsert_documents`, `delete_documents`, `append_revisions`, `replace_edges`, `replace_chunks`, `duck`) are used consistently across 1.1–2.3; `Store`'s public surface is held stable by 1.2 and never renamed; `remote.pull` / `cloud_init` / `cloud_connect` / `commit_with_retry` names are stable across 2.2–3.3; `Workspace.catalog_dir`/`replica_dir`/`remote_uri`/`is_cloud` stable across 1.2–3.x.
