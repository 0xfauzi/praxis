"""The extractors, run on real files: X1 on a real git repository built here and on
praxis's own source at a pinned commit (where docs/design/03-measurement.md section 3.1
recorded the counts by hand, so the expectation predates this code), X2 on a real
two-module program run through a real interpreter and a real shell, X3 on real DDL and
on praxis's own store.SCHEMA. Expected values are written before each run.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from praxis import store
from praxis.extract import (
    Edge,
    Scenario,
    StaticGraph,
    reach,
    run_scenario,
    scratch_clone,
    sqlite_schema,
    static_imports,
)

pytestmark = pytest.mark.integration

PRAXIS_ROOT = Path(__file__).resolve().parents[2]
# The commit at which the design measured X1 on src/praxis by hand.
MEASURED_AT = "697b332720424f8c270fd5c5bb8fc315561e245b"
PRAXIS_COMPONENTS = ["cli", "doctor", "drill", "gitdiff", "hooks", "install"]
PRAXIS_COMPONENTS += ["recorder", "spans", "store"]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return done.stdout


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


COMMITTED = {
    "pkg/__init__.py": "from .core import run\n",
    "pkg/core.py": (
        "import os\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from pathlib import Path\n"
        "from pkg import util\n"
        "from . import util as u2\n\n"
        "def run():\n"
        "    from .sub import deep\n"
        "    return deep.go()\n"
    ),
    "pkg/util.py": (
        "import importlib\n\n"
        "def load(name):\n    return importlib.import_module(name)\n\n"
        "def load2():\n    return __import__('json')\n"
    ),
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from pkg.core import run\n"
        "from ..util import load\n\n"
        "def go():\n    return load\n"
    ),
    "pkg/broken.py": "def broken(:\n",
    "pkg/gone.py": "from pkg import util\n",
    "tests/test_core.py": "from pkg.core import run\nimport pkg.sub.deep\n",
    "scripts/a/main.py": "VALUE = 2\n",
    "tools/main.py": "VALUE = 1\n",
    "scripts/run.py": "import main\nfrom .. import nothing\n",
    ".gitignore": "pkg/ignored.py\n",
}
UNTRACKED = {
    "pkg/extra.py": "class K:\n    from pkg.core import run\n",
    "pkg/ignored.py": "from pkg import util\n",
}


def built_assign(path: str) -> str:
    parts = path.split("/")
    if parts[0] in ("tests", "scripts", "tools"):
        return parts[0]
    if parts[:2] == ["pkg", "sub"]:
        return "sub"
    if parts[0] == "pkg" and len(parts) == 2:
        return "pkg/." if parts[1] == "__init__.py" else parts[1][:-3]
    return "unmapped"


@pytest.fixture
def built(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    write(repo, COMMITTED)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    (repo / "pkg" / "gone.py").unlink()
    write(repo, UNTRACKED)
    return repo


def test_x1_built_repository(built: Path) -> None:
    graph = static_imports(built, built_assign)
    assert graph.edges == (
        Edge("core", "sub", 1, False),
        Edge("core", "util", 2, True),
        Edge("extra", "core", 1, True),
        Edge("pkg/.", "core", 1, True),
        Edge("sub", "core", 1, False),
        Edge("sub", "util", 1, True),
        Edge("tests", "core", 1, True),
        Edge("tests", "sub", 1, True),
    )
    # sub's one TYPE_CHECKING import resolves (to core): an edge, not a blind spot.
    assert graph.blind_spots == {
        "core": 0, "extra": 0, "pkg/.": 0, "scripts": 1,
        "sub": 0, "tests": 0, "tools": 0, "util": 2,
    }  # fmt: skip
    assert graph.type_only == {
        "core": 0, "extra": 0, "pkg/.": 0, "scripts": 0,
        "sub": 1, "tests": 0, "tools": 0, "util": 0,
    }  # fmt: skip
    assert graph.parse_failures == ("pkg/broken.py",)
    assert graph.extractor_version == "x1-v2"
    assert reach(graph, ["core", "util", "sub", "extra"]) == {
        ("core", "sub"), ("core", "util"), ("sub", "util"), ("sub", "core"),
        ("extra", "core"), ("extra", "sub"), ("extra", "util"),
    }  # fmt: skip


def test_x1_given_files_resolve_against_the_whole_tree(built: Path) -> None:
    graph = static_imports(built, built_assign, files=["pkg/sub/deep.py"])
    assert graph.edges == (Edge("sub", "core", 1, False), Edge("sub", "util", 1, True))
    assert (graph.blind_spots, graph.type_only) == ({"sub": 0}, {"sub": 1})
    assert graph.parse_failures == ()
    with pytest.raises(ValueError, match="twice"):
        static_imports(built, built_assign, files=["pkg/util.py", "pkg/util.py"])


TYPED = {
    "app/__init__.py": "",
    "app/base.py": "VALUE = 1\n",
    "app/model.py": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from app.base import VALUE\n    from app import view\n"
        "    import os\n    import shared\n"
    ),
    "app/view.py": (
        "import typing\n"
        "if typing.TYPE_CHECKING:\n    from app import base\n"
        "from app import base as runtime\n"
    ),
    "one/shared.py": "",
    "two/shared.py": "",
}


def typed_assign(path: str) -> str:
    return path.rsplit("/", 1)[-1][:-3] if path.startswith("app/") else "elsewhere"


def test_x1_type_checking_imports_are_edges_not_blind_spots(tmp_path: Path) -> None:
    """Decision (design 3.1): X1 resolves a TYPE_CHECKING import statically, so it is a
    known edge the owner can be asked about. Only what X1 cannot resolve is a blind
    spot: here ``import shared``, which names two files."""
    repo = tmp_path / "typed"
    repo.mkdir()
    git(repo, "init", "-q")
    write(repo, TYPED)
    graph = static_imports(repo, typed_assign)
    assert graph.edges == (
        Edge("model", "base", 1, False),
        Edge("model", "view", 1, False),
        Edge("view", "base", 2, True),
    )
    assert graph.blind_spots == {
        "__init__": 0, "base": 0, "elsewhere": 0, "model": 1, "view": 0,
    }  # fmt: skip
    # One per statement that resolves into another component: model has two such
    # statements (os and the ambiguous shared do not resolve), view has one.
    assert graph.type_only == {
        "__init__": 0, "base": 0, "elsewhere": 0, "model": 2, "view": 1,
    }  # fmt: skip


def test_reach_excludes_self_pairs_on_a_cycle() -> None:
    cycle = StaticGraph(
        (Edge("a", "b", 1, True), Edge("b", "a", 1, True), Edge("b", "x", 1, True)),
        {},
        {},
        (),
        "x1-v2",
    )
    assert reach(cycle, ["a", "b"]) == {("a", "b"), ("b", "a")}


def praxis_assign(path: str) -> str:
    parts = path.split("/")
    if parts[:2] == ["src", "praxis"] and len(parts) == 3:
        return "src/praxis/." if parts[2] == "__init__.py" else parts[2][:-3]
    return "unmapped"


def test_x1_praxis_at_the_measured_commit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    clone = scratch_clone(PRAXIS_ROOT, home)
    assert clone.parent == home / "scratch"
    assert git(clone, "rev-parse", "HEAD") == git(PRAXIS_ROOT, "rev-parse", "HEAD")
    assert git(clone, "remote") == ""
    try:
        git(clone, "cat-file", "-e", f"{MEASURED_AT}^{{commit}}")
    except subprocess.CalledProcessError:
        shutil.rmtree(clone)
        pytest.skip(f"{MEASURED_AT[:7]} is not in this clone (a shallow checkout)")
    git(clone, "checkout", "-q", "--detach", MEASURED_AT)
    graph = static_imports(clone, praxis_assign)
    shutil.rmtree(clone)

    product = [e for e in graph.edges if {e.src, e.dst} <= set(PRAXIS_COMPONENTS)]
    by_pair = {(e.src, e.dst): e for e in product}
    assert len(product) == 16
    assert by_pair["spans", "store"].module_level is False
    assert by_pair["drill", "gitdiff"].sites == 2
    assert sum(e.dst == "store" for e in product) == 5
    assert sum(e.src == "cli" for e in product) == 5
    assert len(reach(graph, PRAXIS_COMPONENTS)) == 16 + 7
    assert graph.parse_failures == ()
    # spans imports store only under TYPE_CHECKING (the design calls it function-level):
    # an edge X1 resolves, so not a blind spot. The TYPE_CHECKING pathlib imports in
    # doctor, hooks and spans name no component.
    blind = {c: graph.blind_spots[c] for c in PRAXIS_COMPONENTS}
    assert blind == dict.fromkeys(PRAXIS_COMPONENTS, 0)
    typed = {c: graph.type_only[c] for c in PRAXIS_COMPONENTS}
    assert typed == {**dict.fromkeys(PRAXIS_COMPONENTS, 0), "spans": 1}


PROGRAM = {
    "app/lib.py": (
        "def _double(x):\n    return x * 2\n\n"
        "def work(x, report):\n    report(_double(x))\n"
    ),
    "app/main.py": (
        "import sys\n"
        "from app import lib\n\n"
        "seen = []\n\n"
        "def report(v):\n    seen.append(v)\n\n"
        "for i in range(3):\n    lib.work(i, report)\n"
        "sys.exit(0 if seen == [0, 2, 4] else 3)\n"
    ),
}


def program_assign(path: str) -> str:
    return {"app/main.py": "main", "app/lib.py": "lib"}.get(path, "unmapped")


@pytest.fixture
def program(tmp_path: Path) -> Path:
    root = tmp_path / "program"
    write(root, PROGRAM)
    return root


def scenario(*argv: str) -> Scenario:
    return Scenario("s1", tuple(argv))


def test_x2_python_module(program: Path) -> None:
    trace = run_scenario(
        program, scenario(sys.executable, "-m", "app.main"), program_assign
    )
    # main imports lib (1) and calls work three times (3); work calls report back (3).
    assert trace.outcome == "ok"
    assert trace.exit_code == 0
    assert trace.components == frozenset({"main", "lib"})
    assert trace.call_edges == {("main", "lib"): 4, ("lib", "main"): 3}
    assert trace.scenario_id == "s1"


def test_x2_python_under_a_shell_is_traced(program: Path) -> None:
    command = f"{shlex.quote(sys.executable)} -m app.main < /dev/null"
    trace = run_scenario(program, scenario("sh", "-c", command), program_assign)
    assert trace.outcome == "ok"
    assert trace.call_edges == {("main", "lib"): 4, ("lib", "main"): 3}


def test_x2_failure_keeps_the_exit_code_and_what_ran(program: Path) -> None:
    argv = (sys.executable, "-c", "import sys, app.lib; sys.exit(3)")
    trace = run_scenario(program, scenario(*argv), program_assign)
    assert (trace.outcome, trace.exit_code) == ("failed", 3)
    assert trace.components == frozenset({"lib"})
    assert trace.call_edges == {}


def test_x2_outcomes_where_nothing_is_observed(program: Path) -> None:
    sleep = scenario(sys.executable, "-c", "import time; time.sleep(60)")
    timed_out = run_scenario(program, sleep, program_assign, timeout_s=1)
    assert (timed_out.outcome, timed_out.exit_code) == ("timeout", None)
    assert timed_out.components == frozenset()
    assert timed_out.duration_ms < 10_000

    shell = run_scenario(program, scenario("sh", "-c", "exit 0"), program_assign)
    assert (shell.outcome, shell.exit_code) == ("not_python", 0)

    missing = run_scenario(program, scenario("/nonexistent/x2"), program_assign)
    assert (missing.outcome, missing.exit_code) == ("failed", None)


def test_scenario_hash_and_refusal() -> None:
    expected = hashlib.sha256(b"praxis hook").hexdigest()[:16]
    assert Scenario("h", ("praxis", "hook")).command_hash == expected
    with pytest.raises(ValueError, match="command"):
        Scenario("empty", ())


DDL = """
CREATE TABLE nodes (id INTEGER PRIMARY KEY);
CREATE TABLE edges (src INTEGER REFERENCES nodes, dst INTEGER REFERENCES nodes);
CREATE TABLE pairs (a INTEGER, b INTEGER, PRIMARY KEY (a, b));
CREATE TABLE notes (a INTEGER, b INTEGER, FOREIGN KEY (a, b) REFERENCES pairs (a, b));
CREATE VIEW v AS SELECT * FROM nodes;
"""


def test_x3_counts_each_foreign_key_once() -> None:
    assert sqlite_schema(DDL) == {
        "edges": ["nodes", "nodes"],
        "nodes": [],
        "notes": ["pairs"],
        "pairs": [],
    }


def test_x3_refuses_attach(tmp_path: Path) -> None:
    target = tmp_path / "attached.db"
    with pytest.raises(sqlite3.OperationalError):
        sqlite_schema(f"ATTACH DATABASE '{target}' AS other;")
    assert not target.exists()


def test_x3_on_the_praxis_store_matches_its_text() -> None:
    # An independent reading of the same DDL: table names and REFERENCES targets by
    # regex, block by block.
    expected: dict[str, list[str]] = {}
    for block in re.split(r"\bCREATE\s+", store.SCHEMA):
        table = re.match(r"TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)", block)
        if table:
            expected[table[1]] = sorted(re.findall(r"REFERENCES\s+(\w+)", block))
    assert expected
    assert sqlite_schema(store.SCHEMA) == expected
