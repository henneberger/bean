"""Task 4.2: opt-in REAL-S3 smoke test for bean's cloud round-trip.

Standalone script, NOT part of the offline `make test` suite (that runs only
tests/test_bean.py). Skips cleanly (exit 0) unless BEAN_SMOKE_BUCKET is set to a real
`s3://bucket` value, in which case it drives the full writer -> S3 -> consumer path against
that live bucket:

  1. writer workspace tracks a `localfiles` doc, `cloud_init`s onto the bucket, and runs a real
     `run_sync` (cloud-writer branch) with a fake embedder — committing straight to S3 via Lance
     + `aws s3 sync`.
  2. a SEPARATE consumer workspace `cloud_connect`s to the same bucket/prefix (pull-only, no
     source ever synced there) and must find the writer's doc via keyword search.

This proves the S3 write path (Lance commit + `aws s3 sync` push) and the S3 read path (pull)
both work against real infrastructure. It does not exercise embedding quality — the fake
embedder here is the same deterministic bag-of-words vector the offline suite uses, so this
never downloads the real model.

Run: BEAN_SMOKE_BUCKET=s3://my-bucket python tests/smoke_s3.py
Optional: BEAN_SMOKE_PREFIX (default "smoke"), AWS_REGION or BEAN_SMOKE_REGION (default
"us-east-1"). Credentials: read from the standard AWS env chain by both Lance and the `aws`
CLI — this script sets none itself.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

BUCKET_ENV = os.environ.get("BEAN_SMOKE_BUCKET", "").strip()

if not BUCKET_ENV:
    print("smoke_s3: skipped (set BEAN_SMOKE_BUCKET=s3://bucket to run)")
    sys.exit(0)

# Everything below only imports/runs once we know we're actually driving a real bucket.
from bean.workspace import Workspace, set_bean_home  # noqa: E402
from bean import remote  # noqa: E402
from bean.sync import run_sync  # noqa: E402
from bean.store import Store  # noqa: E402

MAGIC = "ZQSMOKE-4242"
BUCKET = BUCKET_ENV[len("s3://"):] if BUCKET_ENV.startswith("s3://") else BUCKET_ENV
PREFIX = os.environ.get("BEAN_SMOKE_PREFIX", "smoke")
REGION = os.environ.get("BEAN_SMOKE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def fake_embed(texts):
    """Same deterministic 64-dim bag-of-words embedder as the offline suite (tests/test_bean.py)
    — validates the sync/Lance/S3 plumbing, not embedding quality, with no model download."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in re.findall(r"[a-z]{3,}", t.lower()):
            h = 0
            for ch in w:
                h = (h * 31 + ord(ch)) & 0xFFFFFFFF
            v[h % 64] += 1
        norm = sum(x * x for x in v) ** 0.5 or 1
        out.append([x / norm for x in v])
    return out


def repo(name: str) -> Path:
    """A fresh temp dir with a `.git` subdir, so `workspace.repo_root` treats it as a distinct
    repo (and thus a distinct Workspace) — same helper the offline suite uses."""
    d = Path(tempfile.mkdtemp(prefix=f"bean-smoke-{name}-"))
    (d / ".git").mkdir()
    return d


def step(msg: str) -> None:
    print(f"-- {msg}")


def passed(msg: str) -> None:
    print(f"PASS: {msg}")


def failed(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise AssertionError(msg)


def main() -> None:
    print(f"smoke_s3: bucket={BUCKET!r} prefix={PREFIX!r} region={REGION!r}")
    set_bean_home(tempfile.mkdtemp(prefix="bean-smoke-home-"))

    # -- writer: track a localfiles doc, become a cloud writer, sync (commits straight to S3) ----
    step("writer: tracking localfiles doc + cloud_init")
    src_dir = Path(tempfile.mkdtemp(prefix="bean-smoke-src-"))
    (src_dir / "note.md").write_text(
        f"# Smoke\n\nThe magic token is {MAGIC} for cloud round-trip.\n")

    ws_writer = Workspace(repo("writer"))
    ws_writer.save_config({"localfiles": {"paths": [str(src_dir)]}})
    remote.cloud_init(ws_writer, BUCKET, PREFIX, REGION)
    if not (ws_writer.is_cloud and ws_writer.cloud.get("role") == "writer"):
        failed("cloud_init did not turn the writer workspace into a cloud writer")
    passed("cloud_init: writer workspace cloud-enabled (role=writer)")

    step("writer: run_sync (cloud-writer branch, fake embedder)")
    result = run_sync(ws_writer, keys={"localfiles"}, embed_fn=fake_embed)
    if result["errors"]:
        failed(f"run_sync reported errors: {result['errors']}")
    if len(result["changed"]) != 1:
        failed(f"expected exactly 1 changed doc, got {result['changed']}")
    if result["embedded"] != 1 or result["chunks"] <= 0:
        failed(f"expected 1 embedded doc with >0 chunks, got embedded={result['embedded']} "
               f"chunks={result['chunks']}")
    passed(f"run_sync: 1 doc changed + embedded, {result['chunks']} chunks committed to S3")

    # -- consumer: a SEPARATE workspace/replica that only ever pulls, never syncs a source --------
    step("consumer: cloud_connect (pull-only) from a fresh, separate workspace")
    ws_consumer = Workspace(repo("consumer"))
    remote.cloud_connect(ws_consumer, BUCKET, PREFIX, REGION)
    if not (ws_consumer.is_cloud and ws_consumer.cloud.get("role") == "consumer"):
        failed("cloud_connect did not turn the consumer workspace into a cloud consumer")
    passed("cloud_connect: consumer workspace cloud-enabled (role=consumer), pulled from S3")

    step("consumer: keyword_search finds the writer's doc via the pulled replica")
    with Store(ws_consumer) as store:
        hits = store.keyword_search(MAGIC)
    if not hits:
        failed(f"consumer keyword_search found no chunk containing {MAGIC!r} after pulling from S3")
    if not any(hit.get("doc_id", "").endswith("note.md") for hit in hits):
        failed(f"consumer found hits but none from note.md: {hits}")
    passed(f"consumer: found {len(hits)} chunk(s) containing {MAGIC!r} pulled from S3")

    print("\nsmoke_s3: PASS — writer->S3 commit and S3->consumer pull both verified")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nsmoke_s3: FAIL", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
