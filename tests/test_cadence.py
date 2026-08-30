"""Tests for praxis.behavior.cadence (US-012, US-013).

Covers:
  - The pure substantive-session predicate plus its threshold constants.
  - compute_weekday_streak over an empty/sparse/dense profile_store.
  - high_adopter_position thresholds, boundaries, and invariant checks.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from praxis.behavior.cadence import (
    DEFAULT_STREAK_WINDOW_DAYS,
    HIGH_STREAK_RATIO,
    LOW_STREAK_RATIO,
    MIN_ELAPSED_SECONDS,
    MIN_USER_TURNS,
    compute_weekday_streak,
    high_adopter_position,
    is_substantive_session,
)
from praxis.scoring.aggregate import SessionScore
from praxis.scoring.features import SessionFeatures
from praxis.scoring.judge import JudgeResult
from praxis.storage.profile_store import ProfileStore

# ----- module-level constants -----------------------------------------------


def test_threshold_constants_are_module_level_and_match_ac():
    """AC: thresholds exposed as module-level constants (US-012, AC #1)."""
    assert MIN_USER_TURNS == 2
    assert MIN_ELAPSED_SECONDS == 60
    assert DEFAULT_STREAK_WINDOW_DAYS == 21


# ----- is_substantive_session: pure check -----------------------------------


def test_is_substantive_session_returns_false_for_none():
    assert is_substantive_session(None) is False


def test_is_substantive_session_returns_false_for_empty_mapping():
    assert is_substantive_session({}) is False


def test_is_substantive_session_true_at_thresholds():
    """Boundary: user_turns == 2 and elapsed_seconds == 60 both qualify."""
    assert (
        is_substantive_session(
            {"user_turns": MIN_USER_TURNS, "elapsed_seconds": MIN_ELAPSED_SECONDS}
        )
        is True
    )


def test_is_substantive_session_false_below_user_turns_threshold():
    assert (
        is_substantive_session({"user_turns": MIN_USER_TURNS - 1, "elapsed_seconds": 600}) is False
    )


def test_is_substantive_session_false_below_elapsed_threshold():
    assert (
        is_substantive_session({"user_turns": 5, "elapsed_seconds": MIN_ELAPSED_SECONDS - 1})
        is False
    )


def test_is_substantive_session_true_for_well_above_thresholds():
    assert is_substantive_session({"user_turns": 20, "elapsed_seconds": 1200.5}) is True


def test_is_substantive_session_missing_fields_treated_as_zero():
    """Missing user_turns or elapsed_seconds yields False, no KeyError."""
    assert is_substantive_session({"elapsed_seconds": 600}) is False
    assert is_substantive_session({"user_turns": 5}) is False


def test_is_substantive_session_rejects_non_numeric_fields():
    """A row with junk values returns False rather than crashing."""
    assert is_substantive_session({"user_turns": "ten", "elapsed_seconds": 600}) is False
    assert is_substantive_session({"user_turns": 5, "elapsed_seconds": "lots"}) is False


def test_is_substantive_session_rejects_booleans():
    """bool is an int subclass; ensure user_turns=True (==1) does not slip through."""
    assert is_substantive_session({"user_turns": True, "elapsed_seconds": 600}) is False


def test_is_substantive_session_accepts_float_user_turns():
    """Floats coerce to int via truncation; 2.5 user_turns counts as 2."""
    assert is_substantive_session({"user_turns": 2.5, "elapsed_seconds": 60}) is True


# ----- compute_weekday_streak: empty / single-session / multi-day -----------


def _make_score(
    *,
    started_at: datetime,
    stable_suffix: str,
) -> SessionScore:
    return SessionScore(
        session_stable_id=f"sid-{stable_suffix}",
        provider="claude",
        started_at=started_at,
        dimension_scores={},
        overall=6.0,
        judge_result=JudgeResult(
            dimension_scores={},
            rationale={},
            standout_moments=[],
            failure_modes=[],
            overall_note="",
            judge_model="test",
            confidence="medium",
        ),
        features=SessionFeatures(turn_count=4, avg_prompt_chars=120.0),
        source_path=f"/tmp/{stable_suffix}.jsonl",
    )


def _seed(
    store: ProfileStore,
    *,
    when: datetime,
    user_turns: int,
    elapsed_seconds: float,
    suffix: str,
) -> None:
    """Persist one session_score row with the cadence-relevant signals fields."""
    score = _make_score(started_at=when, stable_suffix=suffix)
    signals = {
        "user_turn_count": user_turns,
        "elapsed_seconds": elapsed_seconds,
    }
    store.save_session_score(score, signals=signals)


def test_compute_weekday_streak_empty_store_returns_zero(tmp_home):
    """AC: never raises on empty profile_store, returns 0 (not None)."""
    store = ProfileStore()
    result = compute_weekday_streak(store, ending_on_date=date(2026, 5, 28))
    assert result == 0
    assert isinstance(result, int)


def test_compute_weekday_streak_single_substantive_session(tmp_home):
    store = ProfileStore()
    when = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)
    _seed(store, when=when, user_turns=4, elapsed_seconds=300, suffix="a")
    assert compute_weekday_streak(store, ending_on_date=date(2026, 5, 28)) == 1


