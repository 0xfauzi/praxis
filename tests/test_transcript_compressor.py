"""Tests for praxis.transcript_compressor.

US-012 contract: every user turn in the input Session appears
character-for-character in the compressed output, and the order of
user turns is preserved.

US-013 contract: assistant turns of 200 chars or fewer are preserved
verbatim; longer assistant turns are replaced with their first 200
chars followed by `...[+N more chars, M tool calls]`, where N counts
the dropped trailing characters and M counts the assistant turn's
tool calls.

US-014 contract: tool-result turns (Role.TOOL) are dropped entirely;
each assistant tool call is rendered as ``name(args)`` with args
trimmed to the first 80 chars. On a 50-turn fixture the compressed
length is at most 30% of the original.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from praxis.models import Provider, Role, Session, Turn
from praxis.transcript_compressor import (
    ASSISTANT_TRUNCATE_LIMIT,
    TOOL_ARGS_LIMIT,
    compress_transcript,
)


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


# US-014 tests --------------------------------------------------------


def test_role_tool_turn_is_absent_from_output():
    # Role.TOOL turns represent tool-result blocks. They must be dropped
    # entirely from the compressed transcript.
    marker = "UNIQUE_TOOL_RESULT_should_not_appear_omega"
    session = _session([
        Turn(role=Role.USER, content="user question"),
        Turn(role=Role.TOOL, content=marker),
        Turn(role=Role.USER, content="follow-up"),
    ])
    out = compress_transcript(session)
    assert marker not in out


def test_long_role_tool_turn_is_absent_from_output():
    # Even a large tool-result block (the noisiest part of a real
    # transcript) must contribute zero characters to the output.
    big = "x" * 5000
    session = _session([
        Turn(role=Role.USER, content="user"),
        Turn(role=Role.TOOL, content=big),
    ])
    out = compress_transcript(session)
    assert big not in out
    # And no fragment of the big blob leaks through (no run of 200+ x's).
    assert "x" * 200 not in out


def test_tool_call_renders_as_name_with_args():
    # Single tool call on a short assistant turn must appear as
    # `name(args)` with args being the compact JSON of the input.
    tool_calls: list[dict[str, Any]] = [
        {"name": "Read", "input": {"path": "auth.py"}},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content="Reading file.", tool_calls=tool_calls),
    ]))
    # Compact JSON has no spaces between key/value separators.
    expected = 'Read({"path":"auth.py"})'
    assert expected in out


def test_multiple_tool_calls_all_render():
    tool_calls: list[dict[str, Any]] = [
        {"name": "Read", "input": {"path": "a.py"}},
        {"name": "Edit", "input": {"path": "b.py"}},
        {"name": "Bash", "input": {"command": "pytest"}},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content="Doing work.", tool_calls=tool_calls),
    ]))
    assert 'Read({"path":"a.py"})' in out
    assert 'Edit({"path":"b.py"})' in out
    assert 'Bash({"command":"pytest"})' in out


def test_tool_call_args_truncated_to_80_chars():
    # An args payload that exceeds 80 chars must be cut to the first 80.
    long_value = "x" * 200
    tool_calls: list[dict[str, Any]] = [
        {"name": "Read", "input": {"path": long_value}},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content="Short.", tool_calls=tool_calls),
    ]))
    full_args = json.dumps({"path": long_value}, ensure_ascii=False, separators=(",", ":"))
    assert len(full_args) > TOOL_ARGS_LIMIT
    # The full args must NOT be present.
    assert full_args not in out
    # The first 80 chars of the args must be present, prefixed by `Read(`.
    assert f"Read({full_args[:TOOL_ARGS_LIMIT]}" in out


def test_short_tool_call_args_preserved_verbatim():
    # Args shorter than 80 chars must appear verbatim, no truncation.
    tool_calls: list[dict[str, Any]] = [
        {"name": "Bash", "input": {"command": "echo hi"}},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content="Running.", tool_calls=tool_calls),
    ]))
    assert 'Bash({"command":"echo hi"})' in out


def test_tool_call_with_codex_arguments_key():
    # Codex scanner stores args under "arguments" (often as a JSON
    # string), not "input". The renderer must accept either key.
    tool_calls: list[dict[str, Any]] = [
        {"name": "shell", "arguments": '{"cmd":"pytest"}'},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content="Running tests.", tool_calls=tool_calls),
    ]))
    assert 'shell({"cmd":"pytest"})' in out


def test_tool_call_rendering_does_not_count_against_assistant_limit():
    # A short assistant body remains verbatim even when its tool_calls
    # add many characters. The 200-char limit applies to turn.content
    # only -- the rendered tool-call lines are appended after the body.
    content = "Short body."
    tool_calls: list[dict[str, Any]] = [
        {"name": "Read", "input": {"path": f"file_{i}.py"}} for i in range(10)
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content=content, tool_calls=tool_calls),
    ]))
    # Body stays verbatim, no truncation marker.
    assert content in out
    assert "more chars" not in out
    # All ten tool calls render.
    for i in range(10):
        assert f'Read({{"path":"file_{i}.py"}})' in out


def test_tool_call_lines_appear_inside_assistant_block():
    # Tool-call lines belong to the assistant block they came from, not
    # after the closing tag. The judge prompt relies on this for tying
    # tools to their assistant turn.
    tool_calls: list[dict[str, Any]] = [
        {"name": "Read", "input": {"path": "x.py"}},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content="Body.", tool_calls=tool_calls),
        Turn(role=Role.USER, content="next user turn"),
    ]))
    assistant_open = out.index("<assistant>")
    assistant_close = out.index("</assistant>")
    tool_call_pos = out.index('Read({"path":"x.py"})')
    assert assistant_open < tool_call_pos < assistant_close


def test_tool_call_args_none_renders_as_empty_parens():
    # A tool call with no args at all must still render its name.
    tool_calls: list[dict[str, Any]] = [{"name": "Compact"}]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content="Calling.", tool_calls=tool_calls),
    ]))
    assert "Compact()" in out


def _original_length(session: Session) -> int:
    """Sum of raw content lengths across all turns, including tool call
    args. Represents the verbose textual baseline before compression."""
    total = 0
    for turn in session.turns:
        total += len(turn.content)
        for tc in turn.tool_calls:
            total += len(json.dumps(tc, ensure_ascii=False, separators=(",", ":")))
    return total


def test_50_turn_fixture_compressed_to_at_most_30_percent():
    # Build a realistic 50-turn session:
    #   - 25 user turns (short)
    #   - 25 assistant turns (~2000 chars each, 3 tool calls each)
    # Plus a sprinkling of long Role.TOOL turns (tool-result blocks)
    # that must be dropped. The 50-turn count refers to the user/
    # assistant interaction; tool-result turns are extra noise.
    turns: list[Turn] = []
    for i in range(25):
        turns.append(Turn(
            role=Role.USER,
            content=f"Question {i}: please walk me through the next step.",
        ))
        turns.append(Turn(
            role=Role.ASSISTANT,
            content=("Here is a long explanation. " * 80)[:2000],
            tool_calls=[
                {"name": "Read", "input": {"path": f"src/module_{i}.py"}},
                {"name": "Edit", "input": {
                    "path": f"src/module_{i}.py",
                    "old": "old_line_of_code",
                    "new": "new_line_of_code",
                }},
                {"name": "Bash", "input": {"command": "pytest -q"}},
            ],
        ))
        # Tool-result noise that the compressor must drop.
        turns.append(Turn(role=Role.TOOL, content="tool result blob " * 200))
    session = _session(turns)
    # Sanity: at least 50 user+assistant turns.
    real_turns = [t for t in turns if t.role in {Role.USER, Role.ASSISTANT}]
    assert len(real_turns) == 50

    compressed = compress_transcript(session)
    original = _original_length(session)
    assert original > 0
    ratio = len(compressed) / original
    assert ratio <= 0.30, (
        f"compressed/original = {ratio:.3f} "
        f"(compressed={len(compressed)}, original={original}); "
        f"must be <= 0.30"
    )


def test_tool_call_count_in_marker_still_matches_with_rendered_calls():
    # The US-013 marker `M tool calls]` and the new US-014 rendered
    # `name(args)` lines must both appear on a truncated assistant turn.
    content = "d" * 500
    tool_calls: list[dict[str, Any]] = [
        {"name": "Read", "input": {"path": "x.py"}},
        {"name": "Edit", "input": {"path": "x.py"}},
    ]
    out = compress_transcript(_session([
        Turn(role=Role.ASSISTANT, content=content, tool_calls=tool_calls),
    ]))
    assert "...[+300 more chars, 2 tool calls]" in out
    assert 'Read({"path":"x.py"})' in out
    assert 'Edit({"path":"x.py"})' in out
