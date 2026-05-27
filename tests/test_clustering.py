"""Tests for praxis.scoring.clustering.

Spec section 5: one LLM call per week to the cheap-tier model from the
user's primary provider. Each session block has exactly four fields:
session_id, first user turn truncated to 400 chars, project_hint,
started_at.

US-032 (this file) covers the call-shape contract. Validation, retry,
and singleton fallback are covered by later stories.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from praxis.models import Provider, Role, Session, Turn
from praxis.scoring import clustering


# --- helpers -----------------------------------------------------------------


def _make_session(
    session_id: str,
    first_turn: str,
    project_hint: str | None = None,
    started_at: datetime | None = None,
) -> Session:
    when = started_at or datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    return Session(
        provider=Provider.CLAUDE,
        session_id=session_id,
        started_at=when,
        turns=[
            Turn(role=Role.USER, content=first_turn),
            Turn(role=Role.ASSISTANT, content="(reply)"),
        ],
        source_path=f"/tmp/{session_id}.jsonl",
        project_hint=project_hint,
    )


class _FakeAnthropicBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeAnthropicBlock(text)]


class _CallRecorder:
    """Captures the kwargs of each LLM call so the test can assert on them.

    Accepts either a single reply string (returned for every call) or a list
    of reply strings (returned in order, with the last one repeated if more
    calls happen than replies were provided). The list form is what US-033
    retry tests use to differentiate the first (invalid) reply from the
    second (corrected) reply.
    """

    def __init__(self, replies: str | list[str]) -> None:
        self._replies: list[str] = [replies] if isinstance(replies, str) else list(replies)
        self.calls: list[dict[str, Any]] = []

    def messages_create(self, **kwargs: Any) -> _FakeAnthropicResponse:
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return _FakeAnthropicResponse(self._replies[idx])


def _install_fake_anthropic(monkeypatch: pytest.MonkeyPatch, recorder: _CallRecorder) -> None:
    """Patch praxis.scoring.clustering's `from anthropic import Anthropic` path."""
    import sys
    import types

    fake_module = types.ModuleType("anthropic")

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = types.SimpleNamespace(create=recorder.messages_create)

    fake_module.Anthropic = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


class _OpenAIChoiceMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _OpenAIChoice:
    def __init__(self, content: str) -> None:
        self.message = _OpenAIChoiceMessage(content)


class _OpenAIResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_OpenAIChoice(content)]


class _OpenAIRecorder:
    """OpenAI counterpart of _CallRecorder; same str-or-list-of-str contract."""

    def __init__(self, replies: str | list[str]) -> None:
        self._replies: list[str] = [replies] if isinstance(replies, str) else list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat_create(self, **kwargs: Any) -> _OpenAIResponse:
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return _OpenAIResponse(self._replies[idx])


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, recorder: _OpenAIRecorder) -> None:
    import sys
    import types

    fake_module = types.ModuleType("openai")

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            chat_ns = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=recorder.chat_create)
            )
            self.chat = chat_ns

    fake_module.OpenAI = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)


# --- session-block shape -----------------------------------------------------


def test_session_block_has_exactly_four_required_fields():
    s = _make_session("a", "do a thing", project_hint="/repo/api")
    [block] = clustering.build_session_blocks([s])
    assert set(block.keys()) == {
        "session_id",
        "first_user_turn",
        "project_hint",
        "started_at",
    }


def test_first_user_turn_truncated_to_400_chars():
    long_prompt = "x" * 1000
    s = _make_session("a", long_prompt)
    [block] = clustering.build_session_blocks([s])
    assert len(block["first_user_turn"]) == clustering.FIRST_TURN_MAX_CHARS
    assert clustering.FIRST_TURN_MAX_CHARS == 400


def test_first_user_turn_under_400_chars_is_unchanged():
    short = "fix the login redirect"
    s = _make_session("a", short)
    [block] = clustering.build_session_blocks([s])
    assert block["first_user_turn"] == short


def test_first_user_turn_at_exactly_400_chars_is_unchanged():
    edge = "y" * 400
    s = _make_session("a", edge)
    [block] = clustering.build_session_blocks([s])
    assert block["first_user_turn"] == edge
    assert len(block["first_user_turn"]) == 400


def test_first_user_turn_uses_only_the_first_user_turn():
    # Multiple user turns: only the first should be sent.
    when = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    s = Session(
        provider=Provider.CLAUDE,
        session_id="multi",
        started_at=when,
        turns=[
            Turn(role=Role.USER, content="first ask"),
            Turn(role=Role.ASSISTANT, content="reply"),
            Turn(role=Role.USER, content="second ask SHOULD NOT APPEAR"),
        ],
        source_path="/tmp/multi.jsonl",
    )
    [block] = clustering.build_session_blocks([s])
    assert block["first_user_turn"] == "first ask"


def test_project_hint_defaults_to_none_literal_when_missing():
    s = _make_session("a", "anything", project_hint=None)
    [block] = clustering.build_session_blocks([s])
    assert block["project_hint"] == "none"


def test_project_hint_is_passed_through_when_present():
    s = _make_session("a", "anything", project_hint="/Users/me/code/api")
    [block] = clustering.build_session_blocks([s])
    assert block["project_hint"] == "/Users/me/code/api"


def test_started_at_is_iso_format():
    when = datetime(2026, 5, 20, 9, 15, tzinfo=timezone.utc)
    s = _make_session("a", "anything", started_at=when)
    [block] = clustering.build_session_blocks([s])
    # ISO-format round-trips through fromisoformat.
    assert datetime.fromisoformat(block["started_at"]) == when


def test_session_id_in_block_matches_stable_id():
    s = _make_session("native-provider-id", "anything")
    [block] = clustering.build_session_blocks([s])
    assert block["session_id"] == s.stable_id


# --- single-call contract ----------------------------------------------------


