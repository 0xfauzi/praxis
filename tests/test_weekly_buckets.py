"""Tests for praxis.behavior.weekly (US-042).

Spec 7.2 acceptance criteria:
  - Sessions in the last 90 days are bucketed by ISO week.
  - Weekly mean is computed for engagement_rate, delegation_rate,
    independence_rate, and the 6 dim scores.
  - Weeks with fewer than 2 sessions are excluded from the fit but
    retained for cost-ledger rendering.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from praxis.behavior.weekly import (
    DEFAULT_WINDOW_DAYS,
    MIN_SESSIONS_FOR_FIT,
    WeeklySessionInput,
    bucket_sessions_by_iso_week,
    iso_week_start,
    iso_week_tag,
)
from praxis.scoring.rubric import RUBRIC


RUBRIC_KEYS = [d.key for d in RUBRIC]


def _full_dim_scores(value: float) -> dict[str, float]:
    return {k: value for k in RUBRIC_KEYS}


def _make_input(
    when: datetime,
    *,
    engagement: float = 0.0,
    delegation: float = 0.0,
    independence: float = 0.0,
    dim_score: float = 5.0,
) -> WeeklySessionInput:
    return WeeklySessionInput(
        started_at=when,
        engagement_rate=engagement,
        delegation_rate=delegation,
        independence_rate=independence,
        dim_scores=_full_dim_scores(dim_score),
    )


def test_empty_input_returns_empty_list():
    assert bucket_sessions_by_iso_week([]) == []


def test_iso_week_tag_format():
    # 2026-05-25 is a Monday; iso week 22 of 2026.
    d = date(2026, 5, 25)
    assert iso_week_tag(d) == "2026-W22"
    # Datetime input also accepted; same week.
    assert iso_week_tag(datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)) == "2026-W22"


def test_iso_week_start_is_monday():
    # Any day in ISO week 22 of 2026 maps to Monday 2026-05-25.
    monday = date(2026, 5, 25)
    for offset in range(7):
        assert iso_week_start(monday + timedelta(days=offset)) == monday


def test_single_week_with_two_sessions_is_eligible():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)  # Wed, ISO 2026-W22
    inputs = [
        _make_input(now - timedelta(days=2), engagement=0.4, delegation=0.2, independence=0.1, dim_score=6.0),
        _make_input(now - timedelta(days=1), engagement=0.6, delegation=0.4, independence=0.3, dim_score=8.0),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 1
    b = buckets[0]
    assert b.iso_week == "2026-W22"
    assert b.session_count == 2
    assert b.eligible_for_fit is True
    assert abs(b.engagement_rate_mean - 0.5) < 1e-9
    assert abs(b.delegation_rate_mean - 0.3) < 1e-9
    assert abs(b.independence_rate_mean - 0.2) < 1e-9
    for key in RUBRIC_KEYS:
        assert abs(b.dim_score_means[key] - 7.0) < 1e-9


def test_singleton_week_kept_but_marked_ineligible():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    inputs = [_make_input(now - timedelta(days=1), engagement=0.5, dim_score=7.0)]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 1
    b = buckets[0]
    assert b.session_count == 1
    assert b.eligible_for_fit is False
    # Mean of a singleton is the value itself.
    assert b.engagement_rate_mean == 0.5
    for key in RUBRIC_KEYS:
        assert b.dim_score_means[key] == 7.0


def test_sessions_outside_90_day_window_are_dropped():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    inputs = [
        # In-window: 30 days ago.
        _make_input(now - timedelta(days=30), engagement=0.2),
        _make_input(now - timedelta(days=30), engagement=0.4),
        # Out-of-window: 100 days ago.
        _make_input(now - timedelta(days=100), engagement=0.9),
        _make_input(now - timedelta(days=100), engagement=0.9),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 1
    assert buckets[0].session_count == 2
    # mean of [0.2, 0.4] = 0.3 (floating-point tolerance).
    assert abs(buckets[0].engagement_rate_mean - 0.3) < 1e-9


def test_window_boundary_is_inclusive_at_cutoff():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    # Exactly 90 days ago is the boundary; treat as in-window.
    on_boundary = now - timedelta(days=DEFAULT_WINDOW_DAYS)
    inputs = [
        _make_input(on_boundary, engagement=0.7),
        _make_input(on_boundary + timedelta(hours=1), engagement=0.7),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 1
    assert buckets[0].session_count == 2


def test_multiple_weeks_are_sorted_oldest_first():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    # Three weeks ending in 2026-W22, 2026-W21, 2026-W20.
    week_22 = now - timedelta(days=1)
    week_21 = now - timedelta(days=8)
    week_20 = now - timedelta(days=15)
    inputs = [
        _make_input(week_22, engagement=0.3),
        _make_input(week_22, engagement=0.5),
        _make_input(week_21, engagement=0.4),
        _make_input(week_21, engagement=0.6),
        _make_input(week_20, engagement=0.1),
        _make_input(week_20, engagement=0.3),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert [b.iso_week for b in buckets] == ["2026-W20", "2026-W21", "2026-W22"]
    # Oldest-first ordering also follows week_start ascending.
    starts = [b.week_start for b in buckets]
    assert starts == sorted(starts)


def test_dim_score_means_are_averaged_per_key():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    inputs = [
        WeeklySessionInput(
            started_at=now - timedelta(days=1),
            engagement_rate=0.0,
            delegation_rate=0.0,
            independence_rate=0.0,
            dim_scores={k: 4.0 for k in RUBRIC_KEYS},
        ),
        WeeklySessionInput(
            started_at=now - timedelta(days=2),
            engagement_rate=0.0,
            delegation_rate=0.0,
            independence_rate=0.0,
            dim_scores={k: 8.0 for k in RUBRIC_KEYS},
        ),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 1
    for key in RUBRIC_KEYS:
        assert buckets[0].dim_score_means[key] == 6.0


def test_naive_datetime_is_treated_as_utc():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    naive_when = (now - timedelta(days=1)).replace(tzinfo=None)
    inputs = [
        _make_input(naive_when, engagement=0.5),
        _make_input(naive_when, engagement=0.7),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 1
    assert buckets[0].session_count == 2


def test_min_sessions_constant_is_two():
    # The acceptance criterion phrasing is "fewer than 2 sessions"; lock it in.
    assert MIN_SESSIONS_FOR_FIT == 2


def test_mixed_eligible_and_ineligible_weeks_all_returned():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    week_22 = now - timedelta(days=1)
    week_21 = now - timedelta(days=8)
    inputs = [
        # W22 has 1 session (ineligible).
        _make_input(week_22, engagement=0.5),
        # W21 has 3 sessions (eligible).
        _make_input(week_21, engagement=0.1),
        _make_input(week_21, engagement=0.2),
        _make_input(week_21, engagement=0.3),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 2
    by_week = {b.iso_week: b for b in buckets}
    assert by_week["2026-W22"].eligible_for_fit is False
    assert by_week["2026-W22"].session_count == 1
    assert by_week["2026-W21"].eligible_for_fit is True
    assert by_week["2026-W21"].session_count == 3
    assert abs(by_week["2026-W21"].engagement_rate_mean - 0.2) < 1e-9


def test_dim_score_means_skip_keys_no_session_provides():
    # If no session in a bucket reports a given key, that key is absent
    # from dim_score_means (rather than invented as 0.0).
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    partial_keys = {RUBRIC_KEYS[0]: 5.0, RUBRIC_KEYS[1]: 7.0}
    inputs = [
        WeeklySessionInput(
            started_at=now - timedelta(days=1),
            engagement_rate=0.0,
            delegation_rate=0.0,
            independence_rate=0.0,
            dim_scores=partial_keys,
        ),
        WeeklySessionInput(
            started_at=now - timedelta(days=2),
            engagement_rate=0.0,
            delegation_rate=0.0,
            independence_rate=0.0,
            dim_scores=partial_keys,
        ),
    ]
    buckets = bucket_sessions_by_iso_week(inputs, now=now)
    assert len(buckets) == 1
    means = buckets[0].dim_score_means
    assert set(means.keys()) == {RUBRIC_KEYS[0], RUBRIC_KEYS[1]}
    assert means[RUBRIC_KEYS[0]] == 5.0
    assert means[RUBRIC_KEYS[1]] == 7.0
