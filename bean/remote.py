"""Replicate the cloud Lance catalog into the local replica.

Cloud mode keeps the authoritative catalog at `s3://<bucket>/<prefix>/`; every machine holds a
full local replica at `Workspace.replica_dir` and reads run over that replica. Lance datasets are
directories of immutable files — data/fragment files under `<name>.lance/data/` (and
`_transactions/`), commit manifests under `<name>.lance/_versions/` — so "bring the replica
current" is an additive copy of whatever isn't already present locally, data files before
manifests so a concurrent local reader never sees a manifest pointing at absent data."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_MANIFEST_MARKER = "/_versions/"
_MANIFEST_GLOB = f"*{_MANIFEST_MARKER}*"


def pull(ws) -> None:
    """Fast-forward `ws`'s local replica from its remote. No-op in local (non-cloud) mode."""
    if not ws.is_cloud:
        return
    _copy_new(ws.remote_uri, ws.replica_dir)


def auto_pull(ws, *, min_interval: int = 60, now: float | None = None) -> bool:
    """Guarded auto-refresh: pull `ws`'s replica before a cloud read, but skip it if the last pull
    (manual `bean pull` or a prior `auto_pull`) landed less than `min_interval` seconds ago, so
    back-to-back reads don't each trigger a fetch. No-op (returns False) in local (non-cloud) mode.
    `now` is injectable for deterministic tests; defaults to the current epoch seconds. Returns
    whether a pull actually ran. `last_pull` is always numeric epoch seconds — written the same way
    by `bean pull` and by this function — so a manual pull correctly holds the throttle here too.
    A non-numeric `last_pull` (e.g. stale/corrupt state) is treated as "no timing info yet" rather
    than raising — it just means this call pulls again."""
    if not ws.is_cloud:
        return False
    import time
    from .store import Store
    now = now if now is not None else time.time()
    with Store(ws) as store:
        last_pull = store.get_state("last_pull")
        if isinstance(last_pull, (int, float)) and now - last_pull < min_interval:
            return False
        pull(ws)
        store.set_state("last_pull", now)
    return True


def push(ws) -> None:
    """Upload `ws`'s local replica up to its remote — the reverse of `pull`, used by `cloud_init`
    to seed a fresh bucket with an already-indexed local catalog. No-op in local (non-cloud) mode,
    and a no-op if the local catalog dir doesn't exist yet (nothing indexed to upload)."""
    if not ws.is_cloud:
        return
    if not Path(ws.replica_dir).is_dir():
        return
    _copy_up(ws.replica_dir, ws.remote_uri)


def _write_cloud_config(ws, role: str, bucket: str, prefix: str, region: str) -> None:
    """Write `settings.cloud` (enabled, role, bucket/prefix/region) into `ws`'s config. Shared by
    `cloud_init` (role=writer) and `cloud_connect` (role=consumer) — the config shape is identical,
    only the role and the follow-up sync direction (push vs. pull) differ."""
    config = ws.load_config()
    config.setdefault("settings", {})["cloud"] = {
        "enabled": True, "role": role, "bucket": bucket, "prefix": prefix, "region": region,
    }
    ws.save_config(config)


def cloud_init(ws, bucket: str, prefix: str, region: str) -> None:
    """Turn `ws` into a cloud writer: write the cloud config (enabled, role=writer, bucket/prefix/
    region), then push whatever is already indexed locally up to the new remote so it becomes the
    shared catalog's starting content instead of an empty bucket."""
    _write_cloud_config(ws, "writer", bucket, prefix, region)
    push(ws)


def cloud_connect(ws, bucket: str, prefix: str, region: str) -> None:
    """Turn `ws` into a read-only cloud consumer: write the cloud config (enabled, role=consumer,
    bucket/prefix/region) then pull the existing remote catalog down into the local replica. No
    source credentials needed — this only ever reads the shared Lance catalog."""
    _write_cloud_config(ws, "consumer", bucket, prefix, region)
    pull(ws)