def test_compute_weekday_streak_non_substantive_session_excluded(tmp_home):
    """A session with <2 user turns OR <60s elapsed does not count."""
    store = ProfileStore()
    base = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)
    # Below user_turns threshold.
    _seed(store, when=base, user_turns=1, elapsed_seconds=300, suffix="few-turns")
    # Below elapsed threshold (different day, so it would otherwise count).
    _seed(
        store,
        when=base + timedelta(days=1),
        user_turns=10,
        elapsed_seconds=30,
        suffix="too-short",
    )
    assert compute_weekday_streak(store, ending_on_date=date(2026, 5, 28)) == 0


def test_compute_weekday_streak_multiple_distinct_days(tmp_home):
    store = ProfileStore()
    base = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
    for offset in range(5):
        _seed(
            store,
            when=base + timedelta(days=offset),
            user_turns=3,
            elapsed_seconds=200,
            suffix=f"day{offset}",
        )
    # 5 distinct days, all within 21-day window ending 2026-05-28.
    assert compute_weekday_streak(store, ending_on_date=date(2026, 5, 28)) == 5


def test_compute_weekday_streak_dedupes_within_same_day(tmp_home):
    """Two substantive sessions on one day count as 1 distinct weekday."""
    store = ProfileStore()
    morning = datetime(2026, 5, 27, 9, 0, tzinfo=UTC)
    evening = datetime(2026, 5, 27, 21, 0, tzinfo=UTC)
    _seed(store, when=morning, user_turns=3, elapsed_seconds=200, suffix="am")
    _seed(store, when=evening, user_turns=4, elapsed_seconds=400, suffix="pm")
    assert compute_weekday_streak(store, ending_on_date=date(2026, 5, 28)) == 1


def test_compute_weekday_streak_excludes_sessions_before_window(tmp_home):
    """A session 22 days before ending_on_date is outside the default 21-day window."""
    store = ProfileStore()
    end = date(2026, 5, 28)
    # 22 days before end (inclusive window of 21 means earliest in-window day is end - 20).
    out_of_window = datetime.combine(end - timedelta(days=22), datetime.min.time(), tzinfo=UTC)
    _seed(store, when=out_of_window, user_turns=5, elapsed_seconds=600, suffix="ancient")
    assert compute_weekday_streak(store, ending_on_date=end) == 0


def test_compute_weekday_streak_excludes_sessions_after_ending_on_date(tmp_home):
    store = ProfileStore()
    end = date(2026, 5, 20)
    future = datetime(2026, 5, 25, 10, 0, tzinfo=UTC)
    _seed(store, when=future, user_turns=5, elapsed_seconds=600, suffix="future")
    assert compute_weekday_streak(store, ending_on_date=end) == 0


def test_compute_weekday_streak_window_days_configurable(tmp_home):
    """window_days=7 narrows the streak to a 7-day rolling window."""
    store = ProfileStore()
    end = date(2026, 5, 28)
    # Day inside the 21-day default window, outside a 7-day window.
    older = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    _seed(store, when=older, user_turns=3, elapsed_seconds=300, suffix="old")
    assert compute_weekday_streak(store, ending_on_date=end) == 1
    assert compute_weekday_streak(store, ending_on_date=end, window_days=7) == 0


