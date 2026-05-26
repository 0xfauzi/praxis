"""Tests for the 90-day rolling baseline.

Acceptance criteria (US-033, spec sections 8.1 / 8.4):
  - Baseline is the mean of session scores over the last 90 days
    excluding the current ISO week.
  - Baseline is computed for each of the 6 rubric dims and for
    engagement / delegation / independence rates.
  - Sessions are weighted equally regardless of length.
  - Each session's `overall` is clipped to [1.0, 9.0] before contributing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from praxis.scoring.baseline import (
    BASELINE_WINDOW_DAYS,
    Baseline,
    BaselineInputSession,
    OVERALL_CLIP_HIGH,
    OVERALL_CLIP_LOW,
    _iso_week_start,
    compute_baseline,
)
from praxis.scoring.rubric import RUBRIC


# A Wednesday so the current-week start is unambiguous (Monday 2 days prior).
AS_OF = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
CURRENT_WEEK_MONDAY = datetime(2026, 5, 25, tzinfo=timezone.utc).date()


def _all_dims(value: float) -> dict[str, float]:
    return {d.key: value for d in RUBRIC}


def _session(
    days_ago: int,
    overall: float = 6.0,
    dim_value: float = 6.0,
    engagement_rate: float = 0.2,
    delegation_rate: float = 0.4,
    independence_rate: float = 0.1,
) -> BaselineInputSession:
    return BaselineInputSession(
        started_at=AS_OF - timedelta(days=days_ago),
        overall=overall,
        dimension_scores=_all_dims(dim_value),
        engagement_rate=engagement_rate,
        delegation_rate=delegation_rate,
        independence_rate=independence_rate,
    )


def test_empty_session_list_returns_zero_baseline():
    baseline = compute_baseline([], as_of=AS_OF)
    assert baseline.session_count == 0
    assert baseline.overall_mean == 0.0
    assert baseline.engagement_mean == 0.0
    assert baseline.delegation_mean == 0.0
    assert baseline.independence_mean == 0.0
    assert set(baseline.dimension_means.keys()) == {d.key for d in RUBRIC}
    assert all(v == 0.0 for v in baseline.dimension_means.values())


def test_window_bounds_match_spec():
    """Window is [as_of_date - 90 days, current ISO week start)."""
    baseline = compute_baseline([], as_of=AS_OF)
    assert baseline.window_end == CURRENT_WEEK_MONDAY
    assert baseline.window_start == AS_OF.date() - timedelta(days=BASELINE_WINDOW_DAYS)


def test_iso_week_start_finds_monday():
    # 2026-05-27 is a Wednesday; its ISO week start is 2026-05-25 (Mon).
    assert _iso_week_start(AS_OF.date()) == CURRENT_WEEK_MONDAY
    # The Monday itself maps to itself.
    assert _iso_week_start(CURRENT_WEEK_MONDAY) == CURRENT_WEEK_MONDAY
    # Sunday should map back to the prior Monday.
    sunday = CURRENT_WEEK_MONDAY + timedelta(days=6)
    assert _iso_week_start(sunday) == CURRENT_WEEK_MONDAY


def test_sessions_in_current_iso_week_are_excluded():
    """Sessions on/after Monday of as_of's week must not contribute."""
    in_window = _session(days_ago=10, overall=6.0, dim_value=6.0)
    in_current_week = _session(days_ago=1, overall=2.0, dim_value=2.0)  # Tuesday
    on_monday = _session(days_ago=2, overall=2.0, dim_value=2.0)        # Monday boundary
    baseline = compute_baseline([in_window, in_current_week, on_monday], as_of=AS_OF)
    assert baseline.session_count == 1
    assert baseline.overall_mean == 6.0
    assert baseline.dimension_means["planning"] == 6.0


def test_sessions_older_than_90_days_are_excluded():
    too_old = _session(days_ago=95, overall=2.0, dim_value=2.0)
    in_window = _session(days_ago=80, overall=6.0, dim_value=6.0)
    baseline = compute_baseline([too_old, in_window], as_of=AS_OF)
    assert baseline.session_count == 1
    assert baseline.overall_mean == 6.0


def test_window_start_is_inclusive():
    """A session exactly 90 days before as_of is included."""
    edge = _session(days_ago=BASELINE_WINDOW_DAYS, overall=7.0, dim_value=7.0)
    baseline = compute_baseline([edge], as_of=AS_OF)
    assert baseline.session_count == 1
    assert baseline.overall_mean == 7.0


