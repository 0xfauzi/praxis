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
    """Captures the kwargs of the single LLM call so the test can assert on them."""

    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.calls: list[dict[str, Any]] = []

    def messages_create(self, **kwargs: Any) -> _FakeAnthropicResponse:
        self.calls.append(kwargs)
        return _FakeAnthropicResponse(self.reply_text)


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
    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.calls: list[dict[str, Any]] = []

    def chat_create(self, **kwargs: Any) -> _OpenAIResponse:
        self.calls.append(kwargs)
        return _OpenAIResponse(self.reply_text)


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
