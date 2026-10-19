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

import contextlib
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

    def hook(
        self, payload: Any, stdout: bytes = b""
    ) -> subprocess.CompletedProcess[bytes]:
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
        # Invariant 7, checked on every call: exit 0, and nothing on stdout but the
        # one allowlisted notice where a test expects it.
        assert done.returncode == 0, done.stderr
        assert done.stdout == stdout, done.stdout
        return done

    # payloads

    def recorded(
        self, name: str, fixtures: Path = FIXTURES, **override: Any
    ) -> dict[str, Any]:
        text = (fixtures / f"{name}.json").read_text()
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


# -- design 03 section 5: base-commit diffing, pointers, edges, decisions, segments ----
#
# Payloads from fixtures/claude/2.1.303/ were recorded on this machine from Claude Code
# 2.1.303 run with `-p` against a scratch repository (2026-10-19), paths replaced by
# <repo> and <home>. AskUserQuestion and ExitPlanMode are unavailable in -p, and no
# Codex binary is installed here, so those payloads are built from the documented fields
# (code.claude.com/docs/en/hooks; the Codex hooks reference) and say so where built.

V303 = FIXTURES.parent / "2.1.303"
NOTICE = b'{"terminalSequence": "\\u001b]9;praxis: feedback ready\\u0007"}'
PAST = "2000-01-01T00:00:00.000Z"