def _copy_new(remote_uri: str, local_dir: Path) -> None:
    """Copy files present under `remote_uri` but not under `local_dir`, manifests last. Files are
    immutable, so anything already present locally is byte-identical and never re-copied."""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    _copy(remote_uri, local_dir)


def _copy_up(local_dir: Path, remote_uri: str) -> None:
    """Copy files present under `local_dir` but not under `remote_uri`, manifests last — the
    push-direction sibling of `_copy_new`, sharing the same manifests-last invariant."""
    if not str(remote_uri).startswith("s3://"):
        Path(remote_uri).mkdir(parents=True, exist_ok=True)
    _copy(local_dir, remote_uri)


def _copy(src, dst) -> None:
    """Copy files present under `src` but not under `dst`, data files before manifests (see module
    docstring). Direction-agnostic — `pull` drives this remote->local, `push` drives it local-
    >remote — so it dispatches on whichever side is `s3://...`; a local directory standing in for
    the remote (offline tests) takes the plain-copy branch either way."""
    if str(src).startswith("s3://") or str(dst).startswith("s3://"):
        _copy_s3(src, dst)
    else:
        _copy_local(Path(src), Path(dst))


def _s3_sync_commands(src, dst) -> list[list[str]]:
    """Build the two `aws s3 sync` argv lists that copy data files before manifests, in whichever
    direction `src`/`dst` indicate (either may be `s3://...` or a local path).

    Pass 1: everything except manifests, so data/fragment files land before the manifests that
    reference them. Pass 2: manifests only — `--exclude "*"` first is required, since a bare
    `--include` is a no-op in the AWS CLI (sync includes everything by default; `--include` only
    re-adds files a prior `--exclude` removed). `aws s3 sync` already skips unchanged files."""
    base = ["aws", "s3", "sync", str(src), str(dst)]
    pass1 = [*base, "--exclude", _MANIFEST_GLOB, "--only-show-errors"]
    pass2 = [*base, "--exclude", "*", "--include", _MANIFEST_GLOB, "--only-show-errors"]
    return [pass1, pass2]


def _copy_s3(src, dst) -> None:
    if shutil.which("aws") is None:
        raise RuntimeError("remote sync needs the `aws` CLI on PATH (s3 sync) but it was not found")
    for cmd in _s3_sync_commands(src, dst):
        subprocess.run(cmd, check=True)


_RETRYABLE_MARKERS = ("conflict", "commit conflict", "retry", "concurrent")


def _is_retryable(exc: Exception) -> bool:
    """Heuristic: the spike never triggered a real Lance commit-conflict exception, so there is no
    confirmed exception type to catch. Retry anything whose type name or message looks
    conflict-related; let everything else propagate. Replace with the confirmed type once Task 4.2's
    real-S3 smoke surfaces one. Deliberately narrow: bare "commit" or "version" are dropped so an
    unrelated "commit ..." message or a schema-version mismatch is never mistakenly retried — only
    a genuine Lance commit conflict (which says "conflict") or an explicit retry/concurrency signal
    triggers a retry here."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


def commit_with_retry(fn, *, retries: int = 5):
    """Call `fn()` and return its result. `fn` is expected to re-open its table and re-apply the op
    each time it's called, so a re-run sees the latest committed version. On an exception that looks
    like a Lance commit conflict (see `_is_retryable`), retry up to `retries` times; any other
    exception propagates immediately without retrying."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable(exc) or attempt == retries:
                raise


def _copy_local(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.is_dir():
        raise RuntimeError(f"remote sync: source dir does not exist: {src_dir}")
    dst_dir.mkdir(parents=True, exist_ok=True)
    def is_manifest(p: Path) -> bool:
        return _MANIFEST_MARKER in "/" + str(p.relative_to(src_dir))

    files = [p for p in src_dir.rglob("*") if p.is_file()]
    data_files = [p for p in files if not is_manifest(p)]
    manifest_files = [p for p in files if is_manifest(p)]
    for src in (*data_files, *manifest_files):
        rel = src.relative_to(src_dir)
        dst = dst_dir / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
