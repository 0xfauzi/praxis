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
    MIN_DAYS_FOR_BASELINE,
    OVERALL_CLIP_HIGH,
    OVERALL_CLIP_LOW,
    _iso_week_start,
    compute_baseline,
    data_span_days,
    has_prior_week_sessions,
    is_baseline_forming,
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


# ---------------------------------------------------------------------- US-035

def test_min_days_for_baseline_matches_spec():
    """Spec section 8.4: under 14 days of data, baseline is 'forming'."""
    assert MIN_DAYS_FOR_BASELINE == 14


def test_data_span_days_empty_is_zero():
    assert data_span_days([], as_of=AS_OF) == 0


def test_data_span_days_today_only_is_zero():
    """A single session today is 0 days of data (today is day 0)."""
    s = _session(days_ago=0)
    assert data_span_days([s], as_of=AS_OF) == 0


def test_data_span_days_yesterday_only_is_one():
    s = _session(days_ago=1)
    assert data_span_days([s], as_of=AS_OF) == 1


def test_data_span_days_uses_earliest_session():
    """The span is from the EARLIEST session, not the count of sessions."""
    a = _session(days_ago=20)
    b = _session(days_ago=2)
    c = _session(days_ago=10)
    assert data_span_days([a, b, c], as_of=AS_OF) == 20


def test_data_span_days_includes_sessions_outside_baseline_window():
    """Sessions older than 90 days still count toward the calendar span.

    A user who used the tool once 200 days ago and is back this week
    has 200 days of data, even though the 200-day-old session does not
    contribute to the 90-day mean. The 'baseline forming' signal is
    about how long the user has been around, not about how many sessions
    fall in the 90-day window.
    """
    ancient = _session(days_ago=200)
    recent = _session(days_ago=5)
    assert data_span_days([ancient, recent], as_of=AS_OF) == 200


def test_is_baseline_forming_under_14_days():
    """13 days of data: still forming."""
    sessions = [_session(days_ago=0), _session(days_ago=13)]
    assert is_baseline_forming(sessions, as_of=AS_OF) is True


def test_is_baseline_forming_at_14_days_is_not_forming():
    """Exactly 14 days of data: boundary is inclusive (NOT forming).

    The spec gate is '< 14 days'. A user whose earliest session was
    14 days ago has 14 days of data, which is not less than 14, so
    the baseline is considered formed.
    """
    sessions = [_session(days_ago=14)]
    assert is_baseline_forming(sessions, as_of=AS_OF) is False


def test_is_baseline_forming_over_14_days_is_not_forming():
    sessions = [_session(days_ago=30)]
    assert is_baseline_forming(sessions, as_of=AS_OF) is False


def test_is_baseline_forming_empty_input_is_forming():
    """No data at all: definitely forming."""
    assert is_baseline_forming([], as_of=AS_OF) is True


def test_has_prior_week_sessions_empty_is_false():
    assert has_prior_week_sessions([], as_of=AS_OF) is False


def test_has_prior_week_sessions_only_current_week_is_false():
    """A user whose entire history is in the current ISO week has no prior week."""
    # AS_OF is Wednesday 2026-05-27. Monday of that week is 2026-05-25.
    # All these sessions are >= Monday, so all are in the current week.
    sessions = [_session(days_ago=0), _session(days_ago=1), _session(days_ago=2)]
    assert has_prior_week_sessions(sessions, as_of=AS_OF) is False


def test_has_prior_week_sessions_last_week_is_true():
    """A session in the immediately prior ISO week counts as 'prior week'."""
    # days_ago=3 from Wed 2026-05-27 -> Sun 2026-05-24 (prior week).
    sessions = [_session(days_ago=3)]
    assert has_prior_week_sessions(sessions, as_of=AS_OF) is True


def test_has_prior_week_sessions_far_prior_week_is_true():
    """Any session before the current ISO week's Monday qualifies."""
    sessions = [_session(days_ago=60)]
    assert has_prior_week_sessions(sessions, as_of=AS_OF) is True


def test_forming_and_prior_week_can_both_be_true():
    """The 'last-week mean still renders if there is at least one prior week of data' case.

    A user who started 8 days ago has <14 days of data (forming=True)
    but also has at least one prior-week session (Monday 2 days before
    AS_OF is the boundary; days_ago=3 lands on Sunday 2026-05-24, which
    is in last week). Renderers must therefore still show last-week
    mean even though the baseline reads '--'.
    """
    # AS_OF is Wed 2026-05-27. days_ago=3 -> Sun 2026-05-24 (last week, prior).
    sessions = [_session(days_ago=0), _session(days_ago=3)]
    assert is_baseline_forming(sessions, as_of=AS_OF) is True
    assert has_prior_week_sessions(sessions, as_of=AS_OF) is True
