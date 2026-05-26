"""Tests for praxis.transcript_compressor.

US-012 contract: every user turn in the input Session appears
character-for-character in the compressed output, and the order of
user turns is preserved.

US-013 contract: assistant turns of 200 chars or fewer are preserved
verbatim; longer assistant turns are replaced with their first 200
chars followed by `...[+N more chars, M tool calls]`, where N counts
the dropped trailing characters and M counts the assistant turn's
tool calls.
"""
from __future__ import annotations

from datetime import datetime, timezone

from praxis.models import Provider, Role, Session, Turn
from praxis.transcript_compressor import ASSISTANT_TRUNCATE_LIMIT, compress_transcript


def _session(turns: list[Turn]) -> Session:
    return Session(
        provider=Provider.CLAUDE,
        session_id="test-session",
        started_at=datetime.now(timezone.utc),
        turns=turns,
        source_path="/tmp/test.jsonl",
    )


def test_single_user_turn_appears_verbatim():
    content = "Goal: refactor auth. Constraint: keep CI under 5 minutes."
    out = compress_transcript(_session([Turn(role=Role.USER, content=content)]))
    assert content in out


def test_every_user_turn_appears_verbatim():
    contents = [
        "First, what is the current schema?",
        "Now apply this migration step-by-step.",
        "Why did the second commit fail CI?",
    ]
    session = _session([Turn(role=Role.USER, content=c) for c in contents])
    out = compress_transcript(session)
    for c in contents:
        assert c in out, f"user content {c!r} missing from compressed output"


def test_user_turn_order_preserved_across_other_roles():
    # Interleaving assistant / tool turns between user turns must not
    # reorder the user content in the compressed output.
    first = "UNIQUE_FIRST_MARKER_alpha"
    second = "UNIQUE_SECOND_MARKER_beta"
    third = "UNIQUE_THIRD_MARKER_gamma"
    session = _session([
        Turn(role=Role.USER, content=first),
        Turn(role=Role.ASSISTANT, content="some assistant reply"),
        Turn(role=Role.USER, content=second),
        Turn(role=Role.TOOL, content="some tool result"),
        Turn(role=Role.USER, content=third),
    ])
    out = compress_transcript(session)
    assert out.index(first) < out.index(second) < out.index(third)


def test_user_content_with_special_characters_appears_verbatim():
    # Code fences, angle brackets, embedded newlines, and tag-like
    # substrings inside user content must survive untouched.
    content = (
        "Here is my snippet:\n"
        "```python\n"
        "def f(x):\n"
        "    return x < 5 and x > 0\n"
        "```\n"
        "And a tag-like literal: <user>not a real tag</user>"
    )
    out = compress_transcript(_session([Turn(role=Role.USER, content=content)]))
    assert content in out


def test_empty_session_does_not_error():
    # No turns at all is a valid input (e.g., a malformed session file
    # the scanner still emitted). The function must return a string and
    # not raise.
    out = compress_transcript(_session([]))
    assert isinstance(out, str)


def test_session_with_no_user_turns_returns_string_without_user_content():
    # Session contains only non-user roles; the user-verbatim contract
    # is vacuously true. The output is still a string. Future stories
    # (US-013/US-014) will populate this with assistant/tool material.
    session = _session([
        Turn(role=Role.ASSISTANT, content="assistant only"),
        Turn(role=Role.TOOL, content="tool only"),
    ])
    out = compress_transcript(session)
    assert isinstance(out, str)


def test_synthetic_session_user_turns_appear_verbatim(synthetic_session_object):
    # End-to-end against the shared fixture used elsewhere in the suite.
    out = compress_transcript(synthetic_session_object)
    for ut in synthetic_session_object.user_turns:
        assert ut.content in out


# US-013 tests --------------------------------------------------------


def test_short_assistant_turn_is_preserved_verbatim():
    content = "Short answer: yes."
    out = compress_transcript(_session([Turn(role=Role.ASSISTANT, content=content)]))
    assert content in out
    assert "more chars" not in out


def test_assistant_turn_at_exactly_limit_is_preserved_verbatim():
    # Boundary: exactly ASSISTANT_TRUNCATE_LIMIT chars must NOT be truncated.
    content = "a" * ASSISTANT_TRUNCATE_LIMIT
    out = compress_transcript(_session([Turn(role=Role.ASSISTANT, content=content)]))
    assert content in out
    assert "more chars" not in out


def test_assistant_turn_one_over_limit_is_truncated_with_marker():
    # Boundary: one char past the limit truncates and shows N=1.
    content = "a" * (ASSISTANT_TRUNCATE_LIMIT + 1)
    out = compress_transcript(_session([Turn(role=Role.ASSISTANT, content=content)]))
    assert "...[+1 more chars, 0 tool calls]" in out
    # The whole content should NOT appear (it was truncated).
    assert content not in out


def test_long_assistant_turn_marker_reports_extra_chars():
    # 350-char content with no tool calls: N = 150, M = 0.
    content = "b" * 350
    out = compress_transcript(_session([Turn(role=Role.ASSISTANT, content=content)]))
    head = "b" * ASSISTANT_TRUNCATE_LIMIT
    assert f"{head}...[+150 more chars, 0 tool calls]" in out


def test_long_assistant_turn_marker_reports_tool_call_count():
    # Truncated turn must report M = number of tool_calls on the turn.
    content = "c" * 500
    tool_calls = [
        {"name": "Read", "input": {"path": "a.py"}},
        {"name": "Edit", "input": {"path": "a.py", "old": "x", "new": "y"}},
        {"name": "Bash", "input": {"command": "pytest"}},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content=content, tool_calls=tool_calls),
    ]))
    assert "...[+300 more chars, 3 tool calls]" in out


def test_short_assistant_turn_with_tool_calls_stays_verbatim():
    # Short content -> verbatim, even if there are tool calls. The
    # `M tool calls` marker only appears on the truncated branch.
    content = "Calling read."
    tool_calls = [{"name": "Read", "input": {"path": "x.py"}}]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content=content, tool_calls=tool_calls),
    ]))
    assert content in out
    assert "tool calls]" not in out
    assert "more chars" not in out


def test_assistant_truncation_does_not_disturb_user_order():
    # A long, truncated assistant turn sandwiched between user turns
    # must not break US-012's ordering invariant for user content.
    first = "USER_FIRST_alpha"
    second = "USER_SECOND_beta"
    long_assistant = "z" * 1000
    out = compress_transcript(_session([
        Turn(role=Role.USER, content=first),
        Turn(role=Role.ASSISTANT, content=long_assistant),
        Turn(role=Role.USER, content=second),
    ]))
    assert first in out
    assert second in out
    assert out.index(first) < out.index(second)
    # And the assistant turn between them was truncated.
    assert "...[+800 more chars, 0 tool calls]" in out


def test_first_200_chars_of_assistant_turn_are_exact_prefix():
    # The truncated body must be the FIRST 200 characters of the input,
    # not some other slice or whitespace-stripped version.
    head = "".join(chr(ord("a") + (i % 26)) for i in range(ASSISTANT_TRUNCATE_LIMIT))
    tail = "TAIL_THAT_SHOULD_BE_GONE_" * 5  # 125 chars
    content = head + tail
    out = compress_transcript(_session([Turn(role=Role.ASSISTANT, content=content)]))
    assert f"{head}...[+{len(tail)} more chars, 0 tool calls]" in out
    assert "TAIL_THAT_SHOULD_BE_GONE" not in out
