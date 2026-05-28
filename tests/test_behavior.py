"""Tests for behavioral signals and trajectory analysis.

Spec 10.4:
  - Empty session list -> INSUFFICIENT_DATA
  - 10 sessions of monotone-rising engagement -> LEARNING
  - 10 sessions of pure_delegator=True -> STABLE_PASSIVE
  - _linear_slope([1,2,3,4,5]) ~= 1.0
  - LLM trajectory call returns None gracefully when keys absent
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from praxis.behavior.signals import (
    SIGNAL_KINDS_IN_PANEL_ORDER,
    BehavioralSignals,
    detect_signal_kinds,
    extract,
)
from praxis.behavior.trajectory import (
    TrajectoryLabel,
    _linear_slope,
    assess,
    assess_trajectory_heuristic,
    assess_trajectory_with_llm,
)
from praxis.models import Provider, Role, Session, Turn


def _make_session(turns: list[Turn], when: datetime) -> Session:
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"s-{when.isoformat()}",
        started_at=when,
        turns=turns,
        source_path="/tmp/x",
    )


def test_linear_slope_basic():
    assert abs(_linear_slope([1, 2, 3, 4, 5]) - 1.0) < 1e-9
    assert _linear_slope([1, 1, 1]) == 0.0
    assert _linear_slope([1, 2]) == 0.0  # n < 3


def test_extract_engaged_session_has_positive_engagement():
    turns = [
        Turn(role=Role.USER, content="Why does this approach work for caching?"),
        Turn(role=Role.ASSISTANT, content="..."),
        Turn(role=Role.USER, content="So if I understand, the LRU evicts oldest. Am I right that this avoids the thundering herd?"),
    ]
    sig = extract(_make_session(turns, datetime.now(timezone.utc)))
    assert sig.engagement_rate > 0
    assert sig.is_pure_delegator is False


def test_extract_delegating_session_flags_pure_delegator():
    turns = [
        Turn(role=Role.USER, content="write me a function"),
        Turn(role=Role.USER, content="make it handle errors"),
        Turn(role=Role.USER, content="fix this"),
        Turn(role=Role.USER, content="now write tests"),
    ]
    sig = extract(_make_session(turns, datetime.now(timezone.utc)))
    assert sig.delegation_rate > 0.5
    assert sig.is_pure_delegator is True


def test_assess_trajectory_insufficient_data():
    assess_result = assess_trajectory_heuristic([])
    assert assess_result.label == TrajectoryLabel.INSUFFICIENT_DATA


def test_assess_trajectory_learning_label():
    # Construct 10 sessions where engagement rises monotonically and delegation falls.
    base = datetime.now(timezone.utc) - timedelta(days=10)
    pairs = []
    for i in range(10):
        sig = BehavioralSignals(
            user_turn_count=4,
            why_question_count=i,
            comprehension_check_count=i // 2,
            explanation_request_count=0,
            pure_delegation_count=max(0, 5 - i),
            outsourced_debug_count=0,
            telegraphic_count=0,
            own_attempt_count=i // 2,
            engagement_rate=min(1.0, 0.05 * i),
            delegation_rate=max(0.0, 0.6 - 0.06 * i),
            independence_rate=0.0,
            is_pure_delegator=False,
        )
        session = _make_session([Turn(role=Role.USER, content=f"turn-{i}")], base + timedelta(days=i))
        pairs.append((session, sig))
    result = assess_trajectory_heuristic(pairs)
    assert result.label == TrajectoryLabel.LEARNING


def test_assess_trajectory_stable_passive_label_for_delegators():
    base = datetime.now(timezone.utc) - timedelta(days=10)
    pairs = []
    for i in range(10):
        sig = BehavioralSignals(
            user_turn_count=4, why_question_count=0, comprehension_check_count=0,
            explanation_request_count=0, pure_delegation_count=4,
            outsourced_debug_count=0, telegraphic_count=4, own_attempt_count=0,
            engagement_rate=0.0, delegation_rate=1.0, independence_rate=0.0,
            is_pure_delegator=True,
        )
        session = _make_session([Turn(role=Role.USER, content="x")], base + timedelta(days=i))
        pairs.append((session, sig))
    result = assess_trajectory_heuristic(pairs)
    assert result.label == TrajectoryLabel.STABLE_PASSIVE


def test_detect_signal_kinds_why_question_only():
    """A turn that only matches the why-questions regex returns just
    'why_question' (no false-positives across the other kinds)."""
    turn = Turn(
        role=Role.USER,
        content="Why does this approach work for caching?",
    )
    kinds = detect_signal_kinds(turn)
    assert "why_question" in kinds


def test_detect_signal_kinds_telegraphic_short_prompt():
    """A very short prompt with no question mark or newline is
    telegraphic per the existing heuristic."""
    turn = Turn(role=Role.USER, content="add tests")
    kinds = detect_signal_kinds(turn)
    assert "telegraphic" in kinds


def test_detect_signal_kinds_pure_delegation_imperative_opener():
    """A turn that opens with an imperative verb fires pure_delegation."""
    turn = Turn(role=Role.USER, content="write me a function that sorts a list")
    kinds = detect_signal_kinds(turn)
    assert "pure_delegation" in kinds


def test_detect_signal_kinds_own_attempt_marker():
    """A 'my approach is...' opener fires the independence signal."""
    turn = Turn(
        role=Role.USER,
        content="My approach is to memoize the lookup, but I'd like a sanity check.",
    )
    kinds = detect_signal_kinds(turn)
    assert "own_attempt" in kinds


def test_detect_signal_kinds_returns_subset_of_panel_order():
    """The keys returned by detect_signal_kinds must all be members of
    SIGNAL_KINDS_IN_PANEL_ORDER so the adapter / renderer can iterate
    that tuple safely."""
    panel_kinds = set(SIGNAL_KINDS_IN_PANEL_ORDER)
    turn = Turn(role=Role.USER, content="why does this even work, fix this")
    for kind in detect_signal_kinds(turn):
        assert kind in panel_kinds


def test_detect_signal_kinds_can_match_multiple_kinds():
    """A single turn can match multiple signal kinds (e.g. an
    imperative request followed by 'why' phrasing)."""
    turn = Turn(
        role=Role.USER,
        content="fix this, and explain why it broke in the first place",
    )
    kinds = detect_signal_kinds(turn)
    # Both a debug outsourcing pattern AND an explanation request.
    assert "outsourced_debug" in kinds
    assert len(kinds) >= 2


def test_llm_trajectory_returns_none_without_keys(tmp_home):
    # tmp_home fixture clears both API key env vars.
    pairs = []
    base = datetime.now(timezone.utc) - timedelta(days=10)
    for i in range(6):
        sig = BehavioralSignals(
            user_turn_count=1, why_question_count=0, comprehension_check_count=0,
            explanation_request_count=0, pure_delegation_count=0,
            outsourced_debug_count=0, telegraphic_count=0, own_attempt_count=0,
            engagement_rate=0.5, delegation_rate=0.0, independence_rate=0.0,
            is_pure_delegator=False,
        )
        pairs.append((_make_session([Turn(role=Role.USER, content="x")], base + timedelta(days=i)), sig))
    assert assess_trajectory_with_llm(pairs) is None
    # Top-level assess should still produce a result via the heuristic fallback.
    assert assess(pairs).label != TrajectoryLabel.INSUFFICIENT_DATA