def test_compute_weekday_streak_non_positive_window_returns_zero(tmp_home):
    """Defensive: window_days<=0 returns 0 rather than negative or raising."""
    store = ProfileStore()
    when = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)
    _seed(store, when=when, user_turns=4, elapsed_seconds=300, suffix="a")
    end = date(2026, 5, 28)
    assert compute_weekday_streak(store, ending_on_date=end, window_days=0) == 0
    assert compute_weekday_streak(store, ending_on_date=end, window_days=-3) == 0


def test_compute_weekday_streak_handles_rows_without_signals_json(tmp_home):
    """Sessions persisted before signals_json was added must not crash; they
    just don't qualify as substantive (user_turns = 0 by default)."""
    store = ProfileStore()
    score = _make_score(started_at=datetime(2026, 5, 25, 14, 0, tzinfo=UTC), stable_suffix="legacy")
    store.save_session_score(score)  # signals=None
    assert compute_weekday_streak(store, ending_on_date=date(2026, 5, 28)) == 0


def test_compute_weekday_streak_handles_malformed_signals_json(tmp_home, monkeypatch):
    """A signals_json that isn't valid JSON falls back to 0 without crashing."""
    store = ProfileStore()
    score = _make_score(
        started_at=datetime(2026, 5, 25, 14, 0, tzinfo=UTC), stable_suffix="garbage"
    )
    store.save_session_score(score, signals={"user_turn_count": 5, "elapsed_seconds": 300})
    # Stomp the column with invalid JSON to simulate corruption.
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE session_scores SET signals_json = 'not-json' WHERE stable_id = ?",
        ("sid-garbage",),
    )
    conn.commit()
    conn.close()
    assert compute_weekday_streak(store, ending_on_date=date(2026, 5, 28)) == 0


def test_compute_weekday_streak_uses_features_elapsed_seconds_fallback(tmp_home):
    """If signals_json lacks elapsed_seconds, features.elapsed_seconds is used."""
    store = ProfileStore()
    score = _make_score(started_at=datetime(2026, 5, 25, 14, 0, tzinfo=UTC), stable_suffix="feat")
    store.save_session_score(score, signals={"user_turn_count": 5})
    # Stomp features_json to add elapsed_seconds. The adapter should pick it up.
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    enriched = json.dumps(
        {
            "turn_count": 4,
            "avg_prompt_chars": 120.0,
            "marker_hit_counts": {},
            "elapsed_seconds": 600,
        }
    )
    conn.execute(
        "UPDATE session_scores SET features_json = ? WHERE stable_id = ?",
        (enriched, "sid-feat"),
    )
    conn.commit()
    conn.close()
    assert compute_weekday_streak(store, ending_on_date=date(2026, 5, 28)) == 1


def test_compute_weekday_streak_returns_int_type_even_when_zero(tmp_home):
    """AC: returns int, never None, even on an empty/skipped path."""
    store = ProfileStore()
    out = compute_weekday_streak(store, ending_on_date=date(2026, 5, 28))
    assert isinstance(out, int)
    assert out == 0


def test_compute_weekday_streak_inclusive_of_ending_on_date(tmp_home):
    """A substantive session on the ending date itself is counted."""
    store = ProfileStore()
    end = date(2026, 5, 28)
    today = datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(hours=15)
    _seed(store, when=today, user_turns=3, elapsed_seconds=200, suffix="today")
    assert compute_weekday_streak(store, ending_on_date=end) == 1


# ----- high_adopter_position: thresholds + invariants (US-013) --------------


def test_high_adopter_position_ratio_constants_match_paper_quartiles():
    """AC: thresholds are 0.25 / 0.75, matching arXiv 2509.19708 quartile cohorts."""
    assert LOW_STREAK_RATIO == 0.25
    assert HIGH_STREAK_RATIO == 0.75


