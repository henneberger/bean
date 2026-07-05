"""Per-repo workspaces under ~/.bean, plus the committed `.bean/` folder inside the repo.

Every repo gets its own folder — <repo-name>-<hash-of-path>/ — holding that repo's personal
config, DuckDB catalog, Lance vector store, and (for local connectors) its own credentials.
A repo may also carry a **committed** `.bean/` folder: `.bean/config.json` holds the shareable
side (which sources the repo tracks + team `settings`, e.g. the storage backend) so anyone who
clones the repo inherits them; `effective_config()` merges it under the personal workspace
config (lists union, personal scalars win). Secrets never go in `.bean/` — credentials always
stay under ~/.bean. bean only writes inside the repo on explicit commands (`bean cloud init
--backend git`) or when asked to share config; day-to-day sync state stays out of the repo.

Credentials resolve by scope, mirroring connectors: a **global** connector's credential is shared
at ~/.bean/credentials/ (one Slack, one personal Google); a **local** connector's credential lives
in that repo's workspace (so you can have a different GitHub token per project), with a fallback to
the shared dir. The active scope is set with `credential_context(ws)` around auth/sync — connectors
themselves just call load_credential/save_credential and stay oblivious. All files are mode 0600.

The home directory defaults to ~/.bean and is set programmatically (never via an environment
variable) — `set_bean_home()` exists so tests can point everything at a temp dir.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path

_HOME: Path = Path.home() / ".bean"


def bean_home() -> Path:
    return _HOME


def set_bean_home(path) -> None:
    """Redirect all bean state to `path` (used by tests and any future `--home`-style option)."""
    global _HOME
    _HOME = Path(path)


def repo_root(cwd: Path | None = None) -> Path:
    """The nearest ancestor with a .git dir, else the cwd — the workspace key."""
    p = Path(cwd or Path.cwd()).resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
    return p


class Workspace:
    def __init__(self, root: Path | None = None):
        self.repo = repo_root(root)
        slug = re.sub(r"[^a-z0-9-]+", "-", self.repo.name.lower()).strip("-") or "repo"
        digest = hashlib.sha1(str(self.repo).encode()).hexdigest()[:8]
        self.dir = bean_home() / f"{slug}-{digest}"
        self.dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def global_(cls) -> "Workspace":
        """The shared workspace for *global* connectors — one index under ~/.bean/_global/ that is
        visible from every repo. Same machinery (config.json / DuckDB / Lance) as a repo workspace,
        just not keyed by a repo."""
        ws = cls.__new__(cls)
        ws.repo = None
        ws.dir = bean_home() / "_global"
        ws.dir.mkdir(parents=True, exist_ok=True)
        return ws

    @property
    def is_global(self) -> bool:
        return self.repo is None

    @property
    def db_path(self) -> Path:
        """The small private DuckDB holding only sync cursors (`state`); documents/revisions/edges
        live on the Lance `Catalog` (see `catalog_dir`)."""
        return self.dir / "bean.duckdb"

    @property
    def catalog_dir(self) -> Path:
        """Root of the Lance `Catalog`: the four shared datasets (documents/revisions/edges/chunks)
        as Lance tables under one lancedb dir."""
        return self.dir / "catalog"

    @property
    def lance_dir(self) -> Path:
        # Chunks live in the same lancedb dir as the rest of the Catalog — one `Catalog` owns all
        # four datasets.
        return self.catalog_dir

    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    # -- cloud (S3-backed catalog) awareness -----------------------------------------------------
    @property
    def cloud(self) -> dict:
        """The resolved `cloud` block for this workspace (DEFAULTS <- global <- this workspace's
        `settings.cloud`). Lazy import: `config` imports `bean_home` from this module."""
        from . import config
        return config.resolve(self).get("cloud") or {}

    @property
    def is_cloud(self) -> bool:
        """Cloud-enabled AND the remote actually resolves (a git-backend workspace with no repo,
        or an s3 one with no bucket, is not usably cloud)."""
        return bool(self.cloud.get("enabled")) and self.remote_uri is not None

    @property
    def remote_uri(self) -> str | None:
        """The authoritative catalog's location: `s3://bucket/prefix` for the s3 backend, the
        repo's committed `.bean/catalog` dir for the git backend, or None when not cloud-enabled
        (or the backend can't resolve — no bucket / no repo)."""
        if not self.cloud.get("enabled"):
            return None
        if self.cloud.get("backend") == "git":
            return str(self.repo_bean_dir / "catalog") if self.repo_bean_dir else None
        bucket = self.cloud.get("bucket") or ""
        if not bucket:
            return None
        prefix = (self.cloud.get("prefix") or "").rstrip("/")
        return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"

    @property
    def replica_dir(self) -> Path:
        """The local mirror of the (possibly cloud) catalog — same path as `catalog_dir`."""
        return self.catalog_dir

    # -- config: which sources this repo tracks -------------------------------------------------
    def load_config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return {}

    def save_config(self, config: dict) -> None:
        self.config_path.write_text(json.dumps(config, indent=2) + "\n")

    # -- committed repo config: the shareable side, versioned with the code ---------------------
    @property
    def repo_bean_dir(self) -> Path | None:
        """`<repo>/.bean` — the committed folder (shared config, and the catalog under the git
        storage backend). None for the global workspace."""
        return (self.repo / ".bean") if self.repo else None

    @property
    def repo_config_path(self) -> Path | None:
        d = self.repo_bean_dir
        return (d / "config.json") if d else None

    def load_repo_config(self) -> dict:
        path = self.repo_config_path
        if path is None:
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def save_repo_config(self, config: dict) -> None:
        self.repo_bean_dir.mkdir(parents=True, exist_ok=True)
        self.repo_config_path.write_text(json.dumps(config, indent=2) + "\n")

    def effective_config(self) -> dict:
        """The config sync/status read: the repo's committed `.bean/config.json` with this
        machine's personal workspace config merged on top — dicts merge recursively, tracked-ref
        lists union (committed refs first), personal scalars win. Writes still go to
        `save_config` (personal) or `save_repo_config` (shared) — never through this view."""
        repo_cfg = self.load_repo_config()
        return _overlay(repo_cfg, self.load_config()) if repo_cfg else self.load_config()


def _overlay(base, over):
    """Merge `over` onto `base`: dicts recurse, lists union (base order first), scalars from
    `over` win."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _overlay(base[k], v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(over, list):
        return base + [v for v in over if v not in base]
    return over


# -- connector scope (per user): which sources sync globally vs per-repo -----------------------
# A source is "global" (indexed once, searchable from every repo) or "local" (scoped to the repo
# you run bean in, e.g. a GitHub project). Default is local. Stored at ~/.bean/scopes.json as
# {source_key: "global"|"local"}. Credentials stay global regardless — this only governs which
# workspace holds the tracked items + index.
def _scopes_path() -> Path:
    return bean_home() / "scopes.json"


def load_scopes() -> dict:
    try:
        return json.loads(_scopes_path().read_text())
    except (OSError, ValueError):
        return {}


def save_scopes(scopes: dict) -> None:
    bean_home().mkdir(parents=True, exist_ok=True)
    _scopes_path().write_text(json.dumps(scopes, indent=2) + "\n")


def source_scope(key: str, default: str = "local") -> str:
    return load_scopes().get(key, default)


def set_source_scope(key: str, scope: str) -> None:
    scopes = load_scopes()
    scopes[key] = scope
    save_scopes(scopes)


# -- credentials (scope-aware: shared for global connectors, per-repo for local ones) ----------
# The "credential workspace" in effect for the current auth/sync operation. None (the default) or a
# global workspace means the shared ~/.bean/credentials dir; a repo workspace means that repo's own
# credentials dir, with the shared dir as a load-time fallback.
_cred_ws: contextvars.ContextVar = contextvars.ContextVar("bean_credential_ws", default=None)


@contextmanager
def credential_context(ws):
    """Within this block, credentials resolve against `ws`'s own credentials dir first (falling back
    to the shared dir on load) and new ones save there. Pass the repo workspace for a local
    connector, or None (or a global workspace) for a global one."""
    token = _cred_ws.set(ws)
    try:
        yield
    finally:
        _cred_ws.reset(token)


def _is_local_ws(ws) -> bool:
    return ws is not None and not getattr(ws, "is_global", False)


def _shared_credentials_dir() -> Path:
    d = bean_home() / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _credential_search_dirs() -> list[Path]:
    """Where to look for a credential, most specific first: the local workspace (if one is in
    context) then the shared dir."""
    ws = _cred_ws.get()
    dirs = [ws.dir / "credentials"] if _is_local_ws(ws) else []
    dirs.append(bean_home() / "credentials")
    return dirs


def credential_path(name: str, ws=None) -> Path:
    """Where `name`'s credential lives given the scope: the workspace dir for a local ws, else the
    shared dir. `bean init` prints this so the assistant writes the credential to the right place."""
    w = ws if ws is not None else _cred_ws.get()
    base = w.dir if _is_local_ws(w) else bean_home()
    return base / "credentials" / f"{name}.json"


def load_credential(name: str) -> dict | None:
    for d in _credential_search_dirs():
        try:
            return json.loads((d / f"{name}.json").read_text())
        except (OSError, ValueError):
            continue
    return None


def save_credential(name: str, data: dict) -> Path:
    ws = _cred_ws.get()
    if _is_local_ws(ws):
        d = ws.dir / "credentials"
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    else:
        d = _shared_credentials_dir()
    path = d / f"{name}.json"
    path.unlink(missing_ok=True)  # rewrite so the mode applies even if the file exists
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return path
