"""Plugin discovery: how a connector that isn't in the core set becomes live.

Drop-in plugins are resolved by `discover_sources` and appended to the registry after the core
connectors (but before `localfiles`, which stays the path catch-all): every `*.py` under a plugin
dir (default `~/.bean/plugins/`, plus any paths in `plugins.paths`). A plugin module exposes ONE
of: a `SOURCE` (a Source), a `SOURCES` list, or a `register() -> Source | list[Source]`. It builds
those with `from bean.sources import Source` and calls the same `bean.http`/`bean.store` helpers the
bundled connectors do. See `docs/authoring-connectors.md` for the authoring guide + template.

A broken plugin file is logged and skipped — it never takes down the registry."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .workspace import bean_home


def _warn(msg: str) -> None:
    print(f"bean: plugin load — {msg}", file=sys.stderr)


def plugin_dirs(config: dict | None = None) -> list[Path]:
    dirs = [bean_home() / "plugins"]
    for p in ((config or {}).get("plugins") or {}).get("paths") or []:
        dirs.append(Path(p).expanduser())
    return dirs


def _sources_from_module(mod, Source) -> list:
    if hasattr(mod, "SOURCES"):
        got = list(mod.SOURCES)
    elif hasattr(mod, "SOURCE"):
        got = [mod.SOURCE]
    elif hasattr(mod, "register") and callable(mod.register):
        r = mod.register()
        got = list(r) if isinstance(r, (list, tuple)) else [r]
    else:
        _warn(f"{getattr(mod, '__name__', mod)} exposes no SOURCE / SOURCES / register() — skipped")
        return []
    return [s for s in got if isinstance(s, Source)]


def _load_plugin_file(path: Path, Source) -> list:
    name = f"bean_plugin_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod  # so dataclasses / typing in the plugin resolve
        spec.loader.exec_module(mod)
        return _sources_from_module(mod, Source)
    except Exception as err:  # noqa: BLE001 — one bad plugin must not break the registry
        _warn(f"{path.name} failed to load ({err})")
        return []


def discover_sources(Source, *, global_config=None, dirs=None) -> list:
    """Sources contributed by drop-in plugin files. `Source` is passed in to avoid an import cycle
    with sources.py."""
    if global_config is None:
        from . import config as cfgmod
        global_config = cfgmod.load_global()
    out: list = []

    # drop-in plugin files
    seen: set = set()
    for d in (dirs if dirs is not None else plugin_dirs(global_config)):
        d = Path(d)
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name == "__init__.py" or f.name in seen:
                continue
            seen.add(f.name)
            out.extend(_load_plugin_file(f, Source))
    return out
