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
    sessions = [_make_session(f"s{i}", f"prompt {i}") for i in range(7)]
    reply = json.dumps(
        {
            "tasks": [
                {
                    "label": "one bucket",
                    "task_type": "other",
                    "session_ids": [s.stable_id for s in sessions],
                    "rationale": "stub",
                }
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
    assert len(tasks) == 1
    assert tasks[0].session_ids == [s.stable_id for s in sessions]


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