def test_anthropic_single_call_for_all_sessions(monkeypatch):
    # N=7 sessions split into 2 tasks: keeps the single-call contract (one
    # LLM call carries all 7 sessions) while avoiding US-036's anti-collapse
    # re-prompt (1 task with N > 6 would otherwise trigger a second call).
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(7)]
    reply = json.dumps(
        {
            "tasks": [
                {
                    "label": "first bucket",
                    "task_type": "other",
                    "session_ids": [s.stable_id for s in sessions[:4]],
                    "rationale": "stub",
                },
                {
                    "label": "second bucket",
                    "task_type": "other",
                    "session_ids": [s.stable_id for s in sessions[4:]],
                    "rationale": "stub",
                },
            ]
        }
    )
    recorder = _CallRecorder(reply)
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    # Exactly one LLM call regardless of session count.
    assert len(recorder.calls) == 1
    # All sessions appear in the prompt body of that single call.
    body = recorder.calls[0]["messages"][0]["content"]
    for s in sessions:
        assert s.stable_id in body
    # Parsed result reflects the reply.
    assert len(tasks) == 2
    assert tasks[0].session_ids == [s.stable_id for s in sessions[:4]]
    assert tasks[1].session_ids == [s.stable_id for s in sessions[4:]]


def test_anthropic_call_uses_cheap_tier_model_by_default(monkeypatch):
    sessions = [_make_session("only", "do thing")]
    reply = json.dumps(
        {"tasks": [{"label": "x y z", "task_type": "other",
                    "session_ids": [sessions[0].stable_id], "rationale": "."}]}
    )
    recorder = _CallRecorder(reply)
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic(sessions)

    assert recorder.calls[0]["model"] == clustering.ANTHROPIC_CHEAP_MODEL
    assert clustering.ANTHROPIC_CHEAP_MODEL == "claude-haiku-4-5"


def test_openai_call_uses_cheap_tier_model_by_default(monkeypatch):
    sessions = [_make_session("only", "do thing")]
    reply = json.dumps(
        {"tasks": [{"label": "x y z", "task_type": "other",
                    "session_ids": [sessions[0].stable_id], "rationale": "."}]}
    )
    recorder = _OpenAIRecorder(reply)
    _install_fake_openai(monkeypatch, recorder)

    clustering.cluster_with_openai(sessions)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["model"] == clustering.OPENAI_CHEAP_MODEL
    assert clustering.OPENAI_CHEAP_MODEL == "gpt-5-mini"


def test_prompt_contains_one_block_per_session_in_input_order():
    sessions = [
        _make_session("s1", "alpha task"),
        _make_session("s2", "beta task"),
        _make_session("s3", "gamma task"),
    ]
    prompt = clustering.build_prompt(sessions)
    # Each session's stable_id appears, and the order in the JSON matches input order.
    positions = [prompt.find(s.stable_id) for s in sessions]
    assert all(p > -1 for p in positions)
    assert positions == sorted(positions)


def test_prompt_truncates_long_first_turn_inside_call_body(monkeypatch):
    long_prompt = "z" * 1200
    sessions = [_make_session("s1", long_prompt)]
    reply = json.dumps(
        {"tasks": [{"label": "x y z", "task_type": "other",
                    "session_ids": [sessions[0].stable_id], "rationale": "."}]}
    )
    recorder = _CallRecorder(reply)
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic(sessions)

    body = recorder.calls[0]["messages"][0]["content"]
    # The truncated value (length 400) appears; the full 1200-char run does not.
    assert ("z" * 400) in body
    assert ("z" * 401) not in body


# --- top-level dispatcher: primary provider selection ------------------------


def test_cluster_sessions_prefers_anthropic_when_both_keys_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    sessions = [_make_session("s1", "anything")]
    reply = json.dumps(
        {"tasks": [{"label": "x y z", "task_type": "other",
                    "session_ids": [sessions[0].stable_id], "rationale": "."}]}
    )
    anth = _CallRecorder(reply)
    oai = _OpenAIRecorder(reply)
    _install_fake_anthropic(monkeypatch, anth)
    _install_fake_openai(monkeypatch, oai)

    clustering.cluster_sessions(sessions, prefer="anthropic")

    assert len(anth.calls) == 1
    assert len(oai.calls) == 0


def test_cluster_sessions_returns_none_when_no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sessions = [_make_session("s1", "anything")]
    assert clustering.cluster_sessions(sessions) is None


def test_cluster_sessions_empty_input_returns_empty_list_and_makes_no_call(monkeypatch):
    recorder = _CallRecorder("never used")
    _install_fake_anthropic(monkeypatch, recorder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert clustering.cluster_sessions([]) == []
    assert recorder.calls == []


# --- US-033: coverage validation + single re-prompt -------------------------


def test_validate_coverage_returns_none_when_each_id_appears_exactly_once():
    tasks = [
        clustering.Task(label="a", task_type="other", session_ids=["x", "y"], rationale="."),
        clustering.Task(label="b", task_type="other", session_ids=["z"], rationale="."),
    ]
    assert clustering._validate_coverage(tasks, {"x", "y", "z"}) is None


def test_validate_coverage_detects_missing_session_ids():
    tasks = [
        clustering.Task(label="a", task_type="other", session_ids=["x"], rationale="."),
    ]
    error = clustering._validate_coverage(tasks, {"x", "y", "z"})
    assert error is not None
    assert "missing" in error
    assert "y" in error and "z" in error


def test_validate_coverage_detects_duplicated_session_ids():
    # Same session_id used in two different tasks.
    tasks = [
        clustering.Task(label="a", task_type="other", session_ids=["x"], rationale="."),
        clustering.Task(label="b", task_type="other", session_ids=["x", "y"], rationale="."),
    ]
    error = clustering._validate_coverage(tasks, {"x", "y"})
    assert error is not None
    assert "duplicated" in error
    assert "x" in error


def test_validate_coverage_detects_invented_session_ids():
    tasks = [
        clustering.Task(
            label="a", task_type="other", session_ids=["x", "FAKE"], rationale="."
        ),
    ]
    error = clustering._validate_coverage(tasks, {"x"})
    assert error is not None
    assert "invented" in error
    assert "FAKE" in error


def _ok_reply(session_ids: list[str], label: str = "x y z") -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "label": label,
                    "task_type": "other",
                    "session_ids": session_ids,
                    "rationale": ".",
                }
            ]
        }
    )


def test_anthropic_retries_once_when_response_is_missing_an_id(monkeypatch):
    s1 = _make_session("s1", "alpha")
    s2 = _make_session("s2", "beta")
    invalid = _ok_reply([s1.stable_id], label="missing-s2")  # s2 missing
    corrected = _ok_reply([s1.stable_id, s2.stable_id], label="fixed")
    recorder = _CallRecorder([invalid, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1, s2])

    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label == "fixed"
    assert set(tasks[0].session_ids) == {s1.stable_id, s2.stable_id}


