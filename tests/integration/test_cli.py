"""The installed `praxis` entry point, driven as a subprocess: the real command, the
real console script, a named environment. Every verb is checked the one way the owner
will run it: the wiring from argv to the module that owns the verb is what is tested
here; each module's behaviour is tested beside it."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from praxis import __version__, task
from praxis.store import Store
from praxis.unassisted import locate

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

CANARY = "CANARYCLI3b8d1"


def _praxis(
    *args: str, home: Path, cwd: Path | None = None, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
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
        cwd=cwd,
        input=stdin,
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


def _arm(repo_id: str, ordinal: int) -> str:
    # task.py's rule, written out here as the oracle: sha256 of [seed, repo_id,
    # ordinal], treatment when the first random() is below 0.5.
    key = json.dumps([task.SEED, repo_id, ordinal]).encode()
    rng = random.Random(int(hashlib.sha256(key).hexdigest(), 16))
    return "treatment" if rng.random() < 0.5 else "control"


def _control_repo(tmp_path: Path) -> tuple[Path, str]:
    """A six-module repository whose first task the committed seed puts in control.
    repo_id hashes the path, so the path is what is searched."""
    for n in range(64):
        root = tmp_path / f"repo{n}"
        pkg = root / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        for m in "abcdef":
            (pkg / f"{m}.py").write_text(f"NAME = {m!r}\n")
        (pkg / "a.py").write_text("from pkg import b\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        found = locate(root)
        assert found is not None
        if _arm(found.repo_id, 1) == "control":
            return root, found.repo_id
    raise AssertionError("no control repository in 64 paths")


def test_every_measurement_verb_reaches_its_module(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo, repo_id = _control_repo(tmp_path)
    typed = f"{CANARY}\n"

    def run(*args: str, stdin: str = typed) -> subprocess.CompletedProcess[str]:
        return _praxis(*args, home=home, cwd=repo, stdin=stdin)

    done = run("task")
    assert (done.returncode, done.stdout) == (
        2, "praxis task: no frozen component map; run `praxis components freeze` "
        "first\n")  # fmt: skip
    done = run("components", "freeze")
    assert done.returncode == 0, done.stderr
    assert "6 product components" in done.stdout
    assert "D1 items: 30 written at epoch 0" in done.stdout
    done = run("components")
    assert done.returncode == 0, done.stderr
    assert "pool: 30 items (1 true), 30 unexposed" in done.stdout
    done = run("components", "freeze", "--reason", "whim")
    assert done.returncode == 2
    assert "invalid choice: 'whim'" in done.stderr
    where = home / "praxis-home" / "scenarios" / f"{repo_id}.toml"
    done = run("scenarios")
    assert (done.returncode, done.stdout) == (
        0, f"no scenarios configured; {where} names them\n")  # fmt: skip
    done = run("scenarios", "run")
    assert (done.returncode, done.stdout) == (
        2, f"praxis scenarios run: no scenarios; {where} names them\n")  # fmt: skip
    done = run("task")
    assert (done.returncode, done.stdout) == (0, "no closed task yet\nregistered\n")
    done = run("last")
    assert (done.returncode, done.stdout) == (0, "no closed task yet\n")
    # Nothing exposed is due, so the two never-drawn validation items are asked.
    done = run("probe", "structural", stdin=f"s\n{CANARY}\ns\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("2 items due, one at a time")
    assert done.stdout.endswith(
        "] answered 2; 0 could not be scored now and stay due\n"
    )
    assert done.stdout.count("  not one of the answers offered") == 1
    done = run("probe", "recall")
    assert done.returncode == 2
    assert "kinds are structural, drift, decision, knockout, pathfinder" in done.stderr
    done = run("report")
    assert done.returncode == 0, done.stderr
    assert f"repository {repo_id}" in done.stdout
    assert "P1 bound control: n 0, tasks 0, refused below_minimum" in done.stdout
    assert "rule PR-1 not met" in done.stdout
    done = run("doctor")
    assert done.returncode == 0, done.stderr
    for line in (
        "  binding rate (bound or closed over registered): 0 of 1",
        "  tasks by status: unbound 1",
        "  void reasons: none",
        "  flags by code: none",
        "probes due now (structural, 7 to 28 days after exposure): 0",
    ):
        assert line in done.stdout.splitlines(), line

    ledger = Store(home / "praxis-home" / "ledger.db").open()
    try:
        (registered,) = ledger.task_rows(repo_id)
        version = ledger.task_rows(repo_id)[0]["map_version"]
        asked = ledger.probe_responses_of(repo_id, version)
    finally:
        ledger.close()
    assert (registered["arm"], registered["status"]) == ("control", "unbound")
    assert [(r["task_id"], r["exposed_task"], r["p"]) for r in asked] == [
        (None, None, None)] * 2  # fmt: skip
    files = [p for p in (home / "praxis-home").rglob("*") if p.is_file()]
    assert files, "nothing was written under $PRAXIS_HOME"
    for path in files:
        assert CANARY.encode() not in path.read_bytes(), path.name


def test_report_outside_a_repository_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis("report", home=home, cwd=home)
    assert (done.returncode, done.stdout) == (
        2, "praxis report: not inside a git repository\n")  # fmt: skip
    assert not (home / "praxis-home").exists(), "the refusal created the ledger"