# Boundary cases for window_days=20 chosen so the thresholds land on exact integers:
#   LOW_STREAK_RATIO * 20 = 5   (streak == 5 is moderate, 4 is low)
#   HIGH_STREAK_RATIO * 20 = 15 (streak == 15 is high, 14 is moderate)
# Boundary cases for window_days=4 chosen so the thresholds land on exact integers:
#   LOW_STREAK_RATIO * 4 = 1    (streak == 1 is moderate, 0 is low)
#   HIGH_STREAK_RATIO * 4 = 3   (streak == 3 is high, 2 is moderate)
# Additional cases for window_days=21 (the production default) and 7 confirm the
# ratio-based tiering generalises across window sizes used in coaching.
@pytest.mark.parametrize(
    "streak, window_days, expected",
    [
        # window=20: exact-integer boundaries.
        (0, 20, "low"),
        (4, 20, "low"),  # just below LOW (0.20)
        (5, 20, "moderate"),  # exactly at LOW (0.25)
        (14, 20, "moderate"),  # just below HIGH (0.70)
        (15, 20, "high"),  # exactly at HIGH (0.75)
        (20, 20, "high"),  # full window
        # window=4: smallest window where boundaries are exact integers.
        (0, 4, "low"),
        (1, 4, "moderate"),  # exactly at LOW (0.25)
        (2, 4, "moderate"),
        (3, 4, "high"),  # exactly at HIGH (0.75)
        (4, 4, "high"),  # full window
        # window=21 (DEFAULT_STREAK_WINDOW_DAYS): non-integer boundaries.
        (0, 21, "low"),
        (5, 21, "low"),  # 5/21 = 0.238 < 0.25
        (6, 21, "moderate"),  # 6/21 = 0.286 >= 0.25
        (15, 21, "moderate"),  # 15/21 = 0.714 < 0.75
        (16, 21, "high"),  # 16/21 = 0.762 >= 0.75
        (21, 21, "high"),  # full window
        # window=7 (one-week look-back).
        (0, 7, "low"),
        (1, 7, "low"),  # 1/7 = 0.143 < 0.25
        (2, 7, "moderate"),  # 2/7 = 0.286 >= 0.25
        (5, 7, "moderate"),  # 5/7 = 0.714 < 0.75
        (6, 7, "high"),  # 6/7 = 0.857 >= 0.75
        (7, 7, "high"),  # full window
    ],
)
def test_high_adopter_position_threshold_boundaries(streak, window_days, expected):
    """AC: parameterised tests at every threshold boundary."""
    assert high_adopter_position(streak, window_days) == expected


def test_high_adopter_position_window_days_zero_raises_before_work():
    """AC: window_days <= 0 raises ValueError before any further work."""
    with pytest.raises(ValueError, match="window_days must be positive"):
        high_adopter_position(0, 0)


def test_high_adopter_position_window_days_negative_raises():
    """AC: any non-positive window_days is rejected."""
    with pytest.raises(ValueError, match="window_days must be positive"):
        high_adopter_position(0, -7)


def test_high_adopter_position_streak_exceeds_window_raises():
    """AC: streak > window_days is an invariant violation."""
    with pytest.raises(ValueError, match="streak .* cannot exceed window_days"):
        high_adopter_position(22, 21)


def test_high_adopter_position_streak_equal_to_window_does_not_raise():
    """Invariant guard is strict-greater-than; streak == window_days is the full-week 'high' case."""
    assert high_adopter_position(21, 21) == "high"


def test_high_adopter_position_window_check_runs_before_streak_check():
    """A non-positive window_days raises even when streak > window_days; the
    window invariant is checked first so callers see a useful error."""
    with pytest.raises(ValueError, match="window_days must be positive"):
        high_adopter_position(5, 0)


def test_high_adopter_position_is_deterministic():
    """AC: deterministic output for fixed inputs (no hidden state, no clock)."""
    # Calling the function twice with the same arguments returns the same value.
    for streak, window_days in [(3, 21), (10, 21), (18, 21), (0, 7), (4, 4)]:
        first = high_adopter_position(streak, window_days)
        second = high_adopter_position(streak, window_days)
        assert first == second


def test_high_adopter_position_return_value_is_in_literal_set():
    """AC: returns an enum-like literal in {'low','moderate','high'}."""
    valid = {"low", "moderate", "high"}
    for streak in range(22):
        assert high_adopter_position(streak, 21) in valid