def test_anthropic_retries_once_when_response_has_duplicate_id(monkeypatch):
    s1 = _make_session("s1", "alpha")
    s2 = _make_session("s2", "beta")
    duplicated = json.dumps(
        {
            "tasks": [
                {"label": "a", "task_type": "other",
                 "session_ids": [s1.stable_id], "rationale": "."},
                {"label": "b", "task_type": "other",
                 "session_ids": [s1.stable_id, s2.stable_id], "rationale": "."},
            ]
        }
    )
    corrected = _ok_reply([s1.stable_id, s2.stable_id])
    recorder = _CallRecorder([duplicated, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic([s1, s2])

    assert len(recorder.calls) == 2


def test_anthropic_retries_once_when_response_has_invented_id(monkeypatch):
    s1 = _make_session("s1", "alpha")
    # Response includes s1 but also a made-up id that wasn't in input.
    invented = _ok_reply([s1.stable_id, "TOTALLY-FAKE-ID"])
    corrected = _ok_reply([s1.stable_id])
    recorder = _CallRecorder([invented, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic([s1])

    assert len(recorder.calls) == 2


def test_anthropic_retry_prompt_contains_the_validation_error(monkeypatch):
    s1 = _make_session("s1", "alpha")
    s2 = _make_session("s2", "beta")
    invalid = _ok_reply([s1.stable_id])  # s2 missing
    corrected = _ok_reply([s1.stable_id, s2.stable_id])
    recorder = _CallRecorder([invalid, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic([s1, s2])

    second_prompt = recorder.calls[1]["messages"][0]["content"]
    # The retry message carries the validation error verbatim.
    assert "validation" in second_prompt.lower()
    assert "missing" in second_prompt.lower()
    # And the missing session_id itself is named so the model knows what to add back.
    assert s2.stable_id in second_prompt


def test_anthropic_no_retry_when_first_response_is_valid(monkeypatch):
    s1 = _make_session("s1", "alpha")
    s2 = _make_session("s2", "beta")
    valid = _ok_reply([s1.stable_id, s2.stable_id])
    # Only one reply provided; a second call would re-use it, but we assert
    # the second call never happens at all.
    recorder = _CallRecorder([valid])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic([s1, s2])

    assert len(recorder.calls) == 1


def test_openai_retries_once_when_response_is_missing_an_id(monkeypatch):
    s1 = _make_session("s1", "alpha")
    s2 = _make_session("s2", "beta")
    invalid = _ok_reply([s1.stable_id])
    corrected = _ok_reply([s1.stable_id, s2.stable_id], label="fixed")
    recorder = _OpenAIRecorder([invalid, corrected])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai([s1, s2])

    assert len(recorder.calls) == 2
    assert tasks[0].label == "fixed"


def test_openai_retry_prompt_contains_the_validation_error(monkeypatch):
    s1 = _make_session("s1", "alpha")
    invented = _ok_reply([s1.stable_id, "BOGUS"])
    corrected = _ok_reply([s1.stable_id])
    recorder = _OpenAIRecorder([invented, corrected])
    _install_fake_openai(monkeypatch, recorder)

    clustering.cluster_with_openai([s1])

    assert len(recorder.calls) == 2
    second_prompt = recorder.calls[1]["messages"][0]["content"]
    assert "invented" in second_prompt.lower()
    assert "BOGUS" in second_prompt


# --- US-034: singleton fallback on second protocol failure ------------------


def test_task_default_label_source_is_llm():
    # The dataclass default keeps US-032/US-033 callsites - which construct
    # Task without label_source - on the 'llm' branch.
    t = clustering.Task(
        label="x", task_type="other", session_ids=["a"], rationale="."
    )
    assert t.label_source == clustering.LABEL_SOURCE_LLM
    assert clustering.LABEL_SOURCE_LLM == "llm"
    assert clustering.LABEL_SOURCE_FALLBACK == "fallback"


def test_singleton_fallback_label_takes_first_five_words():
    s = _make_session("s1", "fix the bug in our login redirect handler")
    assert (
        clustering._singleton_fallback_label(s) == "fix the bug in our"
    )


def test_singleton_fallback_label_short_turn_is_unchanged():
    s = _make_session("s1", "do thing")
    assert clustering._singleton_fallback_label(s) == "do thing"


def test_singleton_fallback_label_normalises_whitespace():
    # Multiple/odd whitespace shouldn't leak into the label.
    s = _make_session("s1", "   add   tests   for   the   redactor   please   ")
    # split() with no args splits on runs of whitespace AND drops empty edges,
    # so we expect a clean single-space join.
    assert (
        clustering._singleton_fallback_label(s) == "add tests for the redactor"
    )


def test_singleton_fallback_label_empty_when_no_user_turn():
    when = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    s = Session(
        provider=Provider.CLAUDE,
        session_id="no-user",
        started_at=when,
        turns=[Turn(role=Role.ASSISTANT, content="hi")],
        source_path="/tmp/x.jsonl",
    )
    assert clustering._singleton_fallback_label(s) == ""


def test_singleton_fallback_returns_one_task_per_session():
    sessions = [
        _make_session("s1", "alpha task one"),
        _make_session("s2", "beta task two"),
        _make_session("s3", "gamma task three"),
    ]
    tasks = clustering._singleton_fallback(sessions)
    assert len(tasks) == 3
    for task, s in zip(tasks, sessions):
        assert task.session_ids == [s.stable_id]
        assert task.label_source == clustering.LABEL_SOURCE_FALLBACK
        assert task.task_type == "other"


def test_singleton_fallback_labels_match_first_five_words():
    sessions = [
        _make_session("s1", "fix the bug in our login redirect handler"),
        _make_session("s2", "short two"),
    ]
    tasks = clustering._singleton_fallback(sessions)
    assert tasks[0].label == "fix the bug in our"
    assert tasks[1].label == "short two"


def test_anthropic_falls_back_to_singletons_when_retry_also_fails(monkeypatch):
    s1 = _make_session("s1", "alpha task one big")
    s2 = _make_session("s2", "beta task two big")
    # Both calls miss s2 -> coverage validation fails twice -> fallback fires.
    first_invalid = _ok_reply([s1.stable_id])
    second_invalid = _ok_reply([s1.stable_id])
    recorder = _CallRecorder([first_invalid, second_invalid])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1, s2])

    # Exactly 2 calls (original + 1 retry), then fallback.
    assert len(recorder.calls) == 2
    # Fallback: one task per session, with the deterministic label.
    assert len(tasks) == 2
    assert tasks[0].session_ids == [s1.stable_id]
    assert tasks[1].session_ids == [s2.stable_id]
    assert tasks[0].label == "alpha task one big"  # 4 words, all of them
    assert tasks[1].label == "beta task two big"
    assert all(t.label_source == clustering.LABEL_SOURCE_FALLBACK for t in tasks)
    assert all(t.task_type == "other" for t in tasks)


def test_anthropic_falls_back_when_retry_response_is_unparseable(monkeypatch):
    s1 = _make_session("s1", "alpha task one")
    s2 = _make_session("s2", "beta task two")
    first_invalid = _ok_reply([s1.stable_id])  # missing s2
    second_garbage = "this is not JSON at all"
    recorder = _CallRecorder([first_invalid, second_garbage])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1, s2])

    assert len(recorder.calls) == 2
    assert len(tasks) == 2
    assert all(t.label_source == clustering.LABEL_SOURCE_FALLBACK for t in tasks)
    # Order matches input order.
    assert [t.session_ids[0] for t in tasks] == [s1.stable_id, s2.stable_id]


def test_anthropic_no_fallback_when_retry_succeeds(monkeypatch):
    s1 = _make_session("s1", "alpha")
    s2 = _make_session("s2", "beta")
    invalid = _ok_reply([s1.stable_id])  # missing s2
    corrected = _ok_reply([s1.stable_id, s2.stable_id], label="recovered")
    recorder = _CallRecorder([invalid, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1, s2])

    # The corrected reply wins; no fallback.
    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label == "recovered"
    assert tasks[0].label_source == clustering.LABEL_SOURCE_LLM


def test_anthropic_no_retry_no_fallback_when_first_response_is_valid(monkeypatch):
    s1 = _make_session("s1", "alpha")
    valid = _ok_reply([s1.stable_id])
    recorder = _CallRecorder([valid])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1])

    assert len(recorder.calls) == 1
    assert tasks[0].label_source == clustering.LABEL_SOURCE_LLM


