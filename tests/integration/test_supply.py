"""`praxis show supply` and `supply.measure` on a SYNTHETIC transcript directory. The
entries carry the field names of a real Claude Code 2.1.303 transcript (`origin.kind`,
`promptId`, `cwd`, `version`, `isMeta`, `toolUseResult`); the sessions, repositories and
prompts are made up here. One session has no `origin` anywhere, as 2.1.257 wrote. The
expected counts are written out before each run, line by line from the fixture.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from praxis import supply
from praxis.unassisted import locate

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

CANARY = "CANARYSUPPLY5e21"
REPO_CANARY = "REPONAMECANARY9c"


def _iso(when: dt.datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Session:
    """Writes one transcript, line by line, in the 2.1.303 shape."""

    def __init__(self, cwd: Path, version: str = "2.1.303") -> None:
        self.cwd, self.version = str(cwd), version
        self.lines: list[str] = [json.dumps({"type": "queue-operation"})]
        self.n = 0

    def _base(self, kind: str, ts: str, **extra: object) -> None:
        self.n += 1
        entry = {"type": kind, "isSidechain": False, "timestamp": ts, "cwd": self.cwd,
                 "version": self.version, "sessionId": "s", **extra}  # fmt: skip
        self.lines.append(json.dumps(entry))

    def prompt(
        self, ts: str, text: str, kind: str | None = "human", **extra: object
    ) -> None:
        origin = {} if kind is None else {"origin": {"kind": kind}}
        message = {"role": "user", "content": text}
        self._base(
            "user", ts, promptId=f"P{self.n}", message=message, **origin, **extra
        )

    def tool(
        self, ts: str, name: str, tool_input: object, result: object = None
    ) -> None:
        tid = f"tu_{self.n}"
        block = {"type": "tool_use", "id": tid, "name": name, "input": tool_input}
        message = {"role": "assistant", "content": [block], "stop_reason": "tool_use"}
        self._base("assistant", ts, message=message)
        done = {"type": "tool_result", "tool_use_id": tid, "is_error": False,
                "content": "ok"}  # fmt: skip
        self._base("user", ts, message={"role": "user", "content": [done]},
                   toolUseResult=result or {})  # fmt: skip

    def text(self, ts: str) -> None:
        message = {"role": "assistant", "content": [{"type": "text", "text": "done"}],
                   "stop_reason": "end_turn"}  # fmt: skip
        self._base("assistant", ts, message=message)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n")


OPTIONS = [{"label": x, "description": "d"} for x in ("a", "b", "c")]
QUESTION = {"question": "Which store?", "header": "h", "multiSelect": False}
ASK = {"questions": [{**QUESTION, "options": OPTIONS}]}


def _fixture(home: Path, now: dt.datetime) -> tuple[Path, Path, dt.datetime]:
    """Sessions A, B and C in the week of `now`, D in the week before. Returns the
    two repositories and the time of the first entry in the week of `now`."""
    repo_a, repo_b = home / f"{REPO_CANARY}-a", home / f"{REPO_CANARY}-b"
    outside = home / f"{REPO_CANARY}-plain"
    for repo in (repo_a, repo_b):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo_a / "sub").mkdir()
    outside.mkdir()
    monday = dt.datetime.combine(
        now.date() - dt.timedelta(days=now.weekday()), dt.time(), tzinfo=dt.UTC
    )
    base = max(now - dt.timedelta(seconds=5), monday)
    clock = iter(_iso(base + dt.timedelta(milliseconds=10 * k)) for k in range(100))
    projects = home / ".claude" / "projects"

    a = _Session(repo_a)
    a.prompt(next(clock), f"Refactor the store {CANARY}")  # human 1: edit
    a.tool(next(clock), "Read", {"file_path": "x"})
    a.prompt(next(clock), "task done", kind="task-notification")  # not a boundary
    a.tool(next(clock), "Edit", {"file_path": f"/{REPO_CANARY}/store.py"})
    a.prompt(next(clock), "and the tests?")  # human 2: empty
    a.text(next(clock))
    a.prompt(next(clock), "<local-command-caveat>", kind=None, isMeta=True)  # absent
    a.prompt(next(clock), "pick one")  # human 3: bash only
    a.tool(next(clock), "AskUserQuestion", ASK, {"answers": {"Which store?": "b"}})
    a.tool(next(clock), "Bash", {"command": "ls"})
    a.prompt(next(clock), "ask a subagent")  # human 4: subagent only
    a.tool(next(clock), "Agent", {"prompt": "go"})
    a.lines.append("{not json " + CANARY)  # unusable: not JSON
    a.lines.append(json.dumps({"type": "user", "message": 5}))  # unusable: shape
    a.cwd = str(repo_a / "sub")
    a.prompt(next(clock), "write it")  # human 5: edit, same repository
    a.tool(next(clock), "Write", {"file_path": f"/{REPO_CANARY}/web/App.tsx"})
    a.text(next(clock))
    a.write(projects / "-a" / "aaaa-1.jsonl")

    b = _Session(repo_b)
    b.prompt(next(clock), "plan it")  # human 6: empty (a plan is not a change)
    b.tool(next(clock), "ExitPlanMode", {"plan": "p"}, {"plan": "p"})
    b.text(next(clock))
    b.prompt(next(clock), "go")  # human 7: edit
    b.tool(next(clock), "MultiEdit", {"file_path": "pkg/main.go"})
    b.write(projects / "-b" / "bbbb-2.jsonl")

    c = _Session(outside, version="2.1.257")  # no origin on any prompt
    c.prompt(next(clock), "old cli", kind=None)
    c.tool(next(clock), "Edit", {"file_path": "Makefile"})  # no suffix
    c.prompt(next(clock), "again", kind=None)
    c.write(projects / "-c" / "cccc-3.jsonl")

    d = _Session(repo_a)
    last_week = now - dt.timedelta(days=7)
    d.prompt(_iso(last_week), "last week")
    d.tool(_iso(last_week + dt.timedelta(seconds=1)), "Edit", {"file_path": "u.PY"})
    d.cwd = str(repo_b)  # the session moves: the prompt's own cwd names the repository
    d.prompt(_iso(last_week + dt.timedelta(seconds=2)), "and here")  # empty
    d.write(projects / "-a" / "dddd-4.jsonl")

    rollout = home / ".codex" / "sessions" / "2026" / "10" / "19" / "rollout-x.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    return repo_a, repo_b, base


def _id(repo: Path) -> str:
    found = locate(repo)
    assert found is not None
    return found.repo_id


def _decisions(ask: int, plan: int) -> dict[str, dict[str, int]]:
    """One call each at most: AskUserQuestion with 3 options, ExitPlanMode with 2."""
    asked = {"options_3": 1} if ask else {}
    planned = {"options_2": 1} if plan else {}
    return {
        "AskUserQuestion": {"calls": ask, "unscorable": 0, **asked},
        "ExitPlanMode": {"calls": plan, "unscorable": 0, **planned},
    }


def _praxis(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    found = shutil.which("praxis")
    assert found is not None, "no `praxis` on PATH: run `uv sync` first"
    return subprocess.run(
        [found, "show", "supply", *args],
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


def _label(when: dt.datetime) -> str:
    year, week, _ = when.astimezone(dt.UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _check_table(lines: list[str]) -> None:
    """The text table of the fixture: the expected cells, and one sentence per row."""
    rows = {line.split("  ")[0]: line.split() for line in lines if "  " in line}
    assert rows["human prompts"][-3:] == ["2", "7", "9"]
    assert rows["mutating"][-3:] == ["1", "4", "5"]
    assert rows["sessions"][-4:] == ["unknown", "1", "3", "4"]
    (suffix_line,) = (line for line in lines if line.startswith("edit suffixes  "))
    for cell in (".py:1", "(none):1 .go:1 .py:1 .tsx:1", ".py:2 (none):1 .go:1 .tsx:1"):
        assert f"  {cell}" in suffix_line, cell  # by count, then the suffix in ASCII
    for name, meaning, _ in supply._ROWS:  # every row has its one sentence
        assert f"{name}: {meaning}" in lines


def test_show_supply_counts_the_fixture_and_prints_no_path_or_text(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    now = dt.datetime.now(dt.UTC)
    repo_a, repo_b, base = _fixture(home, now)
    ids = {"a": _id(repo_a), "b": _id(repo_b)}
    this_week, last_week = _label(base), _label(now - dt.timedelta(days=7))
    # Expected, from the fixture above, before the run.
    current = {
        "coverage": "partial", "sessions": 3, "prompts": 11, "human": 7,
        "origin_unrecorded": 2, "origin_absent": 1,
        "segments": {"edit": 3, "bash_only": 1, "subagent_only": 1, "empty": 2},
        "mutating": 4, "empty_rate": 2 / 7, "repositories": 2,
        "repository_ids": sorted(ids.values()), "cwd_outside_repository": 1,
        "decisions": _decisions(1, 1),
        # Edit, Write and MultiEdit by file suffix; the Read and the Bash call are not
        # edits, and session C counts although its prompts have no origin.
        "edit_suffixes": {".go": 1, ".py": 1, ".tsx": 1, "(none)": 1},
    }  # fmt: skip
    previous = {
        "coverage": "full", "sessions": 1, "prompts": 2, "human": 2,
        "origin_unrecorded": 0, "origin_absent": 0,
        "segments": {"edit": 1, "bash_only": 0, "subagent_only": 0, "empty": 1},
        "mutating": 1, "empty_rate": 0.5, "repositories": 2,
        "repository_ids": sorted(ids.values()), "cwd_outside_repository": 0,
        "decisions": _decisions(0, 0),
        "edit_suffixes": {".py": 1},  # u.PY: the suffix is lowercased
    }  # fmt: skip
    total = {
        **current, "sessions": 4, "prompts": 13, "human": 9, "mutating": 5,
        "segments": {"edit": 4, "bash_only": 1, "subagent_only": 1, "empty": 3},
        "empty_rate": 3 / 9,
        "edit_suffixes": {".go": 1, ".py": 2, ".tsx": 1, "(none)": 1},
    }  # fmt: skip

    done = _praxis(home, "--weeks", "3", "--json")
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    weeks = {w["week"]: w for w in got["weeks"]}
    assert len(weeks) == 3
    for label, expected in ((this_week, current), (last_week, previous)):
        assert {k: weeks[label][k] for k in expected} == expected, label
    (earliest,) = set(weeks) - {this_week, last_week}
    assert weeks[earliest]["coverage"] == "unobservable"
    assert {v for k, v in weeks[earliest].items() if k not in ("week", "coverage")} == {
        None
    }
    assert {k: got["total"][k] for k in total} == total
    assert got["files"] == 4
    found = (got["unreadable"], got["unusable_lines"], got["bad_timestamps"])
    assert found == (0, 2, 0)
    assert got["duplicate_session_ids"] == 0
    assert got["codex"] == {"files": 1, "read": False, "reason": "no_reader"}

    table = _praxis(home, "--weeks", "3")
    assert table.returncode == 0, table.stderr
    _check_table(table.stdout.splitlines())
    for out in (done, table):
        for secret in (CANARY, REPO_CANARY, str(tmp_path), "Refactor", "Which store"):
            assert secret not in out.stdout + out.stderr, secret
    assert not (home / "praxis-home").exists(), "show supply wrote under PRAXIS_HOME"


def test_weeks_must_be_a_whole_number(tmp_path: Path) -> None:
    for bad in ("0", "x", "-1"):
        done = _praxis(tmp_path, "--weeks", bad)
        assert done.returncode == 2, bad
        assert done.stdout.startswith("praxis show supply: write a whole number")


def test_measure_splits_utc_weeks_and_reports_what_it_cannot_read(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    s = _Session(tmp_path / "gone")  # a cwd that no longer exists on disk
    s.prompt("2026-10-18T23:59:59.000Z", "sunday")  # 2026-W42, edit
    s.tool("2026-10-18T23:59:59.500Z", "Edit", {"file_path": "x"})
    s.prompt("2026-10-19T01:30:00+02:00", "sunday in UTC")  # 2026-W42, empty
    s.prompt("2026-10-19T00:00:00.000Z", "monday")  # 2026-W43, empty
    s.prompt("2026-10-19T09:00:00", "no zone")  # bad timestamp, counted, not placed
    s.write(projects / "-x" / "eeee-5.jsonl")
    s.write(projects / "-y" / "eeee-5.jsonl")  # the same id in a second project
    (projects / "-z" / "ffff-6.jsonl").mkdir(parents=True)  # unreadable
    now = dt.datetime(2026, 10, 21, 12, tzinfo=dt.UTC)
    # Expected: each copy is read; the id counts once a week; the duplicate is named.
    first = supply.measure(projects, None, 2, now=now)
    w42, w43 = first.weeks
    assert (w42.week, w43.week) == ("2026-W42", "2026-W43")
    assert (w42.sessions, w42.prompts, w42.human, w42.mutating) == (1, 4, 4, 2)
    assert w42.segments == {"edit": 2, "bash_only": 0, "subagent_only": 0, "empty": 2}
    assert (w43.sessions, w43.prompts, w43.human, w43.mutating) == (1, 2, 2, 0)
    # The no-zone prompt still ends the Monday prompt's segment, so both stay empty.
    assert (w42.cwd_outside_repository, w42.repositories) == (1, ())
    assert (first.files, first.unreadable, first.duplicate_session_ids) == (3, 1, 1)
    assert first.bad_timestamps == 2
    assert first.codex_files is None
    assert first.first_prompt == "2026-10-18T23:30:00+00:00"
    assert supply.measure(projects, None, 2, now=now) == first  # a second run agrees
    with pytest.raises(ValueError, match="time zone"):
        supply.measure(projects, None, 2, now=dt.datetime(2026, 10, 21))  # noqa: DTZ001
