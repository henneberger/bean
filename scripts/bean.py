#!/usr/bin/env python3
"""bean wrapper: self-bootstrapping launcher for the Claude plugin.

Ensures a virtualenv at <plugin>/.venv with bean's dependencies installed (first run only,
stamped by pyproject.toml's mtime so dependency changes reinstall), then runs `bean` inside
it with the given arguments. Pure stdlib, so `python3 scripts/bean.py …` always works.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
STAMP = VENV / ".bean-stamp"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_venv() -> None:
    stamp = str(int((ROOT / "pyproject.toml").stat().st_mtime))
    if venv_python().exists() and STAMP.exists() and STAMP.read_text() == stamp:
        return
    print("bean: first-time setup — creating a virtualenv and installing dependencies "
          "(a few minutes, once)…", file=sys.stderr)
    if not venv_python().exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(venv_python()), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                   check=True)
    subprocess.run([str(venv_python()), "-m", "pip", "install", "--quiet", "-e", str(ROOT)],
                   check=True)
    STAMP.write_text(stamp)


def main() -> int:
    ensure_venv()
    return subprocess.run([str(venv_python()), "-m", "bean", *sys.argv[1:]]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
