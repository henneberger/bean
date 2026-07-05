"""Git commit-history source. Indexes commit messages — one document per commit — so the
decisions and context recorded in a repo's history are searchable alongside docs and chat.
No auth: it shells out to `git log` on the local clone. With nothing tracked it indexes the
repo bean runs in; extra clones can be added to the `repos` list. doc_id is `<repo-name>:<sha>`
(the repo prefix feeds the `in_repo` graph edge); author and commit dates land in the document
metadata so `--author`/`--since` filters work over history. Commits are immutable, so the
cursor is just the last-indexed HEAD — an incremental sync reads `<last>..HEAD` only; a
rewritten history (rebase/force-push) falls back to a full scan, which is also when commits
that no longer exist are pruned."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

NAME = "git"

# One record per commit, fields unit-separated; `git log -z` NUL-separates records (a commit
# message can never contain NUL, so parsing needs no escaping).
_FMT = "%H%x1f%an%x1f%aI%x1f%cI%x1f%s%x1f%B"


def _git(repo: Path, *args) -> str | None:
    """Run git in `repo`; None on any failure (not a repo, unborn HEAD, unknown range)."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def _repo_root(path: Path) -> Path | None:
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top.strip()) if top and top.strip() else None


def _commit_url_base(repo: Path) -> str | None:
    """A web URL template for commits, derived from the `origin` remote (GitHub/GitLab shapes,
    both ssh and https). None when there's no recognizable remote — url is optional."""
    origin = (_git(repo, "config", "--get", "remote.origin.url") or "").strip()
    m = re.match(r"(?:git@([^:/]+):|https?://([^/]+)/)(.+?)(?:\.git)?/?$", origin)
    if not m:
        return None
    host, path = m.group(1) or m.group(2), m.group(3)
    if "github" in host:
        return f"https://{host}/{path}/commit/{{sha}}"
    if "gitlab" in host:
        return f"https://{host}/{path}/-/commit/{{sha}}"
    return None


def _log(repo: Path, spec: str | None) -> list[dict] | None:
    """Parsed commits for `spec` (a range like `abc..HEAD`, or None = all history), newest first.
    None when git itself fails (e.g. the range's base commit was rebased away)."""
    args = ["log", "-z", f"--format={_FMT}"] + ([spec] if spec else [])
    out = _git(repo, *args)
    if out is None:
        return None
    commits = []
    for rec in out.split("\0"):
        parts = rec.split("\x1f")
        if len(parts) != 6:
            continue
        sha, author, adate, cdate, subject, body = parts
        commits.append({"sha": sha.strip(), "author": author, "created_at": adate,
                        "modified_at": cdate, "subject": subject, "body": body.strip()})
    return commits


def connected():
    """No auth — always ready. Activation (always_when_connected) hangs off this; a cwd outside
    any git repo just makes `sync` a no-op."""
    return {"local": True}


def sync(store, config: dict, *, settings=None, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    repos = [Path(p).expanduser() for p in (config.get("repos") or [])] or [Path.cwd()]
    changed, removed = [], []
    tracked_names = set()
    for raw in repos:
        root = _repo_root(raw)
        if root is None:
            if config.get("repos"):  # only warn for an explicitly tracked path
                log(f"git: {raw} is not a git repo — skipped")
            continue
        name = root.name
        tracked_names.add(name)
        head = (_git(root, "rev-parse", "HEAD") or "").strip()
        if not head:
            continue  # unborn HEAD — nothing committed yet
        key = f"git.head.{root}"
        last = store.get_state(key)
        if not full and last == head:
            continue  # no new commits since the cursor
        spec = f"{last}..HEAD" if (last and not full) else None
        commits = _log(root, spec) if spec else None
        full_scan = commits is None  # first sync, --rebuild, or the cursor was rebased away
        if commits is None:
            commits = _log(root, None) or []
        url_base = _commit_url_base(root)
        seen = set()
        n = 0
        for c in commits:
            doc_id = f"{name}:{c['sha']}"
            seen.add(doc_id)
            if store.upsert(NAME, doc_id, title=c["subject"] or c["sha"][:12],
                            url=url_base.format(sha=c["sha"]) if url_base else None,
                            revision_id=c["sha"], body=c["body"] or c["subject"],
                            meta={"author": c["author"], "created_at": c["created_at"],
                                  "modified_at": c["modified_at"]}):
                changed.append(doc_id)
                n += 1
        store.set_state(key, head)
        if full_scan:  # only a full enumeration can tell which commits are gone
            prefix = f"{name}:"
            removed += [d for d in store.doc_ids(NAME) if d.startswith(prefix) and d not in seen]
        if n:
            log(f"git: {name} — {n} commit(s) indexed")
    # Commits from repos no longer tracked (and no longer the cwd repo) age out here.
    if config.get("repos"):
        removed += [d for d in store.doc_ids(NAME)
                    if d.split(":", 1)[0] not in tracked_names and d not in removed]
    for doc_id in removed:
        store.delete(NAME, doc_id)
    return {"changed": changed, "removed": removed}
