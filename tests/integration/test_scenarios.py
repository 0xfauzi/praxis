"""`praxis scenarios run`, `praxis scenarios` and the doctor's scenarios line, driven
through the installed binary against a real git repository and a real $PRAXIS_HOME,
plus the tree_hash column those runs write. Every expected value is written here, from
the files planted, before the run.

The planted package `app` (src layout) has five product components: `.` (a non-empty
__init__.py), main, lib, a and b. `python3 -m app.main` enters `.` (the package),
main and lib; main calls into lib twice (lib's module body on import, then lib.f), so
the one call edge is main -> lib with count 2. The owner's PYTHONPATH points at the
owner's own src/ and the owner's main.py holds an uncommitted import of `a`, so a run
that imported the owner's tree instead of the clone's would show neither the clone's
frames nor the clone's components.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from typing import TYPE_CHECKING

import pytest

from praxis.store import Evidence, Store
from praxis.unassisted import locate

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

CANARY = "CANARYSCENARIOe7a1"
FILES = {
    "src/app/__init__.py": '"""The app."""\n',
    "src/app/main.py": (
        "import sys\n\nfrom app import lib\n\nlib.f()\n"
        "if '--fail' in sys.argv:\n    sys.exit(3)\n"
    ),
    "src/app/lib.py": "def f():\n    return 1\n",
    "src/app/a.py": "X = 1\n",
    "src/app/b.py": "Y = 2\n",
    "README.md": "app\n",
}
ARGV = {
    "main": ["python3", "-m", "app.main", CANARY],
    "main-fails": ["python3", "-m", "app.main", "--fail"],
    "no-python": ["true"],
}
TRACED = [".", "lib", "main"]
EDGES = [["main", "lib", 2]]
# scenario id: (outcome, exit code, components, call edges, coverage, warnings)
EXPECTED = {
    "main": ("ok", 0, TRACED, EDGES, "full", []),
    "main-fails": ("failed", 3, TRACED, EDGES, "partial", ["outcome_failed"]),
    "no-python": ("not_python", 0, [], [], "unobservable", ["outcome_not_python"]),
}


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def plant(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "plant")


def scenario_toml(argv: dict[str, list[str]]) -> str:
    return "".join(
        f"[[scenario]]\nid = {sid!r}\nargv = {json.dumps(a)}\n"
        for sid, a in argv.items()
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    plant(root, FILES)
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def praxis(
    *args: str, home: Path, cwd: Path, pythonpath: str | None = None
) -> subprocess.CompletedProcess[str]:
    found = shutil.which("praxis")
    assert found is not None, "no `praxis` on PATH: run `uv sync` first"
    env = {
        "HOME": str(home),
        "PRAXIS_HOME": str(home / "praxis-home"),
        "PATH": os.environ.get("PATH", ""),
    }
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [found, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=cwd,
        env=env,
    )


def ledger_rows(praxis_home: Path, sql: str) -> list[dict[str, object]]:
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        return [dict(r) for r in ledger.db.execute(sql)]
    finally:
        ledger.close()


def write_scenarios(praxis_home: Path, repo: Path, text: str) -> Path:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    path = praxis_home / "scenarios" / f"{repo_id}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def freeze(repo: Path, home: Path) -> str:
    done = praxis("components", "freeze", home=home, cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr
    first = done.stdout.splitlines()[0]
    assert first.endswith("frozen (mechanical), 5 product components"), first
    return first.split()[1]


def ledger_bytes(praxis_home: Path) -> bytes:
    """Every file under $PRAXIS_HOME but the owner's own scenario files, the one
    place an argv lives (design 3.2)."""
    return b"".join(
        p.read_bytes()
        for p in sorted(praxis_home.rglob("*"))
        if p.is_file() and p.parent != praxis_home / "scenarios"
    )


def git_state(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in (root / ".git").rglob("*")
        if p.is_file()
    }


def test_run_traces_the_clone_and_records_one_row_per_scenario(
    repo: Path, home: Path
) -> None:
    praxis_home = home / "praxis-home"
    version = freeze(repo, home)
    where = write_scenarios(praxis_home, repo, scenario_toml(ARGV))
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    (repo / "src/app/main.py").write_text(
        "from app import a\n" + FILES["src/app/main.py"]
    )
    owner_main = (repo / "src/app/main.py").read_bytes()
    before = git_state(repo)

    done = praxis("scenarios", "run", home=home, cwd=repo, pythonpath=str(repo / "src"))

    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == f"scenarios on map {version}, each in a scratch clone of HEAD"
    assert [line.rsplit(",", 1)[0] for line in lines[1:]] == [
        f"  main: ok (exit 0) on tree {tree[:12]}, components . lib main, 1 call edges",
        f"  main-fails: failed (exit 3) on tree {tree[:12]}, components . lib main,"
        " 1 call edges",
        f"  no-python: not_python (exit 0) on tree {tree[:12]}, components none seen,"
        " 0 call edges",
    ]
    runs = ledger_rows(praxis_home, "SELECT * FROM scenario_runs ORDER BY id")
    assert [r["scenario_id"] for r in runs] == list(ARGV)
    for r in runs:
        sid = str(r["scenario_id"])
        outcome, code, components, edges, coverage, warnings = EXPECTED[sid]
        assert (r["outcome"], r["exit_code"]) == (outcome, code), sid
        assert json.loads(str(r["components_json"])) == components, sid
        assert json.loads(str(r["call_edges_json"])) == edges, sid
        assert (r["coverage"], json.loads(str(r["warnings"]))) == (coverage, warnings)
        assert r["tree_hash"] == tree
        assert (r["map_version"], r["claim_class"]) == (version, "derived")
        assert r["extractor_version"] == "x2-v1"
        digest = hashlib.sha256(" ".join(ARGV[sid]).encode()).hexdigest()[:16]
        assert r["command_hash"] == digest
        assert json.loads(str(r["sources"])) == [
            f"scenario:{sid}",
            f"tree:{tree}",
            f"map:{version}",
        ]
    assert list((praxis_home / "scratch").iterdir()) == [], "a clone was left behind"
    assert git_state(repo) == before, "a scenario wrote under the owner's .git"
    assert (repo / "src/app/main.py").read_bytes() == owner_main
    assert CANARY.encode() not in ledger_bytes(praxis_home), "argv reached the ledger"

    shown = praxis("scenarios", home=home, cwd=repo)
    assert shown.returncode == 0, shown.stderr
    ts = runs[0]["ts"]
    assert shown.stdout.splitlines() == [
        f"3 scenarios in {where}",
        f"  main: last run {ts} ok on tree HEAD, components . lib main",
        f"  main-fails: last run {ts} failed on tree HEAD, components . lib main",
        f"  no-python: last run {ts} not_python on tree HEAD, components none seen",
    ]
    doctor = praxis("doctor", home=home, cwd=repo).stdout.splitlines()
    assert (
        f"scenarios: 3 configured, 3 run on map {version}; last run {ts}"
        " (failed 1, not_python 1, ok 1)" in doctor
    )

    git(repo, "commit", "-qam", "owner work")
    write_scenarios(
        praxis_home, repo, scenario_toml({**ARGV, "main": ["python3", "-m", "app.b"]})
    )
    lines = praxis("scenarios", home=home, cwd=repo).stdout.splitlines()
    assert lines[1] == (
        f"  main: last run {ts} ok on tree {tree[:12]}, components . lib main;"
        " argv changed"
    )


def test_refusals_write_nothing(repo: Path, home: Path) -> None:
    praxis_home = home / "praxis-home"
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    where = praxis_home / "scenarios" / f"{repo_id}.toml"
    verb = "praxis scenarios run"

    done = praxis("scenarios", "run", home=home, cwd=repo)
    assert (done.returncode, done.stdout) == (
        2,
        f"{verb}: no scenarios; {where} names them\n",
    )
    shown = praxis("scenarios", home=home, cwd=repo)
    assert (shown.returncode, shown.stdout) == (
        0,
        f"no scenarios configured; {where} names them\n",
    )
    write_scenarios(praxis_home, repo, scenario_toml(ARGV))
    done = praxis("scenarios", "run", home=home, cwd=repo)
    assert (done.returncode, done.stdout) == (
        2,
        f"{verb}: no frozen map; `praxis components freeze` freezes one\n",
    )
    assert not (praxis_home / "ledger.db").exists(), "a refusal created the ledger"

    freeze(repo, home)
    id_rule = (
        "a scenario id is 1 to 64 of a-z 0-9 . _ - and starts with a letter or a digit"
    )
    one = '[[scenario]]\nid = "x"\nargv = ["true"]\n'
    malformed = {
        "argv = [\n": "not TOML (",
        "other = 1\n": "expected [[scenario]] tables and nothing else",
        one + "[settings]\n": "expected [[scenario]] tables and nothing else",
        one + "extra = 1\n": "each [[scenario]] has exactly `id` and `argv`",
        one.replace('"x"', f'"{CANARY} typed"'): id_rule,
        one.replace('["true"]', '"true"'): "scenario 'x' argv is not a list of strings",
        one.replace('["true"]', "[]"): "scenario 'x' argv names no command",
        one.replace('["true"]', '["", "true"]'): "scenario 'x' argv names no command",
        one + one: "scenario ids repeated: ['x']",
    }
    for text, why in malformed.items():
        write_scenarios(praxis_home, repo, text)
        done = praxis("scenarios", "run", home=home, cwd=repo)
        assert done.returncode == 2, text
        assert done.stdout.startswith(f"{verb}: {where}: {why}"), done.stdout
        assert CANARY not in done.stdout
        shown = praxis("scenarios", home=home, cwd=repo)
        assert shown.returncode == 2
        assert shown.stdout.startswith(f"praxis scenarios: {where}: {why}")
        doctor = praxis("doctor", home=home, cwd=repo).stdout.splitlines()
        assert f"scenarios: {where} refused; `praxis scenarios`" in doctor
    assert ledger_rows(praxis_home, "SELECT * FROM scenario_runs") == []
    assert list((praxis_home / "scratch").glob("*")) == []
    assert CANARY.encode() not in ledger_bytes(praxis_home)


def test_tree_hash_is_required_except_for_an_untraced_run(
    repo: Path, home: Path
) -> None:
    praxis_home = home / "praxis-home"
    version = freeze(repo, home)
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    ev = Evidence("full", ["scenario:x"], "test", [], [])

    def put(ledger: Store, outcome: str, code: int, tree: str | None) -> None:
        ledger.scenario_run(
            components=[], call_edges={}, ev=ev, repo_id=repo_id,
            map_version=version, ts="t", scenario_id="x", command_hash="0" * 16,
            outcome=outcome, exit_code=code, duration_ms=1.0,
            extractor_version="x2-v1", tree_hash=tree,
        )  # fmt: skip

    ledger = Store(praxis_home / "ledger.db").open()
    try:
        refused = [("ok", 0, None), ("failed", 1, None), ("ok", 0, "z" * 40),
                   ("ok", 0, "a" * 41)]  # fmt: skip
        for outcome, code, tree in refused:
            with pytest.raises(sqlite3.IntegrityError):
                put(ledger, outcome, code, tree)
        put(ledger, "not_python", 0, None)
        put(ledger, "ok", 0, "a" * 40)
        put(ledger, "failed", 1, "b" * 64)
        assert [r["tree_hash"] for r in ledger.scenario_runs_for(repo_id, version)] == [
            None, "a" * 40, "b" * 64]  # fmt: skip

        # A ledger from before the column: migrate adds it, with the not_python rule
        # only where no run is stored yet.
        db = ledger.db
        db.execute("DROP TRIGGER scenario_runs_delete_never")
        db.execute("DELETE FROM scenario_runs")
        db.execute("ALTER TABLE scenario_runs DROP COLUMN tree_hash")
        db.execute("PRAGMA user_version = 0")
    finally:
        ledger.close()
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            put(ledger, "ok", 0, None)
        put(ledger, "ok", 0, "c" * 40)
        ledger.db.execute("ALTER TABLE scenario_runs DROP COLUMN tree_hash")
        ledger.db.execute("PRAGMA user_version = 0")
    finally:
        ledger.close()
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        put(ledger, "ok", 0, None)  # rows before the column: the rule cannot hold
        with pytest.raises(sqlite3.IntegrityError):
            put(ledger, "ok", 0, "z" * 40)
        stored = [r["tree_hash"] for r in ledger.scenario_runs_for(repo_id, version)]
        assert stored == [None, None]
    finally:
        ledger.close()
