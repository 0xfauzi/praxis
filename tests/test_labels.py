"""Tests for praxis.behavior.labels (US-044).

Acceptance criteria (PRD US-044, spec section 7.2):
  - Labels Learning, Growing autonomy, Steady, Drifting, Atrophying, Reading
    are assigned exactly according to the table in Section 7.2.
  - Reading is returned when fewer than 4 weekly buckets have data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from praxis.behavior import (
    MIN_BUCKETS_FOR_LABEL,
    MIN_SESSIONS_FOR_FIT,
    SlopeFit,
    WeeklyBucket,
    WeeklySessionInput,
    WeeklyTrajectoryFit,
    WeeklyTrajectoryLabel,
    bucket_sessions_by_iso_week,
    iso_week_tag,
    label_from_fit,
    label_trajectory,
)
from praxis.scoring.rubric import RUBRIC


RUBRIC_KEYS = [d.key for d in RUBRIC]


# ---- helpers ----------------------------------------------------------------


def _bucket(
    week_start: date,
    *,
    eng: float = 0.0,
    deleg: float = 0.0,
    indep: float = 0.0,
    dim_scores: dict[str, float] | None = None,
    count: int = 2,
) -> WeeklyBucket:
    return WeeklyBucket(
        iso_week=iso_week_tag(week_start),
        week_start=week_start,
        session_count=count,
        engagement_rate_mean=eng,
        delegation_rate_mean=deleg,
        independence_rate_mean=indep,
        dim_score_means=dict(dim_scores or {}),
        eligible_for_fit=count >= MIN_SESSIONS_FOR_FIT,
    )


def _fit(
    *,
    eng: SlopeFit | None = None,
    deleg: SlopeFit | None = None,
    indep: SlopeFit | None = None,
) -> WeeklyTrajectoryFit:
    """Build a WeeklyTrajectoryFit from per-axis SlopeFits.

    Defaults to a "flat at n=4" fit on each axis (significant=False), which
    is the table's 'Steady' row when used unchanged.
    """
    flat = SlopeFit(slope=0.0, stderr=0.0, n=4)
    return WeeklyTrajectoryFit(
        engagement=eng or flat,
        delegation=deleg or flat,
        independence=indep or flat,
        dim_scores={},
    )


# Significant slopes use exact-in-float values so the 1.5*stderr boundary
# is unambiguous (see test_slope.py for the rationale).
_UP = SlopeFit(slope=0.75, stderr=0.5, n=4)        # significant up
_DOWN = SlopeFit(slope=-0.75, stderr=0.5, n=4)     # significant down
_FLAT = SlopeFit(slope=0.0, stderr=0.0, n=4)       # not significant (flat)
_FLAT_NOISY = SlopeFit(slope=0.10, stderr=0.5, n=4)  # |slope| < 1.5*stderr -> flat


# ---- constants --------------------------------------------------------------


def test_min_buckets_for_label_matches_spec():
    # Spec 7.2: "Fewer than 4 weekly buckets with data" -> Reading.
    assert MIN_BUCKETS_FOR_LABEL == 4


def test_label_value_strings_match_spec_display_strings():
    # The .value of each member is the spec's display string, so callers can
    # render the label directly without a separate display-name table.
    assert WeeklyTrajectoryLabel.LEARNING.value == "Learning"
    assert WeeklyTrajectoryLabel.GROWING_AUTONOMY.value == "Growing autonomy"
    assert WeeklyTrajectoryLabel.STEADY.value == "Steady"
    assert WeeklyTrajectoryLabel.DRIFTING.value == "Drifting"
    assert WeeklyTrajectoryLabel.ATROPHYING.value == "Atrophying"
    assert WeeklyTrajectoryLabel.READING.value == "Reading"


# ---- Reading gate -----------------------------------------------------------


def test_label_from_fit_returns_reading_for_zero_eligible():
    assert label_from_fit(0, _fit()) is WeeklyTrajectoryLabel.READING


def test_label_from_fit_returns_reading_for_three_eligible():
    # Below the 4-bucket threshold even with a very clear direction.
    assert label_from_fit(3, _fit(eng=_UP, deleg=_DOWN)) is WeeklyTrajectoryLabel.READING


def test_label_from_fit_reading_boundary_at_four_eligible_buckets():
    # 4 is the inclusive boundary: a 4-bucket window is no longer Reading.
    label = label_from_fit(4, _fit(eng=_UP, deleg=_DOWN))
    assert label is WeeklyTrajectoryLabel.LEARNING


# ---- spec 7.2 table - one test per row -------------------------------------


def test_learning_when_engagement_up_and_delegation_down():
    label = label_from_fit(5, _fit(eng=_UP, deleg=_DOWN))
    assert label is WeeklyTrajectoryLabel.LEARNING


def test_growing_autonomy_when_engagement_flat_and_delegation_down():
    label = label_from_fit(5, _fit(eng=_FLAT, deleg=_DOWN))
    assert label is WeeklyTrajectoryLabel.GROWING_AUTONOMY


def test_steady_when_both_flat():
    label = label_from_fit(5, _fit(eng=_FLAT, deleg=_FLAT))
    assert label is WeeklyTrajectoryLabel.STEADY


def test_drifting_when_engagement_flat_and_delegation_up():
    label = label_from_fit(5, _fit(eng=_FLAT, deleg=_UP))
    assert label is WeeklyTrajectoryLabel.DRIFTING


def test_atrophying_when_engagement_down_and_delegation_up():
    label = label_from_fit(5, _fit(eng=_DOWN, deleg=_UP))
    assert label is WeeklyTrajectoryLabel.ATROPHYING


def test_atrophying_beats_drifting_for_down_up_subset():
    # The Drifting row's condition is "engagement flat OR down, delegation up".
    # (down, up) qualifies for both rows; spec lists Atrophying as a separate
    # row, so the more specific label must win.
    label = label_from_fit(5, _fit(eng=_DOWN, deleg=_UP))
    assert label is WeeklyTrajectoryLabel.ATROPHYING
    assert label is not WeeklyTrajectoryLabel.DRIFTING


# ---- 'flat' semantics: not significant counts as flat -----------------------


def test_noisy_slope_counts_as_flat_for_label_assignment():
    # _FLAT_NOISY has a non-zero slope but is not significant (stderr swamps
    # it), so per spec 7.2 the trajectory is "flat / noisy, regardless of
    # sign". (noisy, down) should match the GROWING_AUTONOMY row.
    label = label_from_fit(5, _fit(eng=_FLAT_NOISY, deleg=_DOWN))
    assert label is WeeklyTrajectoryLabel.GROWING_AUTONOMY


def test_noisy_engagement_with_significant_delegation_up_is_drifting():
    label = label_from_fit(5, _fit(eng=_FLAT_NOISY, deleg=_UP))
    assert label is WeeklyTrajectoryLabel.DRIFTING


# ---- rows NOT in the spec table fall back to Steady -------------------------


def test_unspecified_up_up_falls_back_to_steady():
    label = label_from_fit(5, _fit(eng=_UP, deleg=_UP))
    assert label is WeeklyTrajectoryLabel.STEADY


def test_unspecified_up_flat_falls_back_to_steady():
    label = label_from_fit(5, _fit(eng=_UP, deleg=_FLAT))
    assert label is WeeklyTrajectoryLabel.STEADY


def test_unspecified_down_flat_falls_back_to_steady():
    label = label_from_fit(5, _fit(eng=_DOWN, deleg=_FLAT))
    assert label is WeeklyTrajectoryLabel.STEADY


def test_unspecified_down_down_falls_back_to_steady():
    label = label_from_fit(5, _fit(eng=_DOWN, deleg=_DOWN))
    assert label is WeeklyTrajectoryLabel.STEADY


# ---- label_trajectory (bucket-level wrapper) --------------------------------


def test_label_trajectory_empty_returns_reading():
    assert label_trajectory([]) is WeeklyTrajectoryLabel.READING


def test_label_trajectory_short_circuits_below_threshold():
    # Three eligible buckets with a clear up-down pattern -> still Reading.
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=i), eng=0.10 * i, deleg=0.50 - 0.10 * i, count=2)
        for i in range(3)
    ]
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.READING


def test_label_trajectory_ineligible_buckets_do_not_count_for_reading_gate():
    # Three eligible buckets + one singleton (ineligible) = 3 eligible -> Reading.
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=0), eng=0.20, deleg=0.40, count=2),
        _bucket(monday + timedelta(weeks=1), eng=0.30, deleg=0.30, count=2),
        _bucket(monday + timedelta(weeks=2), eng=0.40, deleg=0.20, count=2),
        _bucket(monday + timedelta(weeks=3), eng=0.50, deleg=0.10, count=1),  # ineligible
    ]
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.READING


def test_label_trajectory_learning_end_to_end():
    monday = date(2026, 5, 4)
    # Engagement rising 0.10/wk, delegation falling 0.10/wk - both significant.
    buckets = [
        _bucket(
            monday + timedelta(weeks=i),
            eng=0.10 + 0.10 * i,
            deleg=0.60 - 0.10 * i,
            count=2,
        )
        for i in range(5)
    ]
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.LEARNING


def test_label_trajectory_atrophying_end_to_end():
    monday = date(2026, 5, 4)
    # Engagement falling, delegation rising - both significant.
    buckets = [
        _bucket(
            monday + timedelta(weeks=i),
            eng=0.60 - 0.10 * i,
            deleg=0.10 + 0.10 * i,
            count=2,
        )
        for i in range(5)
    ]
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.ATROPHYING


def test_label_trajectory_steady_end_to_end():
    monday = date(2026, 5, 4)
    # Flat on both axes - 5 buckets all identical.
    buckets = [
        _bucket(monday + timedelta(weeks=i), eng=0.30, deleg=0.30, count=2)
        for i in range(5)
    ]
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.STEADY


def test_label_trajectory_drifting_end_to_end():
    monday = date(2026, 5, 4)
    # Engagement flat, delegation rising significantly.
    buckets = [
        _bucket(
            monday + timedelta(weeks=i),
            eng=0.30,
            deleg=0.10 + 0.10 * i,
            count=2,
        )
        for i in range(5)
    ]
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.DRIFTING


def test_label_trajectory_growing_autonomy_end_to_end():
    monday = date(2026, 5, 4)
    # Engagement flat, delegation falling significantly.
    buckets = [
        _bucket(
            monday + timedelta(weeks=i),
            eng=0.30,
            deleg=0.60 - 0.10 * i,
            count=2,
        )
        for i in range(5)
    ]
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.GROWING_AUTONOMY


def test_label_trajectory_via_bucket_pipeline_end_to_end():
    # Builds sessions, buckets them, then labels - confirms the v0.2 stack
    # (US-042 -> US-043 -> US-044) composes without glue.
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    sessions: list[WeeklySessionInput] = []
    for week_index in range(5):
        week_anchor = now - timedelta(days=28 - 7 * week_index)
        for offset in (0, 1):
            sessions.append(
                WeeklySessionInput(
                    started_at=week_anchor - timedelta(hours=offset),
                    # Engagement rising 0.10/wk, delegation falling 0.10/wk.
                    engagement_rate=0.10 + 0.10 * week_index,
                    delegation_rate=0.50 - 0.10 * week_index,
                    independence_rate=0.05,
                    dim_scores={k: 5.0 for k in RUBRIC_KEYS},
                )
            )
    buckets = bucket_sessions_by_iso_week(sessions, now=now)
    assert sum(1 for b in buckets if b.eligible_for_fit) == 5
    assert label_trajectory(buckets) is WeeklyTrajectoryLabel.LEARNING