def test_overall_is_clipped_to_one_nine():
    """Outlier highs and lows are clipped before averaging."""
    high_outlier = _session(days_ago=10, overall=15.0, dim_value=6.0)
    low_outlier = _session(days_ago=20, overall=-5.0, dim_value=6.0)
    in_band = _session(days_ago=30, overall=5.0, dim_value=6.0)
    baseline = compute_baseline([high_outlier, low_outlier, in_band], as_of=AS_OF)
    # Clipped contributions: 9.0, 1.0, 5.0 -> mean 5.0
    assert baseline.overall_mean == 5.0
    # Sanity: the constants are what the spec says.
    assert OVERALL_CLIP_LOW == 1.0
    assert OVERALL_CLIP_HIGH == 9.0


def test_dimension_scores_are_NOT_clipped():
    """Spec 8.4 clips `overall` only; dims average as-is."""
    s = BaselineInputSession(
        started_at=AS_OF - timedelta(days=10),
        overall=6.0,
        dimension_scores={"planning": 9.5, "context": 9.5, "iteration": 9.5,
                          "tools": 9.5, "fit": 9.5, "verification": 9.5},
        engagement_rate=0.0,
        delegation_rate=0.0,
        independence_rate=0.0,
    )
    baseline = compute_baseline([s], as_of=AS_OF)
    assert baseline.dimension_means["planning"] == 9.5


def test_all_six_dims_are_averaged():
    a = _session(days_ago=10, overall=6.0)
    a = BaselineInputSession(
        started_at=a.started_at,
        overall=a.overall,
        dimension_scores={"planning": 4.0, "context": 5.0, "iteration": 6.0,
                          "tools": 7.0, "fit": 8.0, "verification": 9.0},
        engagement_rate=0.0,
        delegation_rate=0.0,
        independence_rate=0.0,
    )
    b = BaselineInputSession(
        started_at=AS_OF - timedelta(days=20),
        overall=6.0,
        dimension_scores={"planning": 6.0, "context": 7.0, "iteration": 8.0,
                          "tools": 9.0, "fit": 4.0, "verification": 5.0},
        engagement_rate=0.0,
        delegation_rate=0.0,
        independence_rate=0.0,
    )
    baseline = compute_baseline([a, b], as_of=AS_OF)
    assert baseline.dimension_means == {
        "planning": 5.0,
        "context": 6.0,
        "iteration": 7.0,
        "tools": 8.0,
        "fit": 6.0,
        "verification": 7.0,
    }


def test_engagement_delegation_independence_rates_are_averaged():
    a = _session(days_ago=10, engagement_rate=0.2, delegation_rate=0.6, independence_rate=0.1)
    b = _session(days_ago=20, engagement_rate=0.4, delegation_rate=0.2, independence_rate=0.3)
    baseline = compute_baseline([a, b], as_of=AS_OF)
    assert baseline.engagement_mean == 0.3
    assert baseline.delegation_mean == 0.4
    assert baseline.independence_mean == 0.2


def test_sessions_weighted_equally_regardless_of_length():
    """The function does not see length, so it cannot weight by it.

    This is the test that locks in the design: BaselineInputSession
    has no length field, so two sessions contribute exactly equally
    even if one was a 50-turn marathon and the other was 2 turns.
    """
    fields = set(BaselineInputSession.__dataclass_fields__.keys())
    length_like = {"turn_count", "user_turn_count", "length", "duration", "weight"}
    assert fields.isdisjoint(length_like)

    short_session = _session(days_ago=10, overall=4.0)
    long_session = _session(days_ago=20, overall=8.0)
    baseline = compute_baseline([short_session, long_session], as_of=AS_OF)
    assert baseline.overall_mean == 6.0


def test_default_as_of_uses_now():
    """Smoke test: when as_of is None, window is anchored to today."""
    baseline = compute_baseline([])
    today_iso_week_start = _iso_week_start(datetime.now(timezone.utc).date())
    assert baseline.window_end == today_iso_week_start


def test_baseline_is_immutable():
    """Frozen dataclass guards against accidental mutation downstream."""
    baseline = compute_baseline([], as_of=AS_OF)
    assert isinstance(baseline, Baseline)
    try:
        baseline.overall_mean = 99.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Baseline should be frozen")