class Mapped(Session):
    """A five-module package, so the mechanical map has components a to e."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, with_repo=False)
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "__init__.py").write_text("")
        for name in "abcde":
            (self.repo / "pkg" / f"{name}.py").write_text(f"{name.upper()} = 1\n")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.commit("base")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    def v303(self, name: str, **override: Any) -> dict[str, Any]:
        return self.recorded(name, V303, **override)

    def bash(self, mutate: Any, tid: str, **extra: Any) -> None:
        self.hook(self.v303("pre_tool_use_bash_commit", tool_use_id=tid, **extra))
        mutate()
        self.hook(self.v303("post_tool_use_bash_commit", tool_use_id=tid, **extra))

    def append(self, name: str, text: str, tid: str, **extra: Any) -> None:
        def write() -> None:
            with (self.repo / "pkg" / f"{name}.py").open("a") as f:
                f.write(text)

        self.bash(write, tid, **extra)

    def ledger_store(self) -> Any:
        from praxis import store

        return store.Store(self.praxis_home / "ledger.db").open()

    def freeze(self) -> Any:
        from praxis import components, gitdiff

        top = gitdiff.toplevel(self.repo)
        assert top is not None
        cmap = components.derive(self.repo, gitdiff.repo_id(top))
        db = self.ledger_store()
        db.component_map_freeze(
            cmap.repo_id, db.component_map_put(components.to_json(cmap), PAST), PAST
        )
        db.close()
        return cmap

    def register(self, cmap: Any, ts: str = PAST) -> str:
        db = self.ledger_store()
        db.task_register(
            region=[], bound=[], related=[], drawn=[], task_id="task-1",
            repo_id=cmap.repo_id, map_version=cmap.version, ordinal=1, ts=ts,
            arm="control", seed_hash="ab" * 32, stratum="empty",
        )  # fmt: skip
        db.close()
        return "task-1"

    def rows(self, sql: str, *params: object) -> list[sqlite3.Row]:
        db = self.ledger()
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()


def test_an_agent_commit_inside_bash_moves_no_line(tmp_path: Path) -> None:
    s = Mapped(tmp_path)
    base = s.head()
    s.hook(s.v303("session_start"))
    s.hook(s.v303("user_prompt_submit"))
    s.hook(s.v303("pre_tool_use_edit"))
    (s.repo / "pkg" / "a.py").write_text("A = 2\n")
    s.hook(s.v303("post_tool_use_edit"))
    tid = "toolu_commit"
    s.bash(lambda: s.commit("agent commit"), tid)
    s.hook(s.v303("stop"))
    assert s.head() != base
    # The commit is the agent's, inside one Bash call: it changes no line against base.
    assert s.rows("SELECT * FROM agent_deltas WHERE tool_use_id = ?", tid) == []
    row = s.latest("Stop")["pkg/a.py"]
    want = {"agent": 2, "human": 0, "preexisting": 0, "ambiguous": 0, "reverted": 0}
    assert counts(row) == want
    assert (row["coverage"], json.loads(row["warnings"])) == ("full", [])
    heads = {r["event"]: r["head_sha"] for r in s.events() if r["head_sha"]}
    assert heads["SessionStart"] == base
    assert heads["Stop"] == s.head()


def test_head_moved_outside_a_tool_call_is_partial(tmp_path: Path) -> None:
    s = Mapped(tmp_path)
    s.hook(s.v303("session_start"))
    s.append("a", "AGENT = 1\n", "toolu_a")
    (s.repo / "pkg" / "b.py").write_text("OWNER = 1\n")
    s.commit("owner commit between tool calls")
    s.hook(s.v303("stop"))
    rows = s.latest("Stop")
    assert {f: r["coverage"] for f, r in rows.items()} == {
        "pkg/a.py": "partial", "pkg/b.py": "partial"}  # fmt: skip
    assert json.loads(rows["pkg/b.py"]["warnings"]) == ["head_moved_outside_tool"]
    assert counts(rows["pkg/b.py"])["human"] == 2  # the owner's - and + line


def test_head_off_the_base_history_is_unobservable_and_voids_the_task(
    tmp_path: Path,
) -> None:
    s = Mapped(tmp_path)
    first = s.head()
    (s.repo / "pkg" / "a.py").write_text("A = 3\n")
    s.commit("second")
    cmap = s.freeze()
    s.hook(s.v303("session_start"))
    s.register(cmap)
    s.hook(s.v303("user_prompt_submit"))
    s.git("checkout", "-q", first)
    s.hook(s.v303("stop"))
    assert s.events()[-1]["outcome"] == "unobservable"
    assert s.events()[-1]["note"].startswith("head_not_descendant")
    (task,) = s.rows("SELECT status, void_reason FROM tasks")
    assert tuple(task) == ("void", "head_not_descendant")


def test_pointers_are_stored_on_events_and_deltas(tmp_path: Path) -> None:
    s = Mapped(tmp_path)
    s.hook(s.v303("session_start"))
    s.hook(s.v303("pre_tool_use_edit_subagent"))
    (s.repo / "pkg" / "a.py").write_text("A = 4\n")
    s.hook(s.v303("post_tool_use_edit_subagent"))
    # Codex, built from the documented fields: apply_patch with a turn_id.
    codex = {"turn_id": "turn-7", "tool_name": "apply_patch", "prompt_id": None,
             "permission_mode": None, "agent_id": None}  # fmt: skip
    s.append("b", "B2 = 2\n", "call_patch", **codex)
    deltas = s.rows(
        "SELECT DISTINCT tool_name, prompt_id, turn_id, permission_mode, agent_id,"
        " cli_version FROM agent_deltas ORDER BY id"
    )
    sub = json.loads((V303 / "pre_tool_use_edit_subagent.json").read_text())
    assert [tuple(r) for r in deltas] == [
        ("Edit", sub["prompt_id"], None, "bypassPermissions", sub["agent_id"], None),
        ("apply_patch", None, "turn-7", None, None, None),
    ]
    marks = {r["tool_use_id"]: r["prompt_id"] for r in s.events()}
    assert marks[sub["tool_use_id"]] == sub["prompt_id"]


def ask(s: Mapped, labels: list[str], *, tid: str, answer: str | None) -> None:
    """AskUserQuestion's PostToolUse, built from the documented tool_input."""
    options = [{"label": x, "description": "d"} for x in labels]
    question = {"question": "Which?", "header": "P", "multiSelect": False,
                "options": options}  # fmt: skip
    answers = {"Which?": answer} if answer else {}
    body = s.v303("post_tool_use_edit", tool_name="AskUserQuestion", tool_use_id=tid)
    body["tool_input"] = {"questions": [question]}
    body["tool_response"] = {"questions": [question], "answers": answers}
    s.hook(body)


