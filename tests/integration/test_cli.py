"""The installed `praxis` entry point, driven as a subprocess: the real command, the
real console script, a named environment. Every command is checked the one way the
owner will run it: the wiring from argv to the module that owns the command is what is
tested here; each module's behaviour is tested beside it."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
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
# The wave 4 grammar (verb first, then the object), written out as the oracle in the
# order that the usage prints it.
GRAMMAR = [
    "praxis show status",
    "praxis show install AGENT",
    "praxis install AGENT",
    "praxis remove AGENT",
    "praxis make components [--write]",
    "praxis set components [--reason REASON]",
    "praxis show components",
    "praxis start task",
    "praxis show task",
    "praxis test structure",
    "praxis test changes",
    "praxis test decisions",
    "praxis test knockout",
    "praxis test pathfinder",
    "praxis show scenarios",
    "praxis run scenarios",
    "praxis show report",
    "praxis show lines [--session ID] [--json]",
    "praxis show sessions",
    "praxis hook",
]
# Every name the grammar replaced. There are no aliases: one term for one concept.
OLD_NAMES = [
    ("setup", "claude", "--print"),
    ("doctor",),
    ("attributions",),
    ("sessions",),
    ("components", "freeze"),
    ("task",),
    ("last",),
    ("probe", "structural"),
    ("scenarios", "run"),
    ("report",),
    ("drill", "--list"),
]


def _praxis(
    *args: str,
    home: Path,
    cwd: Path | None = None,
    stdin: str = "",
    env: dict[str, str] | None = None,
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
            **(env or {}),
        },
    )


def test_version_imports_only_the_command_module_and_writes_nothing_home(
    tmp_path: Path,
) -> None:
    # The knockout tracer counts a module body that runs at import as entered, and
    # `praxis hook` pays for every import: the command surface imports a module only
    # inside the handler that uses it.
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis("--version", home=home, env={"PYTHONPROFILEIMPORTTIME": "1"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"praxis {__version__}"
    loaded = re.findall(r"^import time:.*\|\s+(praxis\S*)$", done.stderr, re.M)
    assert sorted(loaded) == ["praxis", "praxis.cli"]
    assert sorted(home.rglob("*")) == [], "the command wrote under HOME"


def test_no_verb_prints_one_short_line_per_command_and_exits_zero(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis(home=home)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("usage: praxis VERB OBJECT [OPTIONS]\n")
    rows = [
        re.split(r"\s{2,}", line.strip(), maxsplit=1)
        for line in done.stdout.splitlines()
        if line.startswith("  praxis ")
    ]
    assert [command for command, _help in rows] == GRAMMAR
    for command, help_line in rows:
        assert len(help_line.split()) <= 20, command
    assert done.stdout.endswith("  AGENT is claude or codex.\n")


@pytest.mark.parametrize("old", OLD_NAMES, ids=" ".join)
def test_an_old_name_exits_2_with_the_usage(
    tmp_path: Path, old: tuple[str, ...]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis(*old, home=home)
    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith("usage: praxis VERB OBJECT [OPTIONS]\n")
    assert "  praxis show status  " in done.stderr
    assert done.stderr.endswith(f"\npraxis {old[0]} is not a command.\n")
    assert sorted(home.rglob("*")) == [], "the refused command wrote under HOME"


@pytest.mark.parametrize(
    ("argv", "last_line", "usage_lines"),
    [
        (("show", "foo"), "praxis show foo is not a command.", 8),
        (("test", "structural"), "praxis test structural is not a command.", 5),
        (("show",), "praxis show: the object is missing.", 8),
        (("install",), "praxis install: the agent is missing. Write claude or codex.",
         1),
        (("install", "cursor"), "praxis install cursor is not a command.", 1),
        (("show", "install", "x"), "praxis show install x is not a command.", 1),
        (
            ("show", "status", "--json"),
            "praxis show status: these words are not part of a command: --json",
            1,
        ),
        (("show", "lines", "--session"), "praxis show lines: write a value after"
         " --session.", 1),
    ],
)  # fmt: skip
def test_a_wrong_object_prints_the_allowed_commands(
    tmp_path: Path, argv: tuple[str, ...], last_line: str, usage_lines: int
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis(*argv, home=home)
    assert (done.returncode, done.stdout) == (2, "")
    lines = done.stderr.splitlines()
    assert lines[0] == "usage: praxis VERB OBJECT [OPTIONS]"
    assert lines[-1] == last_line
    listed = [line for line in lines if line.startswith("  praxis ")]
    assert len(listed) == usage_lines, done.stderr
    assert all(line.split()[1] == argv[0] for line in listed), done.stderr
    assert sorted(home.rglob("*")) == [], "the refused command wrote under HOME"


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


class Ledger:
    def __init__(self, home: Path, repo_id: str) -> None:
        self.path = home / "praxis-home" / "ledger.db"
        self.repo_id = repo_id

    def counts(self) -> dict[str, object]:
        ledger = Store(self.path).open()
        try:
            frozen = ledger.component_map_frozen(self.repo_id)
            version = None if frozen is None else json.loads(frozen)["version"]
            items = ledger.probe_items_d1(self.repo_id, version) if version else []
            flags = ledger.db.execute("SELECT code, detail_json FROM flags").fetchall()
            return {
                "map": version,
                "items": len(items),
                "tasks": [
                    (r["arm"], r["status"]) for r in ledger.task_rows(self.repo_id)
                ],
                "responses": [
                    (r["task_id"], r["exposed_task"], r["p"])
                    for r in ledger.probe_responses_of(self.repo_id, version)
                ] if version else [],
                "abandoned": [
                    json.loads(d)["kind"] for c, d in flags if c == "abandoned"
                ],
                "hook events": len(ledger.hook_event_rows()),
            }  # fmt: skip
        finally:
            ledger.close()


def test_every_command_reaches_its_module(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo, repo_id = _control_repo(tmp_path)
    ledger = Ledger(home, repo_id)
    typed = f"{CANARY}\n"

    def run(*args: str, stdin: str = typed) -> subprocess.CompletedProcess[str]:
        return _praxis(*args, home=home, cwd=repo, stdin=stdin)

    done = run("start", "task")
    assert done.returncode == 2, done.stdout
    assert ledger.counts()["tasks"] == [], "a task was recorded with no set map"
    done = run("set", "components")
    assert done.returncode == 0, done.stderr
    state = ledger.counts()
    version = state["map"]
    assert (isinstance(version, str), state["items"]) == (True, 30)
    done = run("show", "components")
    assert done.returncode == 0, done.stderr
    assert str(version) in done.stdout
    done = run("set", "components", "--reason", "whim")
    assert done.returncode == 2
    assert ledger.counts()["map"] == version
    where = home / "praxis-home" / "scenarios" / f"{repo_id}.toml"
    for verb, code in (("show", 0), ("run", 2)):
        done = run(verb, "scenarios")
        assert done.returncode == code, done.stdout
        assert str(where) in done.stdout
    done = run("start", "task")
    assert done.returncode == 0, done.stderr
    done = run("show", "task")
    assert done.returncode == 0, done.stderr
    assert ledger.counts()["tasks"] == [("control", "unbound")]
    # Nothing exposed is due, so the two never-drawn validation items are asked.
    done = run("test", "structure", stdin=f"s\n{CANARY}\ns\n")
    assert done.returncode == 0, done.stderr
    assert ledger.counts()["responses"] == [(None, None, None)] * 2
    for obj, stdin in (("changes", "s\ns\n"), ("pathfinder", "s\n")):
        done = run("test", obj, stdin=stdin)
        assert done.returncode == 0, done.stderr
    assert ledger.counts()["abandoned"] == ["drift", "pathfinder"]
    done = run("test", "decisions")
    assert done.returncode == 0, done.stderr
    assert "decision" in done.stdout.lower()
    done = run("test", "knockout")  # no scenario run is recorded: a refusal
    assert done.returncode == 2, done.stdout
    assert ledger.counts()["responses"] == [(None, None, None)] * 2
    done = run("show", "report")
    assert done.returncode == 0, done.stderr
    assert f"repository {repo_id}" in done.stdout
    done = run("hook", stdin="{}")
    assert (done.returncode, done.stdout) == (0, "")
    assert ledger.counts()["hook events"] == 1
    done = run("show", "status")
    assert done.returncode == 0, done.stderr
    for line in (
        "  hook events with the outcome unobservable or error: 1",
        f"  repository {repo_id}: set component map {version} (mechanical)",
        "  binding rate: 0 of 1 recorded tasks have the status bound or closed",
        "  tasks by status: unbound 1",
        "  void reasons: none",
        "  flags by code: abandoned 2",
        f"current directory: repository {repo_id}",
        "agents attached: 0",
        "items due for `praxis test structure` (7 to 28 days after exposure): 0",
    ):
        assert line in done.stdout.splitlines(), line
    assert run("show", "lines").stdout == (
        "session  ts  trigger  file  agent  human  preexisting  ambiguous  reverted"
        "  coverage\n"
    )
    assert run("show", "lines", "--json").stdout == ""
    assert run("show", "sessions").stdout == (
        "session  hook events  agent  human  preexisting  ambiguous  reverted"
        "  (at the last hook event)\n"
    )
    owner_map = home / "praxis-home" / "components" / f"{repo_id}.toml"
    done = run("make", "components")
    assert done.returncode == 0, done.stderr
    assert 'id = "a"' in done.stdout
    assert not owner_map.exists()
    assert run("make", "components", "--write").returncode == 0
    assert owner_map.exists()

    files = [p for p in (home / "praxis-home").rglob("*") if p.is_file()]
    assert files, "nothing was written under $PRAXIS_HOME"
    for path in files:
        assert CANARY.encode() not in path.read_bytes(), path.name


def test_report_outside_a_repository_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    done = _praxis("show", "report", home=home, cwd=home)
    assert (done.returncode, done.stdout) == (
        2, "praxis show report: run this command in a git repository.\n")  # fmt: skip
    assert not (home / "praxis-home").exists(), "the refusal created the ledger"
