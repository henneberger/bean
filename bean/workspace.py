"""Per-repo workspaces under ~/.bean.

Every repo gets its own folder — <repo-name>-<hash-of-path>/ — holding that repo's config,
DuckDB catalog, and Lance vector store. Credentials are per USER (shared across repos) at
~/.bean/credentials/, mode 0600. Nothing is ever written inside the repo itself.

The home directory defaults to ~/.bean and is set programmatically (never via an environment
variable) — `set_bean_home()` exists so tests can point everything at a temp dir.
"""

from __future__ import annotations

import hashlib
import json
import re
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

    @property
    def db_path(self) -> Path:
        return self.dir / "bean.duckdb"

    @property
    def lance_dir(self) -> Path:
        return self.dir / "lance"

    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    # -- config: which sources this repo tracks -------------------------------------------------
    def load_config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return {}

    def save_config(self, config: dict) -> None:
        self.config_path.write_text(json.dumps(config, indent=2) + "\n")


# -- credentials (per user, never per repo) ----------------------------------------------------
def _credentials_dir() -> Path:
    d = bean_home() / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def load_credential(name: str) -> dict | None:
    try:
        return json.loads((_credentials_dir() / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None


def save_credential(name: str, data: dict) -> Path:
    path = _credentials_dir() / f"{name}.json"
    path.unlink(missing_ok=True)  # rewrite so the mode applies even if the file exists
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return path
