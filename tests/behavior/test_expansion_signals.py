"""Fixture-driven tests for the US-005, US-006, and US-007 expansion signals.

Each scalar signal (US-005 and US-007) has 5 positive and 5 negative
session fixtures under tests/behavior/fixtures/<signal_name>/. Each
US-006 knowledge-gap subtype has 5 positive and 5 negative fixtures
under tests/behavior/fixtures/knowledge_gaps/<subtype>/, plus a single
precedence-ambiguous fixture at
tests/behavior/fixtures/knowledge_gaps/precedence_ambiguous_01.json.

Positive fixtures must produce count >= 1; negative fixtures must
produce count == 0 *for that signal* (other gap subtypes may still
fire on the same turn -- the negative test only constrains its target).
Empty input also yields 0 (no false positives on blank transcripts).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from praxis.behavior.signals import KNOWLEDGE_GAP_KEYS, extract
from praxis.models import Provider, Role, Session, Turn


FIXTURES_DIR = Path(__file__).parent / "fixtures"
KNOWLEDGE_GAPS_DIR = FIXTURES_DIR / "knowledge_gaps"


def _load_session(fixture_path: Path) -> Session:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    turns = [
        Turn(role=Role(item["role"]), content=item["content"])
        for item in payload
    ]
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"fixture-{fixture_path.stem}",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        turns=turns,
        source_path=str(fixture_path),
    )


def _fixture_paths(signal: str, polarity: str) -> list[Path]:
    paths = sorted((FIXTURES_DIR / signal).glob(f"{polarity}_*.json"))
    assert len(paths) == 5, (
        f"Expected 5 {polarity} fixtures for {signal}, found {len(paths)}"
    )
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
)


@pytest.mark.parametrize("signal,attribute", _SCALAR_SIGNAL_PAIRS)
def test_positive_fixtures_fire(signal: str, attribute: str) -> None:
    for path in _fixture_paths(signal, "positive"):
        session = _load_session(path)
        sig = extract(session)
        count = getattr(sig, attribute)
        assert count >= 1, (
            f"Positive fixture {path.name} for {signal} produced {attribute}={count}"
        )


@pytest.mark.parametrize("signal,attribute", _SCALAR_SIGNAL_PAIRS)
def test_negative_fixtures_silent(signal: str, attribute: str) -> None:
    for path in _fixture_paths(signal, "negative"):
        session = _load_session(path)
        sig = extract(session)
        count = getattr(sig, attribute)
        assert count == 0, (
            f"Negative fixture {path.name} for {signal} produced {attribute}={count}"
        )


def test_empty_session_has_zero_new_counts() -> None:
    session = Session(
        provider=Provider.CLAUDE,
        session_id="empty",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        turns=[],
        source_path="/tmp/empty",
    )
    sig = extract(session)
    for _, attribute in _SCALAR_SIGNAL_PAIRS:
        assert getattr(sig, attribute) == 0, attribute


def test_whitespace_only_session_has_zero_new_counts() -> None:
    session = Session(
        provider=Provider.CLAUDE,
        session_id="ws",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        turns=[Turn(role=Role.USER, content="   \n\t  \n  ")],
        source_path="/tmp/ws",
    )
    sig = extract(session)
    for _, attribute in _SCALAR_SIGNAL_PAIRS:
        assert getattr(sig, attribute) == 0, attribute


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
        f"Expected 5 {polarity} fixtures for knowledge_gap subtype "
        f"{subtype}, found {len(paths)}"
    )
    return paths


@pytest.mark.parametrize("subtype", _KNOWLEDGE_GAP_SUBTYPES)
def test_knowledge_gap_positive_fixtures_fire(subtype: str) -> None:
    for path in _knowledge_gap_fixture_paths(subtype, "positive"):
        session = _load_session(path)
        sig = extract(session)
        count = sig.knowledge_gaps[subtype]
        assert count >= 1, (
            f"Positive fixture {path.name} for knowledge_gaps[{subtype}] "
            f"produced {count}"
        )


@pytest.mark.parametrize("subtype", _KNOWLEDGE_GAP_SUBTYPES)
def test_knowledge_gap_negative_fixtures_silent(subtype: str) -> None:
    for path in _knowledge_gap_fixture_paths(subtype, "negative"):
        session = _load_session(path)
        sig = extract(session)
        count = sig.knowledge_gaps[subtype]
        assert count == 0, (
            f"Negative fixture {path.name} for knowledge_gaps[{subtype}] "
            f"produced {count}"
        )


def test_knowledge_gaps_all_keys_present_on_empty_session() -> None:
    """An empty session must construct knowledge_gaps with all four keys at 0,
    not an empty dict. This is the "writes 0 for all keys when no gap
    detected" half of the US-006 AC, applied to the empty-user-turns branch
    of extract()."""
    session = Session(
        provider=Provider.CLAUDE,
        session_id="empty-gaps",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
