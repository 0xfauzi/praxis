"""The analysis-time transcript reader on two inputs: telltale's recorded Claude Code
2.1.257 fixture transcript (no `origin` field, a truncated last entry), and a SYNTHETIC
jsonl written below with the field names of a real 2.1.303 transcript (`origin.kind`,
`promptId` on tool results, `stop_reason`, `toolUseResult`) and of the AskUserQuestion
shape in the Agent SDK docs. Expected values are written before each run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from praxis import transcript as t

pytestmark = pytest.mark.integration

SID = "11111111-2222-4333-8444-555555555555"
_REL = Path("claude/2.1.257/transcript/-telltale-fake-project") / f"{SID}.jsonl"
_HERE = Path(__file__).resolve().parents[2]
# fixtures/sources/ is recorded evidence: the pre-commit hooks that rewrite file endings
# exclude it, and this file's truncated last line and trailing blank lines are what the
# test reads.
FIXTURE = _HERE / "fixtures/sources" / _REL


def fixture() -> Path:
    assert FIXTURE.is_file(), "copy telltale's 2.1.257 fixture transcript to " + str(
        FIXTURE
    )
    return FIXTURE


def test_fixture_2_1_257() -> None:
    s = t.read(fixture())
    assert s.session_id == SID
    assert s.warnings == ("malformed_line:33",)
    assert [(p.index, p.prompt_id, p.ts, p.origin_kind) for p in s.prompts] == [
        (2, "p-1", "2026-09-02T09:00:01.000Z", "absent"),
        (20, "p-2", "2026-09-02T09:01:05.000Z", "absent"),
    ]
    names = s.prompts[0].names
    assert {"secrets_note.txt", "secrets_note", "cat", "Print"} <= names
    assert "secrets" not in names
    assert [(a.index, a.prompt_id, a.ended_turn) for a in s.assistant] == [
        (3, "p-1", False),
        (11, "p-1", False),
        (13, "p-1", False),
        (32, "p-2", False),
    ]
    assert s.assistant[3].names == frozenset({"Done"})
    assert [(u.index, u.tool_use_id, u.name, u.prompt_id) for u in s.tools] == [
        (3, "toolu_01", "Bash", "p-1"),
        (5, "toolu_02", "Edit", "p-1"),
        (7, "toolu_03", "Write", "p-1"),
        (9, "toolu_04", "Bash", "p-1"),
        (22, "toolu_11", "Read", "p-2"),
        (24, "toolu_12", "Grep", "p-2"),
        (26, "toolu_13", "Bash", "p-2"),
        (28, "toolu_14", "Read", "p-2"),
        (30, "toolu_15", "Bash", "p-2"),
    ]
    assert {(u.option_count, u.chosen_index, u.options_hash) for u in s.tools} == {
        (None, None, None)
    }
    assert "TELLTALEFAKE" not in repr(s)


def _user(
    ts: str | None, pid: str, content: object, **extra: object
) -> dict[str, object]:
    entry: dict[str, object] = {"type": "user", "isSidechain": False, "promptId": pid}
    entry["message"] = {"role": "user", "content": content}
    if ts is not None:
        entry["timestamp"] = ts
    return {**entry, **extra}


def _asst(ts: str, blocks: list[object], stop: str | None, **extra: object) -> object:
    message = {"role": "assistant", "content": blocks, "stop_reason": stop}
    return {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": ts,
        "message": message,
        **extra,
    }


def _ask(tid: str, questions: list[tuple[str, list[str]]]) -> dict[str, object]:
    qs = [
        {
            "question": q,
            "header": "h",
            "multiSelect": False,
            "options": [{"label": o, "description": "d"} for o in opts],
        }
        for q, opts in questions
    ]
    return {
        "type": "tool_use",
        "id": tid,
        "name": "AskUserQuestion",
        "input": {"questions": qs},
    }


def _result(ts: str, tid: str, result: object, is_error: bool = False) -> object:
    block = {
        "type": "tool_result",
        "tool_use_id": tid,
        "is_error": is_error,
        "content": "ok",
    }
    return _user(ts, "P1", [block], toolUseResult=result)


PLAN = {
    "type": "tool_use",
    "name": "ExitPlanMode",
    "input": {"plan": "Plan: split cli into verbs."},
}


def synthetic(path: Path) -> Path:
    ts = "2026-10-19T10:00:{:02d}.000Z".format
    rows: list[object] = [
        {"type": "queue-operation", "operation": "enqueue"},
        _user(
            ts(1),
            "P1",
            "Refactor src/praxis/cli.py, store leaks CANARYTOKEN7f3a.",
            origin={"kind": "human"},
            promptSource="sdk",
        ),
        _asst(
            ts(2),
            [{"type": "text", "text": "Reading ./src/praxis/store.py."}],
            "tool_use",
        ),
        _asst(
            ts(3),
            [_ask("tu_ask", [("Which store?", ["sqlite", "files", "postgres"])])],
            "tool_use",
        ),
        _result(ts(4), "tu_ask", {"answers": {"Which store?": "files"}}),
        _asst(
            ts(5),
            [_ask("tu_multi", [("A?", ["x", "y"]), ("B?", ["z", "w"])])],
            "tool_use",
        ),
        _result(ts(6), "tu_multi", {"answers": {"A?": "x", "B?": "w"}}),
        _asst(ts(7), [_ask("tu_free", [("Proceed?", ["yes", "no"])])], "tool_use"),
        _result(ts(8), "tu_free", {"answers": {"Proceed?": "only on Fridays"}}),
        _asst(ts(9), [{**PLAN, "id": "tu_plan"}], "tool_use"),
        _result(ts(10), "tu_plan", {"plan": "Plan: split cli into verbs."}),
        _asst(
            ts(11),
            [
                {
                    "type": "tool_use",
                    "id": "tu_edit",
                    "name": "Edit",
                    "input": {"file_path": "x"},
                }
            ],
            "tool_use",
        ),
        _result(ts(12), "tu_edit", {}),
        _asst(ts(13), [{"type": "text", "text": "Done with the client."}], "end_turn"),
        "{not json CANARYTOKEN7f3a",
        [1, 2],
        _user(None, "PX", "no timestamp", origin={"kind": "human"}),
        _user(ts(17), "P2", "peer report", origin={"kind": "peer"}, isMeta=True),
        _user(ts(18), "P3", "<local-command-caveat>x</local-command-caveat>"),
        _asst(
            ts(19),
            [{"type": "text", "text": "sidechain"}],
            "end_turn",
            isSidechain=True,
        ),
        _asst(ts(20), [{"type": "text", "text": "partial"}], None),
        "",
        _asst(ts(22), [{**PLAN, "id": "tu_plan2"}], "tool_use"),
    ]
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n")
    return path


HASH_STORE = "d55021aec97eca753f178523ad389dc9374cad045c803358d07475a10c23defd"
HASH_YESNO = "5e31c5409a71aa87327068b3e5fffda18377aa7bed9b3c26360621410b5906fc"
HASH_PLAN = "98cced5a0b49891aa28ec046d58b75018f928f2709979e27e0d9629db9c3c7c3"


def test_synthetic_2_1_303_shape(tmp_path: Path) -> None:
    s = t.read(synthetic(tmp_path / "s1.jsonl"))
    assert s.session_id == "s1"
    assert sorted(s.warnings) == [
        "decision_unscorable:5",
        "malformed_line:14",
        "unexpected_shape:15",
        "unexpected_shape:16",
    ]
    assert [(p.index, p.prompt_id, p.origin_kind) for p in s.prompts] == [
        (1, "P1", "human"),
        (17, "P2", "peer"),
        (18, "P3", "absent"),
    ]
    assert {"src/praxis/cli.py", "cli", "store", "CANARYTOKEN7f3a"} <= s.prompts[
        0
    ].names
    assert [(a.index, a.prompt_id, a.ended_turn) for a in s.assistant] == [
        (2, "P1", False),
        (13, "P1", True),
        (20, "P3", None),
    ]
    assert "src/praxis/store.py" in s.assistant[0].names
    assert "client" in s.assistant[1].names
    assert "cli" not in s.assistant[1].names
    got = [
        (
            u.index,
            u.tool_use_id,
            u.prompt_id,
            u.option_count,
            u.chosen_index,
            u.options_hash,
        )
        for u in s.tools
    ]
    assert got == [
        (3, "tu_ask", "P1", 3, 1, HASH_STORE),
        (5, "tu_multi", "P1", None, None, None),
        (7, "tu_free", "P1", 2, None, HASH_YESNO),
        (9, "tu_plan", "P1", 2, 0, HASH_PLAN),
        (11, "tu_edit", "P1", None, None, None),
        (22, "tu_plan2", "P3", 2, None, HASH_PLAN),
    ]
    assert t.options_hash(["sqlite", "files", "postgres"]) == HASH_STORE
    assert "CANARYTOKEN7f3a" not in repr(s)


def test_rejected_plan_is_index_one(tmp_path: Path) -> None:
    rows = [
        _user("t0", "P1", "go", origin={"kind": "human"}),
        _asst("t1", [{**PLAN, "id": "tu_p"}], "tool_use"),
        _result("t2", "tu_p", "Error: rejected", is_error=True),
    ]
    path = tmp_path / "r.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert [(u.option_count, u.chosen_index) for u in t.read(path).tools] == [(2, 1)]


def test_unreadable_file_refuses_without_arguments(tmp_path: Path) -> None:
    with pytest.raises(t.TranscriptError) as caught:
        t.read(tmp_path / "missing.jsonl")
    assert caught.value.args == ("unreadable",)
    assert caught.value.__suppress_context__


def test_locate(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    (projects / "-proj-a" / SID / "subagents").mkdir(parents=True)
    (projects / "-proj-a" / SID / "subagents" / "agent-x.jsonl").write_text("")
    assert t.locate(SID, projects) is None
    shutil.copy(fixture(), projects / "-proj-a" / f"{SID}.jsonl")
    assert t.locate(SID, projects) == projects / "-proj-a" / f"{SID}.jsonl"
    assert t.locate("22222222-2222-4333-8444-555555555555", projects) is None
    (projects / "-proj-b").mkdir()
    shutil.copy(fixture(), projects / "-proj-b" / f"{SID}.jsonl")
    with pytest.raises(t.TranscriptError, match="ambiguous"):
        t.locate(SID, projects)
    with pytest.raises(t.TranscriptError, match="bad_session_id"):
        t.locate("../x", projects)
    assert t.locate(SID, tmp_path / "absent") is None


def test_duplicate_option_labels_leave_choice_unknown(tmp_path: Path) -> None:
    rows = [
        _user("t0", "P1", "go", origin={"kind": "human"}),
        _asst(
            "t1", [_ask("tu_d", [("Which?", ["same", "same", "other"])])], "tool_use"
        ),
        _result("t2", "tu_d", {"answers": {"Which?": "same"}}),
    ]
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert [(u.option_count, u.chosen_index) for u in t.read(path).tools] == [(3, None)]
