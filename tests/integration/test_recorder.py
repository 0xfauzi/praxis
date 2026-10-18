"""The recorder, driven as the owner's agent drives it: the installed `praxis` binary,
one process per hook event, a payload on stdin, a real git repository, a named HOME.

Payloads come from fixtures/claude/2.1.257/ (recorded Claude Code 2.1.257 bodies) where
one was recorded. SessionStart and UserPromptSubmit were not recorded in that session
(Claude Code did not fire them in -p mode), so those two are built from the documented
common fields and say so in their helper. Expected counts are written before each
scenario runs, and the agent's own count comes from the recorded `structuredPatch`, an
independent source from the git diff the recorder reads.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "claude" / "2.1.257"
EDIT_BODY = json.loads((FIXTURES / "post_tool_use_edit.json").read_text())
ORIGINAL: str = EDIT_BODY["tool_response"]["originalFile"]
OLD: str = EDIT_BODY["tool_response"]["oldString"]
NEW: str = EDIT_BODY["tool_response"]["newString"]
AFTER_AGENT = ORIGINAL.replace(OLD, NEW, 1)
# What the agent itself reported changing: every +/- line in its structuredPatch.
AGENT_LINES = sum(
    1
    for hunk in EDIT_BODY["tool_response"]["structuredPatch"]
    for line in hunk["lines"]
    if line[:1] in "+-"
)


# -- the running system -------------------------------------------------------------


class Session:
    """One session against one repository: the paths, the ids, and the hook runner."""

    def __init__(
        self, tmp_path: Path, *, with_repo: bool = True, commit: bool = True
    ) -> None:
        self.home = tmp_path / "home"
        self.praxis_home = tmp_path / "praxis-home"
        self.repo = tmp_path / "repo"
        self.home.mkdir(parents=True)
        self.repo.mkdir(parents=True)
        self.session_id = str(uuid.uuid4())
        if with_repo:
            (self.repo / "pkg").mkdir()
            self.calc.write_text(ORIGINAL)
            self.git("init", "-q")
            self.git("config", "user.email", "t@example.invalid")
            self.git("config", "user.name", "t")
            if commit:
                self.git("add", ".")
                self.git("commit", "-q", "-m", "base")

    @property
    def calc(self) -> Path:
        return self.repo / "pkg" / "calc.py"

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        )

    def hook(self, payload: Any) -> subprocess.CompletedProcess[bytes]:
        found = shutil.which("praxis")
        assert found is not None, "no `praxis` on PATH: run `uv sync` first"
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        done = subprocess.run(
            [found, "hook"],
            input=raw,
            capture_output=True,
            timeout=60,
            check=False,
            env={
                "HOME": str(self.home),
                "PRAXIS_HOME": str(self.praxis_home),
                "PATH": os.environ.get("PATH", ""),
            },
        )
        # Invariant 7, checked on every call: exit 0 and nothing on stdout, whatever
        # came in.
        assert done.returncode == 0, done.stderr
        assert done.stdout == b"", done.stdout
        return done

    # payloads

    def recorded(self, name: str, **override: Any) -> dict[str, Any]:
        text = (FIXTURES / f"{name}.json").read_text()
        text = text.replace("<repo>", str(self.repo)).replace("<home>", str(self.home))
        body: dict[str, Any] = json.loads(text)
        body["session_id"] = self.session_id
        body["cwd"] = str(self.repo)
        body.update(override)
        return body

    def documented(self, event: str, **fields: Any) -> dict[str, Any]:
        """Built from the documented common fields: not a recorded body."""
        body: dict[str, Any] = {
            "session_id": self.session_id,
            "transcript_path": f"{self.home}/.claude/projects/x/{self.session_id}.jl",
            "cwd": str(self.repo),
            "permission_mode": "default",
            "hook_event_name": event,
        }
        body.update(fields)
        return body

    def start(self) -> None:
        self.hook(self.documented("SessionStart", source="startup"))

    def prompt(self) -> None:
        self.hook(self.documented("UserPromptSubmit", prompt="fix divide"))

    def stop(self) -> None:
        self.hook(self.recorded("stop"))

    def agent_edit(self) -> None:
        """The recorded Edit, replayed: PreToolUse, the file changes, PostToolUse."""
        self.hook(self.recorded("pre_tool_use_edit"))
        self.calc.write_text(AFTER_AGENT)
        self.hook(self.recorded("post_tool_use_edit"))

    def agent_bash(self, mutate: Any, tool_use_id: str = "toolu_bash_test") -> None:
        """A Bash call that writes files: the recorder sees only pre and post."""
        self.hook(self.recorded("pre_tool_use_bash", tool_use_id=tool_use_id))
        mutate()
        self.hook(self.recorded("post_tool_use_bash", tool_use_id=tool_use_id))

    # reads

    def ledger(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.praxis_home / 'ledger.db'}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def latest(self, trigger: str | None = None) -> dict[str, sqlite3.Row]:
        """Attribution rows of the latest boundary, by file."""
        db = self.ledger()
        try:
            rows = db.execute(
                "SELECT * FROM attributions WHERE session_id = ? ORDER BY id",
                (self.session_id,),
            ).fetchall()
        finally:
            db.close()
        assert rows, "no attribution rows"
        ts = rows[-1]["ts"]
        last = [r for r in rows if r["ts"] == ts]
        if trigger is not None:
            assert all(r["trigger"] == trigger for r in last)
        return {r["file"]: r for r in last}

    def events(self) -> list[sqlite3.Row]:
        db = self.ledger()
        try:
            return db.execute(
                "SELECT * FROM hook_events WHERE session_id = ? OR session_id IS NULL"
                " ORDER BY id",
                (self.session_id,),
            ).fetchall()
        finally:
            db.close()

    def home_is_untouched(self) -> None:
        assert sorted(self.home.rglob("*")) == [], "the hook wrote under HOME"


def counts(row: sqlite3.Row) -> dict[str, int | None]:
    return {
        k: row[k] for k in ("agent", "human", "preexisting", "ambiguous", "reverted")
    }


# -- scenarios ----------------------------------------------------------------------


def test_agent_edit_only(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()
    s.prompt()
    s.agent_edit()
    s.stop()
    want = {
        "agent": AGENT_LINES,
        "human": 0,
        "preexisting": 0,
        "ambiguous": 0,
        "reverted": 0,
    }
    row = s.latest("Stop")["pkg/calc.py"]
    assert counts(row) == want
    assert row["coverage"] == "full"
    assert row["claim_class"] == "derived"
    assert EDIT_BODY["tool_use_id"] in json.loads(row["sources"])
    assert json.loads(row["assumptions"])
    s.home_is_untouched()


def test_owner_edits_between_turns_are_human(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()
    s.agent_edit()
    assert "    return left + right" in AFTER_AGENT
    text = AFTER_AGENT.replace("    return left + right", "    return right + left", 1)
    text += "\ndef owner_added() -> int:\n    return 42\n"
    s.calc.write_text(text)
    s.prompt()
    # one line replaced (a - and a +) and three lines appended (blank, def, return)
    want = {
        "agent": AGENT_LINES,
        "human": 5,
        "preexisting": 0,
        "ambiguous": 0,
        "reverted": 0,
    }
    assert counts(s.latest("UserPromptSubmit")["pkg/calc.py"]) == want


def test_owner_altering_an_agent_line_is_ambiguous_not_human(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()
    s.agent_edit()
    assert 'raise ValueError("divide by zero")' in AFTER_AGENT
    s.calc.write_text(
        AFTER_AGENT.replace(
            'raise ValueError("divide by zero")',
            'raise ZeroDivisionError("divide by zero")',
            1,
        )
    )
    s.stop()
    # the agent's + line is gone (ambiguous 1); the owner's replacement is a new + line
    want = {
        "agent": AGENT_LINES - 1,
        "human": 1,
        "preexisting": 0,
        "ambiguous": 1,
        "reverted": 0,
    }
    assert counts(s.latest("Stop")["pkg/calc.py"]) == want


def test_bash_writes_are_the_agents_through_the_same_mechanism(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()

    def append() -> None:
        with s.calc.open("a") as f:
            f.write("\nSHELL_WROTE = True\n")

    s.agent_bash(append)
    s.stop()
    want = {"agent": 2, "human": 0, "preexisting": 0, "ambiguous": 0, "reverted": 0}
    assert counts(s.latest("Stop")["pkg/calc.py"]) == want
    db = s.ledger()
    tools = {r[0] for r in db.execute("SELECT DISTINCT tool_name FROM agent_deltas")}
    db.close()
    assert tools == {"Bash"}


def test_new_untracked_file_written_by_the_agent(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()

    def write() -> None:
        (s.repo / "pkg" / "new.py").write_text("A = 1\nB = 2\n")

    s.hook(
        s.recorded("pre_tool_use_edit", tool_name="Write", tool_use_id="toolu_write")
    )
    write()
    s.hook(
        s.recorded("post_tool_use_edit", tool_name="Write", tool_use_id="toolu_write")
    )
    s.stop()
    rows = s.latest("Stop")
    assert counts(rows["pkg/new.py"]) == {
        "agent": 2,
        "human": 0,
        "preexisting": 0,
        "ambiguous": 0,
        "reverted": 0,
    }
    assert "pkg/calc.py" not in rows


def test_dirty_tree_before_the_session_is_preexisting_and_its_undo_is_reverted(
    tmp_path: Path,
) -> None:
    s = Session(tmp_path)
    # The owner has already changed add() when the session starts: one - and one + line.
    assert "    return left + right" in ORIGINAL
    dirty = ORIGINAL.replace("    return left + right", "    return right + left", 1)
    s.calc.write_text(dirty)
    s.start()
    # The recorded Edit applied on top of the dirty tree, inside its pre/post pair.
    s.hook(s.recorded("pre_tool_use_edit"))
    s.calc.write_text(dirty.replace(OLD, NEW, 1))
    s.hook(s.recorded("post_tool_use_edit"))
    s.prompt()
    want = {
        "agent": AGENT_LINES,
        "human": 0,
        "preexisting": 2,
        "ambiguous": 0,
        "reverted": 0,
    }
    assert counts(s.latest("UserPromptSubmit")["pkg/calc.py"]) == want
    # The owner undoes their pre-session change: those two lines are now reverted.
    s.calc.write_text(AFTER_AGENT)
    s.stop()
    want = {
        "agent": AGENT_LINES,
        "human": 0,
        "preexisting": 0,
        "ambiguous": 0,
        "reverted": 2,
    }
    assert counts(s.latest("Stop")["pkg/calc.py"]) == want


def test_duplicate_lines_are_counted_not_collapsed(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()

    def agent_two() -> None:
        with s.calc.open("a") as f:
            f.write("\nMARK = 0\nMARK = 0\n")

    s.agent_bash(agent_two)
    with s.calc.open("a") as f:
        f.write("MARK = 0\n")  # the owner adds a third identical line
    s.stop()
    # the blank line and two MARK lines are the agent's; the third MARK is the owner's
    want = {"agent": 3, "human": 1, "preexisting": 0, "ambiguous": 0, "reverted": 0}
    assert counts(s.latest("Stop")["pkg/calc.py"]) == want


def test_without_a_baseline_human_and_preexisting_stay_unknown(tmp_path: Path) -> None:
    s = Session(tmp_path)
    # No SessionStart: the hook was installed mid-session.
    s.agent_edit()
    s.calc.write_text(AFTER_AGENT + "\nX = 1\n")
    s.stop()
    row = s.latest("Stop")["pkg/calc.py"]
    assert row["coverage"] == "no_baseline"
    assert row["agent"] == AGENT_LINES
    assert row["human"] is None
    assert row["preexisting"] is None
    assert row["reverted"] is None


def test_post_without_pre_is_unobservable_not_guessed(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()
    s.calc.write_text(AFTER_AGENT)
    s.hook(s.recorded("post_tool_use_edit"))
    s.stop()
    notes = [r["note"] for r in s.events() if r["outcome"] == "unobservable"]
    assert notes == ["no pre snapshot for this tool_use_id"]
    # With no agent delta and a baseline, the change can only be reported as human.
    assert counts(s.latest("Stop")["pkg/calc.py"])["agent"] == 0


@pytest.mark.parametrize(
    ("raw", "note"),
    [
        (b"not json", "payload is not a JSON object"),
        (b"", "payload is not a JSON object"),
        (b"[1, 2]", "payload is not a JSON object"),
        (b'{"hook_event_name": "Stop"}', "payload lacks hook_event_name or session_id"),
        (
            b'{"hook_event_name": "Stop", "session_id": "<session>"}',
            "payload lacks cwd",
        ),
    ],
)
def test_bad_input_exits_zero_and_is_recorded(
    tmp_path: Path, raw: bytes, note: str
) -> None:
    s = Session(tmp_path, with_repo=False)
    s.hook(raw.replace(b"<session>", s.session_id.encode()))
    events = s.events()
    assert len(events) == 1
    assert events[0]["outcome"] == "unobservable"
    assert events[0]["note"] == note
    s.home_is_untouched()


def test_oversize_payload_is_unobservable(tmp_path: Path) -> None:
    s = Session(tmp_path, with_repo=False)
    s.hook(b"{" + b" " * 4_000_001 + b"}")
    assert [r["note"] for r in s.events()] == ["payload over size limit"]


def test_not_a_repository_and_no_commits_are_unobservable(tmp_path: Path) -> None:
    plain = Session(tmp_path / "plain", with_repo=False)
    plain.start()
    assert [r["note"] for r in plain.events()] == ["not a git repository"]
    fresh = Session(tmp_path / "fresh", commit=False)
    fresh.start()
    assert [r["note"] for r in fresh.events()] == ["no HEAD commit"]


def test_binary_and_oversize_untracked_files_are_skipped_with_partial_coverage(
    tmp_path: Path,
) -> None:
    s = Session(tmp_path)
    s.start()

    def write_both() -> None:
        (s.repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        (s.repo / "big.txt").write_bytes(b"x\n" * 600_000)

    s.agent_bash(write_both)
    s.stop()
    rows = s.latest("Stop")
    assert rows["blob.bin"]["coverage"] == "partial"
    assert json.loads(rows["blob.bin"]["warnings"]) == ["blob.bin: binary"]
    assert rows["big.txt"]["coverage"] == "partial"
    assert json.loads(rows["big.txt"]["warnings"]) == [
        "big.txt: 1200000 bytes, over cap"
    ]
    assert counts(rows["blob.bin"])["agent"] == 0


def test_no_text_from_the_repository_reaches_the_ledger(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()
    s.agent_edit()
    s.calc.write_text(AFTER_AGENT + "\nOWNER_SECRET_LINE = 'xyzzy-plugh'\n")
    s.stop()
    # Every byte SQLite left on disk, main file and WAL alike.
    data = b"".join(
        p.read_bytes()
        for p in (
            s.praxis_home / "ledger.db",
            Path(f"{s.praxis_home / 'ledger.db'}-wal"),
        )
        if p.exists()
    )
    for needle in (
        b"divide by zero",
        b"xyzzy-plugh",
        b"OWNER_SECRET_LINE",
        b"def divide",
    ):
        assert needle not in data, needle


def test_every_hook_records_its_own_duration(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.start()
    s.prompt()
    s.agent_edit()
    s.stop()
    durations = [r["duration_ms"] for r in s.events()]
    assert len(durations) == 5
    # Not asserted against a budget: the ledger carries the live distribution, and
    # docs/experiments/E01.md records what this machine measured.
    assert all(d > 0 for d in durations)


def test_setup_prints_a_valid_snippet_and_refuses_to_apply(tmp_path: Path) -> None:
    found = shutil.which("praxis")
    assert found is not None
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    out = subprocess.run(
        [found, "setup", "claude", "--print"], capture_output=True, check=True, env=env
    )
    block = json.loads(out.stdout)
    assert set(block["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
    }
    assert (
        block["hooks"]["PreToolUse"][0]["matcher"]
        == "Edit|Write|MultiEdit|NotebookEdit|Bash"
    )
    assert block["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("praxis hook")
    refused = subprocess.run(
        [found, "setup", "claude", "--apply"], capture_output=True, check=False, env=env
    )
    assert refused.returncode == 2
    assert b"invariant 8" in refused.stderr
    assert sorted(Path(tmp_path).rglob("*")) == [], "setup wrote under HOME"