def test_decision_pointer_keeps_counts_an_index_and_a_hash(tmp_path: Path) -> None:
    from praxis.transcript import options_hash

    s = Mapped(tmp_path)
    ask(s, ["CANARY-red", "CANARY-blue"], tid="toolu_q1", answer="CANARY-blue")
    ask(s, ["x", "y", "z"], tid="toolu_q2", answer=None)
    plan = {"plan": "CANARY-PLAN step one"}
    body = s.v303("post_tool_use_edit", tool_name="ExitPlanMode", tool_use_id="toolu_p")
    s.hook({**body, "tool_input": plan, "tool_response": plan})
    got = s.rows(
        "SELECT tool_use_id, option_count, chosen_index, options_hash, coverage"
        " FROM decisions ORDER BY id"
    )
    assert [tuple(r) for r in got] == [
        ("toolu_q1", 2, 1, options_hash(["CANARY-red", "CANARY-blue"]), "full"),
        ("toolu_q2", 3, None, options_hash(["x", "y", "z"]), "partial"),
        ("toolu_p", 2, 0, options_hash(["CANARY-PLAN step one"]), "full"),
    ]


def test_stop_writes_file_edges_through_the_frozen_map(tmp_path: Path) -> None:
    s = Mapped(tmp_path)
    s.hook(s.v303("session_start"))
    s.append("a", "from pkg import b, c\n", "toolu_1")
    s.hook(s.v303("stop"))
    assert s.events()[-1]["note"].endswith("edges: no_frozen_map")
    assert s.rows("SELECT * FROM file_edges") == []
    s.freeze()
    s.append("a", "from pkg import d\n", "toolu_2")
    s.hook(s.v303("stop"))
    got = s.rows("SELECT src_file, src_component, dst_component, sites, module_level"
                 " FROM file_edges ORDER BY dst_component")  # fmt: skip
    assert [tuple(r) for r in got] == [
        ("pkg/a.py", "a", "b", 1, 1), ("pkg/a.py", "a", "c", 1, 1),
        ("pkg/a.py", "a", "d", 1, 1)]  # fmt: skip


def test_segment_closes_at_the_first_stop_after_a_delta(tmp_path: Path) -> None:
    s = Mapped(tmp_path)
    cmap = s.freeze()
    s.hook(s.v303("session_start"))
    s.append("a", "CANARY_LINE = 'qq-77'\n", "toolu_warm")
    s.hook(s.v303("stop"))  # scans pkg/a.py: the edge history's before
    task = s.register(cmap)
    s.hook(s.v303("user_prompt_submit", prompt="CANARY-PROMPT-zx81"))
    s.hook(s.v303("stop"))  # no delta since the bind: the segment stays open
    s.hook(s.v303("user_prompt_submit", prompt_id="p-2", prompt="yes, go on"))
    ask(s, ["CANARY-red", "CANARY-blue"], tid="toolu_q", answer="CANARY-red")
    s.append("a", "from pkg import b\n", "toolu_imp")
    s.hook(s.v303("stop", last_assistant_message="CANARY-REPLY"), stdout=NOTICE)
    s.hook(s.v303("stop"))  # closed already: nothing more is printed
    (row,) = s.rows("SELECT status, bound_prompt_id FROM tasks")
    prompt = json.loads((V303 / "user_prompt_submit.json").read_text())["prompt_id"]
    assert tuple(row) == ("closed", prompt)
    got = s.rows("SELECT component, o_first, o_final, edges_added, edges_removed,"
                 " coverage FROM outcomes WHERE task_id = ?", task)  # fmt: skip
    assert [tuple(r) for r in got] == [
        ("a", 1, None, 1, 0, "full"),
        *((c, 0, None, 0, 0, "full") for c in "bcde"),
    ]
    flags = s.rows("SELECT code, task_id FROM flags")
    assert [tuple(f) for f in flags] == [("owner_answered_question", task)] * 2
    blob = b"".join(p.read_bytes() for p in s.praxis_home.rglob("*") if p.is_file())
    for canary in (b"CANARY", b"qq-77", b"zx81", b"feedback ready"):
        assert canary not in blob, canary