def test_openai_falls_back_to_singletons_when_retry_also_fails(monkeypatch):
    s1 = _make_session("s1", "alpha task")
    s2 = _make_session("s2", "beta task")
    first_invalid = _ok_reply([s1.stable_id])
    # Second response invents an id that wasn't in the input.
    second_invalid = _ok_reply([s1.stable_id, s2.stable_id, "GHOST"])
    recorder = _OpenAIRecorder([first_invalid, second_invalid])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai([s1, s2])

    assert len(recorder.calls) == 2
    assert len(tasks) == 2
    assert tasks[0].label == "alpha task"
    assert tasks[1].label == "beta task"
    assert all(t.label_source == clustering.LABEL_SOURCE_FALLBACK for t in tasks)


def test_openai_falls_back_when_retry_response_is_unparseable(monkeypatch):
    s1 = _make_session("s1", "alpha task")
    first_invalid = _ok_reply([s1.stable_id, "INVENTED"])
    second_garbage = "still no JSON here"
    recorder = _OpenAIRecorder([first_invalid, second_garbage])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai([s1])

    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label_source == clustering.LABEL_SOURCE_FALLBACK
    assert tasks[0].label == "alpha task"


# --- US-035: label and task_type validation ---------------------------------


def _reply_with_task(
    session_ids: list[str], label: str = "x y z", task_type: str = "other"
) -> str:
    """Like _ok_reply but lets the test pin label and task_type explicitly,
    so we can drive label / task_type validation."""
    return json.dumps(
        {
            "tasks": [
                {
                    "label": label,
                    "task_type": task_type,
                    "session_ids": session_ids,
                    "rationale": ".",
                }
            ]
        }
    )


def test_label_max_chars_is_60():
    assert clustering.LABEL_MAX_CHARS == 60


def test_label_forbidden_tokens_are_the_spec_four():
    assert clustering.LABEL_FORBIDDEN_TOKENS == (
        "I",
        "you",
        "the user",
        "the assistant",
    )


def test_allowed_task_types_are_the_eight_enum_values():
    assert clustering.ALLOWED_TASK_TYPES == (
        "debugging",
        "refactoring",
        "building_new",
        "planning",
        "learning",
        "research",
        "ops",
        "other",
    )


def test_validate_labels_and_types_passes_for_clean_tasks():
    tasks = [
        clustering.Task(
            label="auth migration debugging",
            task_type="debugging",
            session_ids=["x"],
            rationale=".",
        ),
        clustering.Task(
            label="deckgen UI polish",
            task_type="refactoring",
            session_ids=["y"],
            rationale=".",
        ),
    ]
    assert clustering._validate_labels_and_types(tasks) is None


def test_validate_labels_rejects_label_longer_than_60_chars():
    too_long = "x" * 61
    tasks = [
        clustering.Task(
            label=too_long, task_type="other", session_ids=["x"], rationale="."
        ),
    ]
    error = clustering._validate_labels_and_types(tasks)
    assert error is not None
    assert "60" in error  # the limit appears in the error string
    assert too_long in error


def test_validate_labels_accepts_label_exactly_60_chars():
    # Boundary check: 60 is OK, 61 is not (covered above).
    edge = "y" * 60
    tasks = [
        clustering.Task(label=edge, task_type="other", session_ids=["x"], rationale="."),
    ]
    assert clustering._validate_labels_and_types(tasks) is None


