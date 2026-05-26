"""Tests for praxis.transcript_compressor.

US-012 contract: every user turn in the input Session appears
character-for-character in the compressed output, and the order of
user turns is preserved.
"""
from __future__ import annotations

from datetime import datetime, timezone

from praxis.models import Provider, Role, Session, Turn
from praxis.transcript_compressor import compress_transcript


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
