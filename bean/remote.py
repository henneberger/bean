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


def _copy_new(remote_uri: str, local_dir: Path) -> None:
    """Copy files present under `remote_uri` but not under `local_dir`, manifests last. Files are
    immutable, so anything already present locally is byte-identical and never re-copied."""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    if str(remote_uri).startswith("s3://"):
        _copy_new_s3(remote_uri, local_dir)
    else:
        _copy_new_local(Path(remote_uri), local_dir)


def _s3_sync_commands(remote_uri: str, local_dir: Path) -> list[list[str]]:
    """Build the two `aws s3 sync` argv lists that copy data files before manifests.

    Pass 1: everything except manifests, so data/fragment files land before the manifests that
    reference them. Pass 2: manifests only — `--exclude "*"` first is required, since a bare
    `--include` is a no-op in the AWS CLI (sync includes everything by default; `--include` only
    re-adds files a prior `--exclude` removed). `aws s3 sync` already skips unchanged files."""
    base = ["aws", "s3", "sync", remote_uri, str(local_dir)]
    pass1 = [*base, "--exclude", _MANIFEST_GLOB, "--only-show-errors"]
    pass2 = [*base, "--exclude", "*", "--include", _MANIFEST_GLOB, "--only-show-errors"]
    return [pass1, pass2]


def _copy_new_s3(remote_uri: str, local_dir: Path) -> None:
    if shutil.which("aws") is None:
        raise RuntimeError("remote.pull needs the `aws` CLI on PATH (s3 sync) but it was not found")
    for cmd in _s3_sync_commands(remote_uri, local_dir):
        subprocess.run(cmd, check=True)


_RETRYABLE_MARKERS = ("conflict", "commit", "version", "retry", "concurrent")


def _is_retryable(exc: Exception) -> bool:
    """Heuristic: the spike never triggered a real Lance commit-conflict exception, so there is no
    confirmed exception type to catch. Retry anything whose type name or message looks
    conflict-related; let everything else propagate. Replace with the confirmed type once Task 4.2's
    real-S3 smoke surfaces one."""
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


def _copy_new_local(remote_dir: Path, local_dir: Path) -> None:
    if not remote_dir.is_dir():
        raise RuntimeError(f"remote.pull: remote dir does not exist: {remote_dir}")
    def is_manifest(p: Path) -> bool:
        return _MANIFEST_MARKER in "/" + str(p.relative_to(remote_dir))

    files = [p for p in remote_dir.rglob("*") if p.is_file()]
    data_files = [p for p in files if not is_manifest(p)]
    manifest_files = [p for p in files if is_manifest(p)]
    for src in (*data_files, *manifest_files):
        rel = src.relative_to(remote_dir)
        dst = local_dir / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
