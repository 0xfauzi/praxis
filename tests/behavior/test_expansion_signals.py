"""Fixture-driven tests for the US-005 .. US-008 expansion signals.

Each scalar signal (US-005, US-007, US-008 code_comprehension) has 5
positive and 5 negative session fixtures under
tests/behavior/fixtures/<signal_name>/. Each US-006 knowledge-gap
subtype has 5 positive and 5 negative fixtures under
tests/behavior/fixtures/knowledge_gaps/<subtype>/, plus a single
precedence-ambiguous fixture. Each US-008 verification_depth subtype
has 5 positive and 5 negative fixtures under
tests/behavior/fixtures/verification_depth/<subtype>/. US-008's
tool_ladder_level is an ordinal int 0..4 and is tested directly with
constructed Sessions (no fixture grid).

Positive fixtures must produce count >= 1; negative fixtures must
produce count == 0 *for that signal* (other gap subtypes may still
fire on the same turn -- the negative test only constrains its target).
Empty input also yields 0 (no false positives on blank transcripts).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from praxis.behavior.signals import (
    KNOWLEDGE_GAP_KEYS,
    VERIFICATION_DEPTH_KEYS,
    extract,
)
from praxis.models import Provider, Role, Session, Turn

FIXTURES_DIR = Path(__file__).parent / "fixtures"
KNOWLEDGE_GAPS_DIR = FIXTURES_DIR / "knowledge_gaps"
VERIFICATION_DEPTH_DIR = FIXTURES_DIR / "verification_depth"


def _load_session(fixture_path: Path) -> Session:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    turns = [Turn(role=Role(item["role"]), content=item["content"]) for item in payload]
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"fixture-{fixture_path.stem}",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=turns,
        source_path=str(fixture_path),
    )


def _fixture_paths(signal: str, polarity: str) -> list[Path]:
    paths = sorted((FIXTURES_DIR / signal).glob(f"{polarity}_*.json"))
    assert len(paths) == 5, f"Expected 5 {polarity} fixtures for {signal}, found {len(paths)}"
    return paths


_SCALAR_SIGNAL_PAIRS: tuple[tuple[str, str], ...] = (
    # US-005
    ("specification_artifact", "specification_artifact_count"),
    ("error_naming", "error_naming_count"),
    ("iterative_refinement", "iterative_refinement_count"),
    # US-007
    ("plan_mode", "plan_mode_count"),
    ("scaffolding_artifact", "scaffolding_artifact_count"),
    ("tdd_marker", "tdd_marker_count"),
    ("recipe_pattern", "recipe_pattern_count"),
    ("context_instructions", "context_instructions_count"),
    # US-008
    ("code_comprehension", "code_comprehension_count"),
)


@pytest.mark.parametrize("signal,attribute", _SCALAR_SIGNAL_PAIRS)
def test_positive_fixtures_fire(signal: str, attribute: str) -> None:
    for path in _fixture_paths(signal, "positive"):
        session = _load_session(path)
        sig = extract(session)
        count = getattr(sig, attribute)
        assert count >= 1, f"Positive fixture {path.name} for {signal} produced {attribute}={count}"


@pytest.mark.parametrize("signal,attribute", _SCALAR_SIGNAL_PAIRS)
def test_negative_fixtures_silent(signal: str, attribute: str) -> None:
    for path in _fixture_paths(signal, "negative"):
        session = _load_session(path)
        sig = extract(session)
        count = getattr(sig, attribute)
        assert count == 0, f"Negative fixture {path.name} for {signal} produced {attribute}={count}"


def test_empty_session_has_zero_new_counts() -> None:
    session = Session(
        provider=Provider.CLAUDE,
        session_id="empty",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=[],
        source_path="/tmp/empty",
    )
    sig = extract(session)
    for _, attribute in _SCALAR_SIGNAL_PAIRS:
        assert getattr(sig, attribute) == 0, attribute
    assert all(v == 0 for v in sig.verification_depth.values())
    assert sig.tool_ladder_level == 0


def test_whitespace_only_session_has_zero_new_counts() -> None:
    session = Session(
        provider=Provider.CLAUDE,
        session_id="ws",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=[Turn(role=Role.USER, content="   \n\t  \n  ")],
        source_path="/tmp/ws",
    )
    sig = extract(session)
    for _, attribute in _SCALAR_SIGNAL_PAIRS:
        assert getattr(sig, attribute) == 0, attribute
    assert all(v == 0 for v in sig.verification_depth.values())
    assert sig.tool_ladder_level == 0


# --- US-006: Knowledge-gap four-subtype classifier ---------------------------

_KNOWLEDGE_GAP_SUBTYPES = (
    "missing_context",
    "missing_specs",
    "multiple_context",
    "unclear_instructions",
)


def _knowledge_gap_fixture_paths(subtype: str, polarity: str) -> list[Path]:
    paths = sorted((KNOWLEDGE_GAPS_DIR / subtype).glob(f"{polarity}_*.json"))
    assert len(paths) == 5, (
        f"Expected 5 {polarity} fixtures for knowledge_gap subtype {subtype}, found {len(paths)}"
    )
    return paths


@pytest.mark.parametrize("subtype", _KNOWLEDGE_GAP_SUBTYPES)
def test_knowledge_gap_positive_fixtures_fire(subtype: str) -> None:
    for path in _knowledge_gap_fixture_paths(subtype, "positive"):
        session = _load_session(path)
        sig = extract(session)
        count = sig.knowledge_gaps[subtype]
        assert count >= 1, (
            f"Positive fixture {path.name} for knowledge_gaps[{subtype}] produced {count}"
        )


@pytest.mark.parametrize("subtype", _KNOWLEDGE_GAP_SUBTYPES)
def test_knowledge_gap_negative_fixtures_silent(subtype: str) -> None:
    for path in _knowledge_gap_fixture_paths(subtype, "negative"):
        session = _load_session(path)
        sig = extract(session)
        count = sig.knowledge_gaps[subtype]
        assert count == 0, (
            f"Negative fixture {path.name} for knowledge_gaps[{subtype}] produced {count}"
        )


def test_knowledge_gaps_all_keys_present_on_empty_session() -> None:
    """An empty session must construct knowledge_gaps with all four keys at 0,
    not an empty dict. This is the "writes 0 for all keys when no gap
    detected" half of the US-006 AC, applied to the empty-user-turns branch
    of extract()."""
    session = Session(
        provider=Provider.CLAUDE,
        session_id="empty-gaps",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=[],
        source_path="/tmp/empty",
    )
    sig = extract(session)
    assert set(sig.knowledge_gaps.keys()) == set(KNOWLEDGE_GAP_KEYS)
    assert all(v == 0 for v in sig.knowledge_gaps.values())


def test_knowledge_gaps_all_keys_present_when_no_gap_detected() -> None:
    """A session with one well-specified turn (no gap detected) must still
    populate all four knowledge_gaps keys at 0, not omit them."""
    turn = Turn(
        role=Role.USER,
        content=(
            "Goal: parse our CSV inputs. Acceptance criteria: handles empty "
            "rows and utf-8 encoding. Constraints: stdlib only."
        ),
    )
    session = Session(
        provider=Provider.CLAUDE,
        session_id="well-specified",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=[turn],
        source_path="/tmp/well-specified",
    )
    sig = extract(session)
    assert set(sig.knowledge_gaps.keys()) == set(KNOWLEDGE_GAP_KEYS)
    assert sum(sig.knowledge_gaps.values()) == 0


def test_knowledge_gaps_precedence_no_double_count() -> None:
    """A user turn that matches both `missing_specs` AND
    `unclear_instructions` must increment ONLY the higher-precedence key
    (missing_specs) -- not both. This is the explicit US-006 AC against
    double-counting.

    The fixture turn is 'build me a new feature for the dashboard somehow':
      - matches missing_specs   (starts with 'build me', no spec markers)
      - matches unclear_instructions ('somehow' qualifier)
      - does NOT match missing_context (no bare this/it/that referent)
      - does NOT match multiple_context (no 'also'/'additionally' markers)
    Precedence: missing_specs > unclear_instructions, so only missing_specs
    increments and the session total stays at 1 (not 2).
    """
    fixture = KNOWLEDGE_GAPS_DIR / "precedence_ambiguous_01.json"
    session = _load_session(fixture)
    sig = extract(session)
    assert sig.knowledge_gaps["missing_specs"] == 1
    assert sig.knowledge_gaps["unclear_instructions"] == 0
    assert sig.knowledge_gaps["missing_context"] == 0
    assert sig.knowledge_gaps["multiple_context"] == 0
    # Per-precedence: exactly one gap counted across all subtypes for the
    # single user turn in this fixture, not two.
    assert sum(sig.knowledge_gaps.values()) == 1


# --- US-008: Verification depth + code comprehension + tool ladder ------------


def _verification_depth_fixture_paths(subtype: str, polarity: str) -> list[Path]:
    paths = sorted((VERIFICATION_DEPTH_DIR / subtype).glob(f"{polarity}_*.json"))
    assert len(paths) == 5, (
        f"Expected 5 {polarity} fixtures for verification_depth subtype "
        f"{subtype}, found {len(paths)}"
    )
    return paths


@pytest.mark.parametrize("subtype", VERIFICATION_DEPTH_KEYS)
def test_verification_depth_positive_fixtures_fire(subtype: str) -> None:
    for path in _verification_depth_fixture_paths(subtype, "positive"):
        session = _load_session(path)
        sig = extract(session)
        count = sig.verification_depth[subtype]
        assert count >= 1, (
            f"Positive fixture {path.name} for verification_depth[{subtype}] produced {count}"
        )


@pytest.mark.parametrize("subtype", VERIFICATION_DEPTH_KEYS)
def test_verification_depth_negative_fixtures_silent(subtype: str) -> None:
    for path in _verification_depth_fixture_paths(subtype, "negative"):
        session = _load_session(path)
        sig = extract(session)
        count = sig.verification_depth[subtype]
        assert count == 0, (
            f"Negative fixture {path.name} for verification_depth[{subtype}] produced {count}"
        )


def test_verification_depth_all_keys_present_on_empty_session() -> None:
    session = Session(
        provider=Provider.CLAUDE,
        session_id="empty-verif",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=[],
        source_path="/tmp/empty",
    )
    sig = extract(session)
    assert set(sig.verification_depth.keys()) == set(VERIFICATION_DEPTH_KEYS)
    assert all(v == 0 for v in sig.verification_depth.values())


def _ladder_session(turns: list[Turn]) -> Session:
    return Session(
        provider=Provider.CLAUDE,
        session_id="ladder",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=turns,
        source_path="/tmp/ladder",
    )


def test_tool_ladder_empty_session_is_zero() -> None:
    sig = extract(_ladder_session([]))
    assert sig.tool_ladder_level == 0


def test_tool_ladder_prompt_only_is_zero() -> None:
    turns = [Turn(role=Role.USER, content="Write a function to parse JSON.")]
    sig = extract(_ladder_session(turns))
    assert sig.tool_ladder_level == 0


def test_tool_ladder_tools_on_text_is_one() -> None:
    turns = [Turn(role=Role.USER, content="Run this with tools enabled, please.")]
    sig = extract(_ladder_session(turns))
    assert sig.tool_ladder_level == 1


def test_tool_ladder_tool_call_text_is_two() -> None:
    turns = [Turn(role=Role.USER, content="I made a tool call to read the file.")]
    sig = extract(_ladder_session(turns))
    assert sig.tool_ladder_level == 2


def test_tool_ladder_structural_tool_calls_attribute_is_two() -> None:
    """Turn.tool_calls truthy (populated by the scanners on assistant turns)
    elevates a session to rung 2 even without any "tool call" text."""
    turns = [
        Turn(role=Role.USER, content="Read the auth module."),
        Turn(
            role=Role.ASSISTANT,
            content="Reading.",
            tool_calls=[{"name": "Read", "arguments": "/path/to/auth.py"}],
        ),
    ]
    sig = extract(_ladder_session(turns))
    assert sig.tool_ladder_level == 2


def test_tool_ladder_hook_reference_is_three() -> None:
    turns = [Turn(role=Role.USER, content="I added a PreToolUse hook to check the format.")]
    sig = extract(_ladder_session(turns))
    assert sig.tool_ladder_level == 3


def test_tool_ladder_subagent_reference_is_four() -> None:
    turns = [Turn(role=Role.USER, content="Spawn a subagent to handle the long-running search.")]
    sig = extract(_ladder_session(turns))
    assert sig.tool_ladder_level == 4


def test_tool_ladder_mix_tool_and_hook_resolves_to_three_not_sum() -> None:
    """The explicit US-008 AC: a session mixing a tool call AND a hook
    reference resolves to ladder_level = max(2, 3) = 3, not the sum 5.
    """
    turns = [
        Turn(role=Role.USER, content="I made a tool call to read the file."),
        Turn(role=Role.USER, content="I added a PreToolUse hook to check the format."),
    ]
    sig = extract(_ladder_session(turns))
    assert sig.tool_ladder_level == 3
    # And NOT the sum:
    assert sig.tool_ladder_level != 5