def test_a_task_binds_only_to_a_prompt_after_it(tmp_path: Path) -> None:
    s = Mapped(tmp_path)
    s.register(s.freeze(), ts="2999-01-01T00:00:00.000+00:00")  # G8
    s.hook(s.v303("user_prompt_submit"))
    assert [tuple(r) for r in s.rows("SELECT status FROM tasks")] == [("unbound",)]


def test_codex_segment_closes_without_a_notice(tmp_path: Path) -> None:
    s = Mapped(tmp_path)
    cmap = s.freeze()
    codex = {"turn_id": "turn-1", "prompt_id": None}
    s.hook(s.v303("session_start"))
    s.register(cmap)
    s.hook(s.v303("user_prompt_submit", **codex))
    s.append("c", "from pkg import e\n", "call_1", tool_name="apply_patch", **codex)
    s.hook(s.v303("stop", **codex))  # Codex has no terminalSequence: stdout stays empty
    (row,) = s.rows("SELECT status, bound_prompt_id FROM tasks")
    assert tuple(row) == ("closed", None)
    got = s.rows("SELECT component, o_first, edges_added FROM outcomes"
                 " WHERE o_first = 1")  # fmt: skip
    assert [tuple(r) for r in got] == [("c", 1, None)]  # no earlier scan of pkg/c.py


@pytest.mark.live
def test_live_notice_stays_out_of_messages(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Claude Code itself, -p, praxis hooks via --settings, a registered task. In -p the
    terminalSequence is ignored; 2.1.303 still writes the hook's stdout and command
    path to the JSONL as hook_success and stop_hook_summary entries, so the assertion
    is that no user or assistant message holds a praxis string."""
    from praxis import install

    # The module's integration mark also lands here; spend tokens only under -m live.
    if "live" not in str(request.config.getoption("markexpr")):
        pytest.skip("spends tokens: run with -m live")
    s = Mapped(tmp_path)
    s.register(s.freeze())
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(install.snippet("claude")))
    drop = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION")  # fmt: skip
    env = {k: v for k, v in os.environ.items() if k not in drop}
    run = subprocess.run(
        ["claude", "-p", "Use the Edit tool on pkg/a.py to replace 'A = 1' with"
         " 'from pkg import b'. Then reply with the single word done.",
         "--settings", str(settings), "--model", "haiku", "--output-format", "json",
         "--permission-mode", "bypassPermissions"],
        cwd=s.repo, env={**env, "PRAXIS_HOME": str(s.praxis_home)},
        stdin=subprocess.DEVNULL, capture_output=True, timeout=110, check=True,
    )  # fmt: skip
    sid = json.loads(run.stdout)["session_id"]
    (path,) = Path.home().glob(f".claude/projects/*/{sid}.jsonl")
    lines = path.read_bytes().splitlines()
    path.unlink()
    with contextlib.suppress(OSError):
        path.parent.rmdir()  # only when the run left nothing else there
    assert [tuple(r) for r in s.rows("SELECT status FROM tasks")] == [("closed",)]
    entries = [json.loads(line) for line in lines if b"praxis" in line]
    assert entries, "2.1.303 records hook commands; none found"
    for entry in entries:
        assert entry["type"] in ("attachment", "system"), entry["type"]
        out = entry.get("attachment", {}).get("stdout")
        assert out in (None, NOTICE.decode()), out


def test_a_multi_select_question_is_unscorable(tmp_path: Path) -> None:
    s, q = Mapped(tmp_path), {"multiSelect": True, "options": [{"label": "a"}]}
    body = s.v303("post_tool_use_edit", tool_name="AskUserQuestion", tool_use_id="q")
    s.hook({**body, "tool_input": {"questions": [q]}})
    got = s.rows("SELECT option_count, coverage FROM decisions")
    assert [tuple(r) for r in got] == [(None, "unobservable")]
