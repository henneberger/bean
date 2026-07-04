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


def _copy_new_s3(remote_uri: str, local_dir: Path) -> None:
    if shutil.which("aws") is None:
        raise RuntimeError("remote.pull needs the `aws` CLI on PATH (s3 sync) but it was not found")
    # Pass 1: everything except manifests, so data/fragment files land before the manifests that
    # reference them. Pass 2: manifests only. `aws s3 sync` already skips unchanged files.
    subprocess.run(["aws", "s3", "sync", remote_uri, str(local_dir),
                     "--exclude", "*/_versions/*", "--only-show-errors"], check=True)
    subprocess.run(["aws", "s3", "sync", remote_uri, str(local_dir),
                     "--include", "*/_versions/*", "--only-show-errors"], check=True)


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