def test_validate_labels_rejects_label_containing_I_as_word():
    tasks = [
        clustering.Task(
            label="I want auth fix",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    error = clustering._validate_labels_and_types(tasks)
    assert error is not None
    assert "forbidden" in error
    assert "I want auth fix" in error


def test_validate_labels_does_not_reject_Iteration_word_boundary():
    # "Iteration" starts with "I" but is not the standalone pronoun.
    tasks = [
        clustering.Task(
            label="Iteration polish pass",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    assert clustering._validate_labels_and_types(tasks) is None


def test_validate_labels_rejects_label_containing_you_as_word():
    tasks = [
        clustering.Task(
            label="you should fix bug",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    error = clustering._validate_labels_and_types(tasks)
    assert error is not None
    assert "forbidden" in error


def test_validate_labels_does_not_reject_your_word_boundary():
    # "your" contains "you" but is a different word.
    tasks = [
        clustering.Task(
            label="your auth code",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    assert clustering._validate_labels_and_types(tasks) is None


def test_validate_labels_rejects_label_containing_the_user():
    tasks = [
        clustering.Task(
            label="the user wanted X",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    error = clustering._validate_labels_and_types(tasks)
    assert error is not None
    assert "forbidden" in error


def test_validate_labels_does_not_reject_the_users_word_boundary():
    # "the users guide" is a real noun phrase, not a persona reference.
    tasks = [
        clustering.Task(
            label="the users guide",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    assert clustering._validate_labels_and_types(tasks) is None


def test_validate_labels_rejects_label_containing_the_assistant():
    tasks = [
        clustering.Task(
            label="the assistant replied",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    error = clustering._validate_labels_and_types(tasks)
    assert error is not None
    assert "forbidden" in error


def test_validate_labels_forbidden_tokens_are_case_insensitive():
    # "You" capitalized at start of label should still trigger.
    tasks = [
        clustering.Task(
            label="You broke the build",
            task_type="other",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    assert clustering._validate_labels_and_types(tasks) is not None


def test_validate_types_rejects_task_type_not_in_enum():
    tasks = [
        clustering.Task(
            label="auth fix",
            task_type="not_a_real_type",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    error = clustering._validate_labels_and_types(tasks)
    assert error is not None
    assert "task_type" in error
    assert "not_a_real_type" in error


def test_validate_types_accepts_each_of_the_eight_enum_values():
    # Every value in ALLOWED_TASK_TYPES must be accepted, with a clean label.
    for tt in clustering.ALLOWED_TASK_TYPES:
        tasks = [
            clustering.Task(
                label="auth fix", task_type=tt, session_ids=["x"], rationale="."
            ),
        ]
        assert clustering._validate_labels_and_types(tasks) is None


def test_validate_combines_label_and_type_errors():
    # Both a too-long label AND an invalid task_type should be reported.
    tasks = [
        clustering.Task(
            label="z" * 61,
            task_type="nope",
            session_ids=["x"],
            rationale=".",
        ),
    ]
    error = clustering._validate_labels_and_types(tasks)
    assert error is not None
    assert "60" in error
    assert "task_type" in error


def test_anthropic_retries_once_when_label_is_too_long(monkeypatch):
    s1 = _make_session("s1", "alpha")
    too_long_label = "q" * 61
    invalid = _reply_with_task([s1.stable_id], label=too_long_label)
    corrected = _reply_with_task([s1.stable_id], label="short label")
    recorder = _CallRecorder([invalid, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1])

    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label == "short label"
    assert tasks[0].label_source == clustering.LABEL_SOURCE_LLM


def test_anthropic_retries_once_when_label_contains_forbidden_token(monkeypatch):
    s1 = _make_session("s1", "alpha")
    invalid = _reply_with_task([s1.stable_id], label="you broke it")
    corrected = _reply_with_task([s1.stable_id], label="login redirect fix")
    recorder = _CallRecorder([invalid, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1])

    assert len(recorder.calls) == 2
    assert tasks[0].label == "login redirect fix"


def test_anthropic_retries_once_when_task_type_is_invalid(monkeypatch):
    s1 = _make_session("s1", "alpha")
    invalid = _reply_with_task([s1.stable_id], task_type="not_real")
    corrected = _reply_with_task([s1.stable_id], task_type="debugging")
    recorder = _CallRecorder([invalid, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1])

    assert len(recorder.calls) == 2
    assert tasks[0].task_type == "debugging"


def test_anthropic_retry_prompt_carries_label_validation_error(monkeypatch):
    s1 = _make_session("s1", "alpha")
    too_long_label = "p" * 61
    invalid = _reply_with_task([s1.stable_id], label=too_long_label)
    corrected = _reply_with_task([s1.stable_id], label="short label")
    recorder = _CallRecorder([invalid, corrected])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic([s1])

    second_prompt = recorder.calls[1]["messages"][0]["content"]
    # The retry message names the validation failure clearly.
    assert "validation" in second_prompt.lower()
    assert "60" in second_prompt
    # And it includes the offending label so the model can see what was wrong.
    assert too_long_label in second_prompt


def test_anthropic_falls_back_when_retry_label_is_still_invalid(monkeypatch):
    s1 = _make_session("s1", "first call alpha")
    s2 = _make_session("s2", "second call beta")
    invalid_first = _reply_with_task(
        [s1.stable_id, s2.stable_id], label="you should fix this"
    )
    invalid_second = _reply_with_task(
        [s1.stable_id, s2.stable_id], label="I will rewrite this"
    )
    recorder = _CallRecorder([invalid_first, invalid_second])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1, s2])

    # 2 calls (original + retry), then singleton fallback.
    assert len(recorder.calls) == 2
    assert len(tasks) == 2
    assert all(t.label_source == clustering.LABEL_SOURCE_FALLBACK for t in tasks)
    assert tasks[0].label == "first call alpha"
    assert tasks[1].label == "second call beta"


def test_anthropic_no_retry_when_label_and_type_are_valid(monkeypatch):
    s1 = _make_session("s1", "alpha")
    valid = _reply_with_task([s1.stable_id], label="clean label", task_type="other")
    recorder = _CallRecorder([valid])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic([s1])

    assert len(recorder.calls) == 1
    assert tasks[0].label_source == clustering.LABEL_SOURCE_LLM


def test_openai_retries_once_when_task_type_is_invalid(monkeypatch):
    s1 = _make_session("s1", "alpha")
    invalid = _reply_with_task([s1.stable_id], task_type="unknown")
    corrected = _reply_with_task([s1.stable_id], task_type="planning")
    recorder = _OpenAIRecorder([invalid, corrected])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai([s1])

    assert len(recorder.calls) == 2
    assert tasks[0].task_type == "planning"


def test_openai_falls_back_when_retry_label_is_still_invalid(monkeypatch):
    s1 = _make_session("s1", "alpha solo work")
    invalid_first = _reply_with_task([s1.stable_id], label="you broke build")
    invalid_second = _reply_with_task([s1.stable_id], label="the user told me")
    recorder = _OpenAIRecorder([invalid_first, invalid_second])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai([s1])

    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label_source == clustering.LABEL_SOURCE_FALLBACK
    assert tasks[0].label == "alpha solo work"


# --- US-036: anti-collapse + anti-singleton shape re-prompts ----------------


def _multi_task_reply(buckets: list[list[str]]) -> str:
    """Build a JSON reply with one task per bucket of session_ids. Used to
    construct well-shaped responses for the shape-retry tests."""
    return json.dumps(
        {
            "tasks": [
                {
                    "label": f"bucket {i}",
                    "task_type": "other",
                    "session_ids": bucket,
                    "rationale": ".",
                }
                for i, bucket in enumerate(buckets)
            ]
        }
    )


def _singleton_reply(session_ids: list[str]) -> str:
    """Build a JSON reply where every session is its own task. Used to
    drive the anti-singleton path."""
    return _multi_task_reply([[sid] for sid in session_ids])


def test_anti_collapse_threshold_is_6():
    assert clustering.ANTI_COLLAPSE_THRESHOLD == 6


def test_anti_singleton_threshold_is_8():
    assert clustering.ANTI_SINGLETON_THRESHOLD == 8


def test_validate_shape_returns_none_for_well_shaped_response():
    tasks = [
        clustering.Task(
            label="a", task_type="other", session_ids=["x", "y", "z"], rationale="."
        ),
        clustering.Task(
            label="b", task_type="other", session_ids=["q", "r"], rationale="."
        ),
    ]
    assert clustering._validate_shape(tasks, n_sessions=5) is None


def test_validate_shape_rejects_collapse_when_one_task_and_n_over_6():
    tasks = [
        clustering.Task(
            label="all",
            task_type="other",
            session_ids=[f"s{i}" for i in range(7)],
            rationale=".",
        ),
    ]
    error = clustering._validate_shape(tasks, n_sessions=7)
    assert error is not None
    assert "collapsed" in error
    assert "7" in error


def test_validate_shape_accepts_one_task_at_collapse_boundary_n_equal_6():
    tasks = [
        clustering.Task(
            label="all",
            task_type="other",
            session_ids=[f"s{i}" for i in range(6)],
            rationale=".",
        ),
    ]
    assert clustering._validate_shape(tasks, n_sessions=6) is None


def test_validate_shape_accepts_one_task_under_collapse_threshold():
    # N=3, one task, well under the threshold.
    tasks = [
        clustering.Task(
            label="a", task_type="other", session_ids=["x", "y", "z"], rationale="."
        ),
    ]
    assert clustering._validate_shape(tasks, n_sessions=3) is None


def test_validate_shape_rejects_all_singletons_when_n_over_8():
    tasks = [
        clustering.Task(
            label=f"t{i}", task_type="other", session_ids=[f"s{i}"], rationale="."
        )
        for i in range(9)
    ]
    error = clustering._validate_shape(tasks, n_sessions=9)
    assert error is not None
    assert "singleton" in error
    assert "9" in error


def test_validate_shape_accepts_all_singletons_at_boundary_n_equal_8():
    tasks = [
        clustering.Task(
            label=f"t{i}", task_type="other", session_ids=[f"s{i}"], rationale="."
        )
        for i in range(8)
    ]
    assert clustering._validate_shape(tasks, n_sessions=8) is None


def test_validate_shape_accepts_all_singletons_under_threshold():
    tasks = [
        clustering.Task(
            label=f"t{i}", task_type="other", session_ids=[f"s{i}"], rationale="."
        )
        for i in range(4)
    ]
    assert clustering._validate_shape(tasks, n_sessions=4) is None


def test_validate_shape_accepts_mixed_response_above_singleton_threshold():
    # N=10 sessions across 3 tasks; one task has a single session but not all
    # tasks are singletons, so anti-singleton does NOT fire.
    tasks = [
        clustering.Task(
            label="a",
            task_type="other",
            session_ids=[f"s{i}" for i in range(4)],
            rationale=".",
        ),
        clustering.Task(
            label="b",
            task_type="other",
            session_ids=[f"s{i}" for i in range(4, 9)],
            rationale=".",
        ),
        clustering.Task(
            label="c", task_type="other", session_ids=["s9"], rationale="."
        ),
    ]
    assert clustering._validate_shape(tasks, n_sessions=10) is None


def test_validate_shape_passes_for_empty_tasks_defensive():
    # If upstream returns no tasks (which should never happen post-validation)
    # the shape validator is a no-op rather than asserting.
    assert clustering._validate_shape([], n_sessions=0) is None


def test_anthropic_retries_once_when_response_collapses_n_over_6(monkeypatch):
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    collapsed = _ok_reply(ids, label="all in one")
    reshape = _multi_task_reply([ids[:3], ids[3:]])
    recorder = _CallRecorder([collapsed, reshape])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    # Original (collapsed) + shape retry (reshape).
    assert len(recorder.calls) == 2
    assert len(tasks) == 2
    assert tasks[0].label == "bucket 0"
    assert tasks[1].label == "bucket 1"
    assert all(t.label_source == clustering.LABEL_SOURCE_LLM for t in tasks)


def test_anthropic_retries_once_when_response_is_all_singletons_with_n_over_8(
    monkeypatch,
):
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(9)]
    ids = [s.stable_id for s in sessions]
    singletons = _singleton_reply(ids)
    reshape = _multi_task_reply([ids[:4], ids[4:7], ids[7:]])
    recorder = _CallRecorder([singletons, reshape])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 2
    assert len(tasks) == 3


def test_anthropic_no_shape_retry_when_one_task_n_equal_6(monkeypatch):
    # Boundary: N=6 in one task is acceptable, no retry.
    sessions = [_make_session(f"s{i}", "thing") for i in range(6)]
    reply = _ok_reply([s.stable_id for s in sessions], label="six in one")
    recorder = _CallRecorder([reply])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 1


def test_anthropic_no_shape_retry_when_all_singletons_n_equal_8(monkeypatch):
    # Boundary: 8 singletons is acceptable, no retry.
    sessions = [_make_session(f"s{i}", "thing") for i in range(8)]
    reply = _singleton_reply([s.stable_id for s in sessions])
    recorder = _CallRecorder([reply])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 1


def test_anthropic_shape_retry_accepts_reshape_regardless_of_shape(monkeypatch):
    # Per US-036 AC #3: after the single shape retry, the response is accepted
    # regardless of shape. Here both attempts collapse all 7 sessions into one
    # task; the second response is accepted anyway (no further retry, no fallback).
    sessions = [_make_session(f"s{i}", "thing") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    collapse_1 = _ok_reply(ids, label="lump 1")
    collapse_2 = _ok_reply(ids, label="lump 2")
    recorder = _CallRecorder([collapse_1, collapse_2])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    # Exactly 2 calls (original + 1 shape retry), then accept.
    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label == "lump 2"
    assert tasks[0].label_source == clustering.LABEL_SOURCE_LLM


def test_anthropic_shape_retry_accepts_singleton_reshape_regardless(monkeypatch):
    # Same acceptance rule for the anti-singleton path: if the reshape is still
    # all singletons, accept it (the user genuinely had a fragmented week).
    sessions = [_make_session(f"s{i}", "thing") for i in range(9)]
    ids = [s.stable_id for s in sessions]
    singletons_1 = _singleton_reply(ids)
    singletons_2 = _singleton_reply(ids)
    recorder = _CallRecorder([singletons_1, singletons_2])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 2
    # Per AC #3 the reshape is accepted as-is: 9 singletons.
    assert len(tasks) == 9


def test_anthropic_shape_retry_prompt_names_collapse(monkeypatch):
    sessions = [_make_session(f"s{i}", "thing") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    collapsed = _ok_reply(ids, label="all in one")
    reshape = _multi_task_reply([ids[:4], ids[4:]])
    recorder = _CallRecorder([collapsed, reshape])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic(sessions)

    retry_prompt = recorder.calls[1]["messages"][0]["content"]
    assert "shape" in retry_prompt.lower()
    assert "collapsed" in retry_prompt.lower()
    assert "split" in retry_prompt.lower()


def test_anthropic_shape_retry_prompt_names_singleton(monkeypatch):
    sessions = [_make_session(f"s{i}", "thing") for i in range(9)]
    ids = [s.stable_id for s in sessions]
    singletons = _singleton_reply(ids)
    reshape = _multi_task_reply([ids[:5], ids[5:]])
    recorder = _CallRecorder([singletons, reshape])
    _install_fake_anthropic(monkeypatch, recorder)

    clustering.cluster_with_anthropic(sessions)

    retry_prompt = recorder.calls[1]["messages"][0]["content"]
    assert "shape" in retry_prompt.lower()
    assert "singleton" in retry_prompt.lower()
    assert "merge" in retry_prompt.lower()


def test_anthropic_shape_retry_unparseable_keeps_original(monkeypatch):
    # Reshape fails to parse; per US-036 AC #3 we still accept "regardless of
    # shape", and the only well-formed response we have is the original.
    sessions = [_make_session(f"s{i}", "thing") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    collapsed = _ok_reply(ids, label="original")
    garbage = "not even close to JSON"
    recorder = _CallRecorder([collapsed, garbage])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label == "original"
    assert tasks[0].label_source == clustering.LABEL_SOURCE_LLM


def test_anthropic_shape_retry_breaking_coverage_keeps_original(monkeypatch):
    # If the shape retry produces a well-shaped response that invents an id
    # (or drops one), the reshape is correctness-invalid. Per AC #3 we accept
    # regardless of SHAPE, but coverage is still a correctness gate, so we
    # keep the (correctness-valid) original tasks.
    sessions = [_make_session(f"s{i}", "thing") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    collapsed = _ok_reply(ids, label="original")
    reshape_invented = _multi_task_reply(
        [ids[:3], ids[3:] + ["GHOST-ID"]]
    )
    recorder = _CallRecorder([collapsed, reshape_invented])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 2
    # Original kept since reshape broke coverage.
    assert len(tasks) == 1
    assert tasks[0].label == "original"


def test_anthropic_no_shape_retry_when_first_response_is_well_shaped(monkeypatch):
    # Multi-task response with N=7: shape is fine, no retry.
    sessions = [_make_session(f"s{i}", "thing") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    reply = _multi_task_reply([ids[:3], ids[3:]])
    recorder = _CallRecorder([reply])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 1
    assert len(tasks) == 2


def test_anthropic_coverage_fallback_short_circuits_shape_retry(monkeypatch):
    # 9 sessions, both coverage attempts fail. The singleton fallback returns
    # 9 tasks (would normally trigger anti-singleton since N=9 > 8), but the
    # fallback path exits BEFORE the shape gate is reached. Total calls: 2.
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(9)]
    ids = [s.stable_id for s in sessions]
    bad_1 = _ok_reply(ids[:5])
    bad_2 = _ok_reply(ids[:6])
    recorder = _CallRecorder([bad_1, bad_2])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    # 2 calls (original + coverage retry); no shape retry.
    assert len(recorder.calls) == 2
    # Singleton fallback fired: one task per session.
    assert len(tasks) == 9
    assert all(t.label_source == clustering.LABEL_SOURCE_FALLBACK for t in tasks)


def test_anthropic_three_calls_when_coverage_retry_then_shape_retry(monkeypatch):
    # Worst-case happy path: coverage error on call 1, valid coverage on call 2
    # but bad shape (1 task with N=7), then a well-shaped reshape on call 3.
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    coverage_bad = _ok_reply(ids[:6], label="missing one")  # missing s6
    shape_bad = _ok_reply(ids, label="all in one")
    well_shaped = _multi_task_reply([ids[:3], ids[3:]])
    recorder = _CallRecorder([coverage_bad, shape_bad, well_shaped])
    _install_fake_anthropic(monkeypatch, recorder)

    tasks = clustering.cluster_with_anthropic(sessions)

    assert len(recorder.calls) == 3
    assert len(tasks) == 2
    assert tasks[0].label == "bucket 0"
    assert tasks[1].label == "bucket 1"


def test_openai_retries_once_when_response_collapses_n_over_6(monkeypatch):
    sessions = [_make_session(f"s{i}", "thing") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    collapsed = _ok_reply(ids, label="one task")
    reshape = _multi_task_reply([ids[:3], ids[3:]])
    recorder = _OpenAIRecorder([collapsed, reshape])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai(sessions)

    assert len(recorder.calls) == 2
    assert len(tasks) == 2


def test_openai_retries_once_when_response_is_all_singletons_with_n_over_8(
    monkeypatch,
):
    sessions = [_make_session(f"s{i}", "thing") for i in range(9)]
    ids = [s.stable_id for s in sessions]
    singletons = _singleton_reply(ids)
    reshape = _multi_task_reply([ids[:4], ids[4:]])
    recorder = _OpenAIRecorder([singletons, reshape])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai(sessions)

    assert len(recorder.calls) == 2
    assert len(tasks) == 2


def test_openai_shape_retry_accepts_reshape_regardless_of_shape(monkeypatch):
    sessions = [_make_session(f"s{i}", "thing") for i in range(7)]
    ids = [s.stable_id for s in sessions]
    collapse_1 = _ok_reply(ids, label="still one")
    collapse_2 = _ok_reply(ids, label="still one again")
    recorder = _OpenAIRecorder([collapse_1, collapse_2])
    _install_fake_openai(monkeypatch, recorder)

    tasks = clustering.cluster_with_openai(sessions)

    assert len(recorder.calls) == 2
    assert len(tasks) == 1
    assert tasks[0].label == "still one again"


# ---------------------------------------------------------------------------
# US-030: same-task exclusion within a pass-1 batch
# ---------------------------------------------------------------------------


def _task(label: str, sessions: list[Session]) -> clustering.Task:
    return clustering.Task(
        label=label,
        task_type="other",
        session_ids=[s.stable_id for s in sessions],
        rationale="",
    )


def test_pass1_batch_size_default_is_five():
    """Spec §9.1: pass 1 sends 5 sessions per LLM call."""
    assert clustering.PASS1_BATCH_SIZE == 5


def test_build_pass1_batches_empty_input_returns_empty_list():
    assert clustering.build_pass1_batches([], []) == []


def test_build_pass1_batches_no_task_mates_groups_by_size():
    """With each session in its own singleton task, the helper should fill
    batches up to ``max_batch_size`` since there are no exclusion conflicts.
    """
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(7)]
    tasks = [_task(f"label {i}", [s]) for i, s in enumerate(sessions)]

    batches = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)

    assert sum(len(b) for b in batches) == 7
    assert [len(b) for b in batches] == [5, 2]


def test_build_pass1_batches_no_two_sessions_from_the_same_task():
    """US-030 AC #1: pass-1 batches never contain two sessions sharing a task.

    Build a window where two tasks each have multiple sessions; the batcher
    must spread same-task siblings across different batches even when there
    would be room for both in a single batch.
    """
    s_a1 = _make_session("a1", "task A part 1")
    s_a2 = _make_session("a2", "task A part 2")
    s_b1 = _make_session("b1", "task B part 1")
    s_b2 = _make_session("b2", "task B part 2")
    sessions = [s_a1, s_a2, s_b1, s_b2]
    tasks = [
        _task("task A", [s_a1, s_a2]),
        _task("task B", [s_b1, s_b2]),
    ]

    batches = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)

    # Every batch must contain at most one session from each task.
    a_ids = {s_a1.stable_id, s_a2.stable_id}
    b_ids = {s_b1.stable_id, s_b2.stable_id}
    for batch in batches:
        batch_ids = {s.stable_id for s in batch}
        assert len(batch_ids & a_ids) <= 1
        assert len(batch_ids & b_ids) <= 1


def test_build_pass1_batches_oversized_task_overflows_into_new_batches():
    """US-030 AC #2: extras from a single oversized task roll into batches
    alone or alongside non-task-mates only.

    A task with 7 sessions in a window where no other task exists must
    produce 7 batches (each holding exactly one of the task's sessions),
    even though ``max_batch_size`` is 5. There is no other session that
    could join without sharing the task.
    """
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(7)]
    tasks = [_task("oversized", sessions)]

    batches = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)

    assert len(batches) == 7
    for batch in batches:
        assert len(batch) == 1


def test_build_pass1_batches_oversized_task_pairs_with_non_task_mates():
    """The extras from an oversized task should be joined by non-task-mates
    when such sessions exist - the constraint is "no two task-mates in one
    batch", not "task-mates must be alone".
    """
    big_task_sessions = [
        _make_session(f"big-{i}", f"big prompt {i}") for i in range(6)
    ]
    other = [_make_session(f"o-{i}", f"other prompt {i}") for i in range(4)]
    sessions = big_task_sessions + other
    tasks = [
        _task("big task", big_task_sessions),
        _task("other task 1", [other[0], other[1]]),
        _task("other task 2", [other[2], other[3]]),
    ]

    batches = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)

    big_ids = {s.stable_id for s in big_task_sessions}
    other_ids = {s.stable_id for s in other}

    # All ten sessions are placed.
    assert sum(len(b) for b in batches) == 10

    # Each batch has at most one session from the big task.
    for batch in batches:
        ids = {s.stable_id for s in batch}
        assert len(ids & big_ids) <= 1

    # At least one batch demonstrates an extra paired with a non-task-mate
    # (i.e., one big-task session and one other-task session together).
    paired = [
        batch for batch in batches
        if any(s.stable_id in big_ids for s in batch)
        and any(s.stable_id in other_ids for s in batch)
    ]
    assert paired, "expected at least one batch to mix big-task with non-task-mates"


def test_build_pass1_batches_respects_max_batch_size():
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(12)]
    # Each session is its own task so the only ceiling is max_batch_size.
    tasks = [_task(f"task {i}", [s]) for i, s in enumerate(sessions)]

    batches = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)

    for batch in batches:
        assert len(batch) <= 5
    assert sum(len(b) for b in batches) == 12


def test_build_pass1_batches_unassigned_sessions_treated_as_singletons():
    """Defensive: sessions absent from ``tasks`` are placed without exclusion.

    This is not the spec-mandated case (clustering must cover every session)
    but the helper should still produce a usable batching rather than crash
    or drop them.
    """
    assigned = [_make_session("a", "part of task")]
    unassigned = [_make_session(f"u{i}", f"loose {i}") for i in range(3)]
    sessions = assigned + unassigned
    tasks = [_task("only task", assigned)]

    batches = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)

    placed_ids = {s.stable_id for batch in batches for s in batch}
    assert placed_ids == {s.stable_id for s in sessions}


def test_build_pass1_batches_rejects_zero_or_negative_batch_size():
    sessions = [_make_session("a", "x")]
    tasks = [_task("t", sessions)]
    with pytest.raises(ValueError):
        clustering.build_pass1_batches(sessions, tasks, max_batch_size=0)
    with pytest.raises(ValueError):
        clustering.build_pass1_batches(sessions, tasks, max_batch_size=-1)


def test_build_pass1_batches_is_deterministic():
    """The builder is deterministic so tests can pin it and so two runs over
    the same inputs produce the same batch layout. Spec-level randomization
    happens after batching (§9.4 in-batch shuffle) and is the caller's job.
    """
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(8)]
    tasks = [
        _task("alpha", sessions[:3]),
        _task("beta", sessions[3:6]),
        _task("gamma", sessions[6:]),
    ]

    a = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)
    b = clustering.build_pass1_batches(sessions, tasks, max_batch_size=5)

    assert [[s.stable_id for s in batch] for batch in a] == [
        [s.stable_id for s in batch] for batch in b
    ]
