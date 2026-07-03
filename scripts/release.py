#!/usr/bin/env python3
"""bean release helper — pure stdlib, no extra deps.

bean ships two things from one repo: the `bean` Python package (pyproject/hatchling) and the Claude
Code plugin (`.claude-plugin/plugin.json` + `skills/` + `scripts/bean.py`). A release keeps their
versions in lockstep, proves the offline test suite is green, builds the wheel/sdist, and tags git.

  python3 scripts/release.py version              # print the current version
  python3 scripts/release.py version X.Y.Z        # set version in pyproject.toml + plugin.json
  python3 scripts/release.py check                # version-sync + offline tests + byte-compile
  python3 scripts/release.py build                # python -m build  ->  dist/*.whl, *.tar.gz
  python3 scripts/release.py cut X.Y.Z [--yes]    # version -> check -> build -> commit + tag vX.Y.Z

`cut` is a dry run unless you pass --yes. Run it from a clean tree; it makes exactly one commit
("release: vX.Y.Z") and one tag ("vX.Y.Z"). See RELEASE.md for the full procedure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
TESTS = ROOT / "tests" / "test_bean.py"
SEMVER = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?$")


def _py() -> str:
    """The venv python if present (deps installed), else the current interpreter."""
    venv = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


# -- version (source of truth: pyproject; plugin.json mirrors it) -------------------------------
def pyproject_version() -> str:
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', PYPROJECT.read_text())
    if not m:
        sys.exit("release: could not find version in pyproject.toml")
    return m.group(1)


def plugin_version() -> str:
    return json.loads(PLUGIN.read_text()).get("version", "")


def set_version(new: str) -> None:
    if not SEMVER.match(new):
        sys.exit(f"release: {new!r} is not a semver like 1.2.3")
    text = PYPROJECT.read_text()
    text, n = re.subn(r'(?m)^(\s*version\s*=\s*")[^"]+(")', rf"\g<1>{new}\g<2>", text, count=1)
    if not n:
        sys.exit("release: no version line in pyproject.toml to update")
    PYPROJECT.write_text(text)
    data = json.loads(PLUGIN.read_text())
    data["version"] = new
    PLUGIN.write_text(json.dumps(data, indent=2) + "\n")
    print(f"release: set version {new} (pyproject.toml + plugin.json)")


# -- steps --------------------------------------------------------------------------------------
def cmd_version(args) -> int:
    if args.value:
        set_version(args.value)
    else:
        print(pyproject_version())
    return 0


def check() -> None:
    pv, gv = pyproject_version(), plugin_version()
    if pv != gv:
        sys.exit(f"release: version mismatch — pyproject {pv} vs plugin.json {gv} "
                 f"(run `release.py version {pv}`)")
    print(f"release: version in sync at {pv}")
    print("release: byte-compiling …")
    subprocess.run([_py(), "-m", "compileall", "-q", str(ROOT / "bean")], check=True)
    print("release: running offline test suite …")
    subprocess.run([_py(), str(TESTS)], check=True)
    print("release: ✓ checks passed")


def cmd_check(args) -> int:
    check()
    return 0


def build() -> None:
    print("release: building wheel + sdist (python -m build) …")
    try:
        subprocess.run([_py(), "-m", "build", str(ROOT)], check=True)
    except subprocess.CalledProcessError:
        sys.exit("release: build failed — is the `build` package installed? "
                 "(`.venv/bin/pip install build`, or `pip install -e '.[dev]'`)")
    dist = sorted((ROOT / "dist").glob("*"))
    print("release: artifacts:")
    for f in dist[-2:]:
        print(f"  {f.relative_to(ROOT)}")


def cmd_build(args) -> int:
    build()
    return 0


def _git(*a) -> str:
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def cmd_cut(args) -> int:
    new = args.value
    if not SEMVER.match(new):
        sys.exit(f"release: {new!r} is not a semver like 1.2.3")
    if _git("status", "--porcelain"):
        sys.exit("release: working tree is not clean — commit or stash first.")
    tag = f"v{new}"
    existing = _git("tag", "--list", tag)
    if existing:
        sys.exit(f"release: tag {tag} already exists.")
    plan = [f"set version -> {new}", "run checks (tests + compile)", "build wheel + sdist",
            f'git commit -am "release: {tag}"', f"git tag {tag}"]
    print(f"release plan for {tag}:")
    for i, step in enumerate(plan, 1):
        print(f"  {i}. {step}")
    if not args.yes:
        print("\n(dry run — re-run with --yes to execute)")
        return 0
    set_version(new)
    check()
    build()
    _git("commit", "-am", f"release: {tag}")
    _git("tag", tag)
    print(f"\nrelease: ✓ committed and tagged {tag}. Push with:  git push && git push origin {tag}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="release", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("version", help="print or set the version")
    v.add_argument("value", nargs="?")
    v.set_defaults(fn=cmd_version)
    sub.add_parser("check", help="version-sync + tests + compile").set_defaults(fn=cmd_check)
    sub.add_parser("build", help="build wheel + sdist into dist/").set_defaults(fn=cmd_build)
    c = sub.add_parser("cut", help="version -> check -> build -> commit + tag")
    c.add_argument("value")
    c.add_argument("--yes", action="store_true", help="actually do it (default is a dry run)")
    c.set_defaults(fn=cmd_cut)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
