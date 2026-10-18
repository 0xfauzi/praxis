"""The instrument, driven as the owner drives it: the installed `praxis drill` in a real
repository whose function the recorder saw the agent write, scored by that repository's
own pytest run through this environment's interpreter.

The repository is the recorded fixture's calc.py plus a test file that exercises both
functions, so `divide` (agent-authored after the recorded Edit) is covered and the
untouched tree passes. Expected outcomes are written before each run.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_recorder import AFTER_AGENT, EDIT_BODY, Session

pytestmark = pytest.mark.integration

TESTS = (
    "from pkg.calc import add, divide\n"
    "import pytest\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n"
    "def test_divide():\n    assert divide(6, 3) == 2\n\n"
    "def test_divide_by_zero():\n"
    "    with pytest.raises(ValueError):\n        divide(1, 0)\n"
)
TESTS_WITHOUT_DIVIDE = (
    "from pkg.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
)
CORRECT_BODY = (
    "    if right == 0:\n"
    "        raise ValueError('divide by zero')\n"
    "    return left / right\n"
)
WRONG_BODY = "    return left * right\n"


def pytest_cmd() -> str:
    return f"{sys.executable} -m pytest -q -p no:cacheprovider tests"


class Drill(Session):
    """A recorded session's repository, with tests, ready for a drill."""

    def __init__(self, tmp_path: Path, tests: str = TESTS) -> None:
        super().__init__(tmp_path)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_calc.py").write_text(tests)
        (self.repo / "pkg" / "__init__.py").write_text("")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "tests")
        # The agent writes divide's guard, as recorded; the owner commits nothing after.
        self.start()
        self.agent_edit()
        self.stop()

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PRAXIS_HOME": str(self.praxis_home),
            "PATH": os.environ.get("PATH", ""),
        }

    def drill(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        found = shutil.which("praxis")
        assert found is not None
        return subprocess.run(
            [found, "drill", *args],
            cwd=self.repo,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=self.env(),
        )

    def drill_interactive(self, *args: str) -> subprocess.Popen[str]:
        found = shutil.which("praxis")
        assert found is not None
        return subprocess.Popen(
            [found, "drill", *args],
            cwd=self.repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(),
        )

    def wait_for_strip(self, proc: subprocess.Popen[str]) -> str:
        """Block until the drill announces the strip on stdout; return the file.

        The announcement, not the file: validation also writes a stripped copy for one
        test run and restores it, and a watcher of the file would act on that one.
        """
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            assert line, "the drill ended before announcing a strip"
            if line.startswith("write-it: "):
                break
        text = self.calc.read_text()
        assert "# praxis write-it: write this body" in text
        return text

    def rows(self, table: str) -> list[dict[str, Any]]:
        db = sqlite3.connect(f"file:{self.praxis_home / 'ledger.db'}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        query = {
            "items": "SELECT * FROM items ORDER BY rowid",
            "results": "SELECT * FROM results ORDER BY rowid",
        }[table]
        try:
            return [dict(r) for r in db.execute(query)]
        finally:
            db.close()


def test_list_names_the_agent_written_function_with_its_fraction(
    tmp_path: Path,
) -> None:
    d = Drill(tmp_path)
    done = d.drill("--list", "--test-cmd", pytest_cmd())
    assert done.returncode == 0, done.stderr
    # divide spans lines 11-15 after the edit; the agent wrote 12-14.
    assert "pkg/calc.py:11 divide 3/5 agent lines (0.60) eligible" in done.stdout
    assert "add" not in done.stdout
    assert d.calc.read_text() == AFTER_AGENT, "--list must change nothing"


def test_write_it_pass_restores_the_file_and_records_an_observed_result(
    tmp_path: Path,
) -> None:
    d = Drill(tmp_path)
    proc = d.drill_interactive("--test-cmd", pytest_cmd())
    stripped = d.wait_for_strip(proc)
    assert '"""Return left divided by right."""' in stripped, "docstring must stay"
    assert "if right == 0" not in stripped
    # The owner writes a correct body.
    d.calc.write_text(
        stripped.replace(
            "    raise NotImplementedError  # praxis write-it: write this body\n",
            CORRECT_BODY,
        )
    )
    out, err = proc.communicate("\n", timeout=300)
    assert proc.returncode == 0, err
    assert "pass in" in out
    assert d.calc.read_bytes() == AFTER_AGENT.encode(), "file must be restored exactly"
    assert not (d.praxis_home / "drills").exists() or not list(
        (d.praxis_home / "drills").iterdir()
    )
    items, results = d.rows("items"), d.rows("results")
    assert [i["validation"] for i in items] == ["valid"]
    assert items[0]["qualname"] == "divide"
    assert items[0]["file"] == "pkg/calc.py"
    assert items[0]["agent_lines"] == 3
    assert items[0]["total_lines"] == 5
    # The recorded session id is not ours; the test's own session is the source.
    assert EDIT_BODY["session_id"] not in items[0]["sources"]
    assert json.loads(items[0]["sources"]) == [d.session_id]
    assert [r["outcome"] for r in results] == ["pass"]
    assert results[0]["claim_class"] == "observed"
    assert results[0]["elapsed_s"] >= 0


def test_write_it_fail_is_recorded_and_the_file_still_restored(tmp_path: Path) -> None:
    d = Drill(tmp_path)
    proc = d.drill_interactive("--test-cmd", pytest_cmd())
    stripped = d.wait_for_strip(proc)
    d.calc.write_text(
        stripped.replace(
            "    raise NotImplementedError  # praxis write-it: write this body\n",
            WRONG_BODY,
        )
    )
    out, _ = proc.communicate("\n", timeout=300)
    assert "fail in" in out
    assert d.calc.read_text() == AFTER_AGENT
    assert [r["outcome"] for r in d.rows("results")] == ["fail"]


def test_abort_restores_and_records(tmp_path: Path) -> None:
    d = Drill(tmp_path)
    proc = d.drill_interactive("--test-cmd", pytest_cmd())
    d.wait_for_strip(proc)
    out, _ = proc.communicate("q\n", timeout=300)
    assert "aborted" in out
    assert d.calc.read_text() == AFTER_AGENT
    results = d.rows("results")
    assert [r["outcome"] for r in results] == ["aborted"]
    assert json.loads(results[0]["warnings"]) == ["owner aborted"]


def test_uncovered_function_is_a_void_item_not_a_drill(tmp_path: Path) -> None:
    d = Drill(tmp_path, tests=TESTS_WITHOUT_DIVIDE)
    done = d.drill("--test-cmd", pytest_cmd())
    assert done.returncode == 1, done.stdout
    assert "no valid item" in done.stdout
    assert [i["validation"] for i in d.rows("items")] == [
        "void: tests pass without the body"
    ]
    assert d.rows("results") == []
    assert d.calc.read_text() == AFTER_AGENT


def test_failing_tests_on_the_untouched_tree_make_the_item_void(tmp_path: Path) -> None:
    d = Drill(tmp_path)
    (d.repo / "tests" / "test_calc.py").write_text(
        TESTS + "\ndef test_broken():\n    assert False\n"
    )
    done = d.drill("--test-cmd", pytest_cmd())
    assert done.returncode == 1
    assert [i["validation"] for i in d.rows("items")] == [
        "void: tests fail on the untouched file"
    ]


def test_an_attached_agent_is_refused_and_recorded(tmp_path: Path) -> None:
    d = Drill(tmp_path)
    # A process whose argv names `claude`, working inside the repository. A Python
    # script rather than a shell `exec`, so the name survives into the process's argv.
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "claude"
    script.write_text("import time\ntime.sleep(60)\n")
    agent = subprocess.Popen([sys.executable, str(script)], cwd=d.repo)
    try:
        done = d.drill("--test-cmd", pytest_cmd())
    finally:
        agent.kill()
        agent.wait()
    assert done.returncode == 3, done.stdout
    assert "refused: an agent is attached" in done.stdout
    results = d.rows("results")
    assert [r["outcome"] for r in results] == ["refused"]
    assert any("claude" in w for w in json.loads(results[0]["warnings"]))
    assert d.calc.read_text() == AFTER_AGENT


def test_the_test_command_is_remembered_per_repository(tmp_path: Path) -> None:
    d = Drill(tmp_path)
    first = d.drill("--list")
    assert first.returncode == 2
    assert "pass --test-cmd" in first.stdout
    assert d.drill("--list", "--test-cmd", pytest_cmd()).returncode == 0
    assert d.drill("--list").returncode == 0


def test_a_crashed_drill_leaves_a_named_backup_that_restore_puts_back(
    tmp_path: Path,
) -> None:
    d = Drill(tmp_path)
    proc = d.drill_interactive("--test-cmd", pytest_cmd())
    d.wait_for_strip(proc)
    proc.kill()
    proc.wait()
    assert "raise NotImplementedError" in d.calc.read_text(), "the crash left the strip"
    backups = list((d.praxis_home / "drills").glob("*/original"))
    assert len(backups) == 1
    blocked = d.drill("--test-cmd", pytest_cmd())
    assert blocked.returncode == 2
    assert "--restore" in blocked.stdout
    restored = d.drill("--restore")
    assert restored.returncode == 0
    assert "restored" in restored.stdout
    assert d.calc.read_bytes() == AFTER_AGENT.encode()
    assert list((d.praxis_home / "drills").glob("*/original")) == []


def test_no_code_the_owner_typed_reaches_the_ledger(tmp_path: Path) -> None:
    d = Drill(tmp_path)
    proc = d.drill_interactive("--test-cmd", pytest_cmd())
    stripped = d.wait_for_strip(proc)
    marker = "    return left / right  # OWNER_TYPED_xyzzy\n"
    d.calc.write_text(
        stripped.replace(
            "    raise NotImplementedError  # praxis write-it: write this body\n",
            "    if right == 0:\n        raise ValueError('divide by zero')\n" + marker,
        )
    )
    proc.communicate("\n", timeout=300)
    data = b"".join(
        p.read_bytes()
        for p in (
            d.praxis_home / "ledger.db",
            Path(f"{d.praxis_home / 'ledger.db'}-wal"),
        )
        if p.exists()
    )
    assert b"OWNER_TYPED_xyzzy" not in data
    assert b"return left / right" not in data
    # Identifiers that name the item are allowed and present.
    assert b"divide" in data
    assert b"pkg/calc.py" in data
