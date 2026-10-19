"""The attached-agent check, driven against real processes: a Python script named
`claude` started with its cwd inside a real git repository, read back both in-process
and from a separate interpreter, as `praxis probe` will. Expected values are written
first.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from praxis.unassisted import attached_agents, locate

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration


def make_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


@contextmanager
def fake_agent(bin_dir: Path, cwd: Path) -> Iterator[int]:
    # A Python script rather than a shell `exec`, so the name survives into the argv.
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("import time\ntime.sleep(60)\n")
    agent = subprocess.Popen([sys.executable, str(script)], cwd=cwd)
    try:
        yield agent.pid
    finally:
        agent.kill()
        agent.wait()


def test_locate_names_the_repository_and_refuses_outside_one(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "pkg").mkdir()
    found = locate(repo / "pkg")
    assert found is not None
    assert found.root == repo
    assert found.repo_id
    assert locate(tmp_path) is None


def test_no_agent_in_the_repository_is_an_empty_list(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    assert attached_agents(repo) == []


def test_an_agent_inside_the_repository_is_named_with_its_pid(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    with fake_agent(tmp_path / "bin", repo / "sub") as pid:
        assert attached_agents(repo) == [f"claude pid {pid} in {repo / 'sub'}"]


def test_an_agent_outside_the_repository_is_not_counted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with fake_agent(tmp_path / "bin", elsewhere):
        assert attached_agents(repo) == []


def test_a_separate_interpreter_sees_the_same_agent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    probe = (
        "import sys\nfrom pathlib import Path\n"
        "from praxis.unassisted import attached_agents\n"
        "print('\\n'.join(attached_agents(Path(sys.argv[1]))))\n"
    )
    with fake_agent(tmp_path / "bin", repo) as pid:
        done = subprocess.run(
            [sys.executable, "-c", probe, str(repo)],
            capture_output=True,
            text=True,
            check=True,
        )
    assert done.stdout.splitlines() == [f"claude pid {pid} in {repo}"]
