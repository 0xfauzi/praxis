"""The installed `praxis` entry point, driven as a subprocess: the real command, the
real console script, a named environment. The one thing Phase 0 ships is checked the one
way the owner will run it."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from praxis import __version__

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _praxis(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    found = shutil.which("praxis")
    assert found is not None, "no `praxis` on PATH: run `uv sync` first"
    # A named environment rather than the inherited one: HOME is the temporary one so
    # that nothing the command does can reach the owner's files.
    return subprocess.run(
        [found, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "HOME": str(home),
            "PRAXIS_HOME": str(home / "praxis-home"),
            "PATH": os.environ.get("PATH", ""),
        },
    )


def test_version_prints_the_package_version_and_writes_nothing_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis("--version", home=home)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"praxis {__version__}"
    assert sorted(home.rglob("*")) == [], "the command wrote under HOME"


def test_no_verb_prints_usage_and_exits_zero(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis(home=home)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("usage: praxis")


def test_the_rejected_drill_verb_is_gone(tmp_path: Path) -> None:
    # Design 03 section 12: the write-it drill (design 02) is deleted, verb and all.
    # argparse refuses an unknown verb with exit 2 and names it.
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis("drill", "--list", home=home)
    assert done.returncode == 2
    assert "invalid choice: 'drill'" in done.stderr
    assert sorted(home.rglob("*")) == [], "the refused verb wrote under HOME"
