"""Tests for praxis.behavior.slope (US-043).

Acceptance criteria:
  - Slope and standard error are computed via least-squares over weekly means.
  - A slope is `significant` only if |slope| >= 1.5 * stderr AND
    |slope| >= 0.05 per week.

Spec section 7.2 (PRAXIS_V0_2_SPEC.md).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from praxis.behavior import (
    MIN_SESSIONS_FOR_FIT,
    SIGNIFICANCE_MIN_SLOPE_PER_WEEK,
    SIGNIFICANCE_STDERR_MULTIPLIER,
    SlopeFit,
    WeeklyBucket,
    WeeklySessionInput,
    WeeklyTrajectoryFit,
    bucket_sessions_by_iso_week,
    fit_metric,
    fit_weekly_trajectory,
    iso_week_tag,
)
from praxis.behavior.slope import _least_squares
from praxis.scoring.rubric import RUBRIC

RUBRIC_KEYS = [d.key for d in RUBRIC]


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


# ---- _least_squares math ----------------------------------------------------


def test_least_squares_empty_returns_zero_fit():
    fit = _least_squares([])
    assert fit == SlopeFit(slope=0.0, stderr=0.0, n=0)


def test_least_squares_single_point_returns_zero_fit():
    fit = _least_squares([(0.0, 1.0)])
    assert fit == SlopeFit(slope=0.0, stderr=0.0, n=1)


def test_least_squares_two_points_is_exact_with_zero_stderr():
    fit = _least_squares([(0.0, 1.0), (1.0, 3.0)])
    assert fit.n == 2
    assert abs(fit.slope - 2.0) < 1e-12
    assert fit.stderr == 0.0


def test_least_squares_perfect_line_three_points_zero_stderr():
    # y = 1 + 2x
    fit = _least_squares([(0.0, 1.0), (1.0, 3.0), (2.0, 5.0)])
    assert fit.n == 3
    assert abs(fit.slope - 2.0) < 1e-12
    assert fit.stderr == 0.0


def test_least_squares_noisy_three_points_known_values():
    # Points (0,1), (1,3), (2,2). Worked by hand:
    #   slope = 0.5, intercept = 1.5, residuals = [-0.5, 1.0, -0.5]
    #   SSR = 1.5, variance = SSR / (n-2) = 1.5
    #   stderr = sqrt(variance / Sxx) = sqrt(1.5 / 2) = sqrt(0.75)
    fit = _least_squares([(0.0, 1.0), (1.0, 3.0), (2.0, 2.0)])
    assert fit.n == 3
    assert abs(fit.slope - 0.5) < 1e-12
    assert abs(fit.stderr - math.sqrt(0.75)) < 1e-12


def test_least_squares_noisy_four_points_known_values():
    # Points (0,0), (1,2), (2,2), (3,4). Worked by hand:
    #   slope = 1.2, intercept = 0.2, residuals = [-0.2, 0.6, -0.6, 0.2]
    #   SSR = 0.8, variance = 0.8 / (4-2) = 0.4, Sxx = 5
    #   stderr = sqrt(0.4 / 5) = sqrt(0.08)
    fit = _least_squares([(0.0, 0.0), (1.0, 2.0), (2.0, 2.0), (3.0, 4.0)])
    assert fit.n == 4
    assert abs(fit.slope - 1.2) < 1e-12
    assert abs(fit.stderr - math.sqrt(0.08)) < 1e-12


def test_least_squares_degenerate_x_returns_zero_slope():
    # All x equal: no slope defined.
    fit = _least_squares([(1.0, 5.0), (1.0, 9.0), (1.0, 11.0)])
    assert fit.slope == 0.0
    assert fit.stderr == 0.0
    assert fit.n == 3


# ---- SlopeFit.significant --------------------------------------------------


def test_significance_constants_match_spec():
    assert SIGNIFICANCE_STDERR_MULTIPLIER == 1.5
    assert SIGNIFICANCE_MIN_SLOPE_PER_WEEK == 0.05


def test_significant_false_when_n_below_two():
    assert SlopeFit(slope=10.0, stderr=0.0, n=0).significant is False
    assert SlopeFit(slope=10.0, stderr=0.0, n=1).significant is False


def test_significant_false_when_slope_below_floor():
    # |slope| = 0.04 < 0.05/week floor even though stderr is zero.
    assert SlopeFit(slope=0.04, stderr=0.0, n=5).significant is False
    assert SlopeFit(slope=-0.04, stderr=0.0, n=5).significant is False


def test_significant_true_at_floor_with_zero_stderr():
    # Boundary at the 0.05/week floor with perfect fit.
    assert SlopeFit(slope=0.05, stderr=0.0, n=5).significant is True
    assert SlopeFit(slope=-0.05, stderr=0.0, n=5).significant is True


def test_significant_true_at_stderr_boundary():
    # |slope| == 1.5 * stderr AND |slope| >= 0.05. 0.75 and 0.5 are
    # exact in binary float so the comparison hits the boundary cleanly.
    assert 1.5 * 0.5 == 0.75
    assert SlopeFit(slope=0.75, stderr=0.5, n=5).significant is True
    assert SlopeFit(slope=-0.75, stderr=0.5, n=5).significant is True


def test_significant_false_just_below_stderr_boundary():
    # |slope| = 0.7 < 1.5 * 0.5 = 0.75.
    fit = SlopeFit(slope=0.7, stderr=0.5, n=5)
    assert abs(fit.slope) < SIGNIFICANCE_STDERR_MULTIPLIER * fit.stderr
    assert fit.significant is False


def test_significant_false_when_slope_meets_floor_but_is_noisy():
    # |slope| meets the 0.05/week floor but does not exceed 1.5 * stderr.
    fit = SlopeFit(slope=0.05, stderr=0.5, n=5)
    assert fit.significant is False


# ---- fit_metric -------------------------------------------------------------


def test_fit_metric_empty_buckets_returns_zero_fit():
    fit = fit_metric([], lambda b: b.engagement_rate_mean)
    assert fit == SlopeFit(slope=0.0, stderr=0.0, n=0)


def test_fit_metric_skips_ineligible_buckets():
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=i), eng=0.5, count=1)  # all singletons
        for i in range(4)
    ]
    fit = fit_metric(buckets, lambda b: b.engagement_rate_mean)
    # No eligible buckets: nothing to fit.
    assert fit == SlopeFit(slope=0.0, stderr=0.0, n=0)


def test_fit_metric_ignores_ineligible_when_mixed():
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday, eng=0.1, count=2),
        _bucket(monday + timedelta(weeks=1), eng=100.0, count=1),  # outlier, ineligible
        _bucket(monday + timedelta(weeks=2), eng=0.3, count=2),
    ]
    fit = fit_metric(buckets, lambda b: b.engagement_rate_mean)
    assert fit.n == 2
    # Fit is over (x=0, y=0.1) and (x=2, y=0.3): slope (0.3 - 0.1) / 2 = 0.1
    assert abs(fit.slope - 0.1) < 1e-12
    assert fit.stderr == 0.0


def test_fit_metric_x_axis_tracks_calendar_weeks_not_index():
    monday = date(2026, 5, 4)
    # Eligible buckets at week 0 and week 2 (week 1 missing).
    buckets = [
        _bucket(monday, eng=0.10, count=2),
        _bucket(monday + timedelta(weeks=2), eng=0.30, count=2),
    ]
    fit = fit_metric(buckets, lambda b: b.engagement_rate_mean)
    # If x were just bucket index [0, 1], slope would be 0.20/week.
    # x is calendar-week offset [0, 2], so slope is 0.10/week.
    assert abs(fit.slope - 0.10) < 1e-12


def test_fit_metric_positive_trend_gives_positive_slope():
    monday = date(2026, 5, 4)
    buckets = [_bucket(monday + timedelta(weeks=i), eng=0.1 * (i + 1), count=2) for i in range(4)]
    fit = fit_metric(buckets, lambda b: b.engagement_rate_mean)
    assert fit.slope > 0.0
    # Perfect line with step 0.1/week, so stderr should be ~0.
    assert abs(fit.slope - 0.1) < 1e-12
    assert fit.stderr < 1e-12


def test_fit_metric_negative_trend_gives_negative_slope():
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=i), deleg=0.5 - 0.05 * i, count=2) for i in range(4)
    ]
    fit = fit_metric(buckets, lambda b: b.delegation_rate_mean)
    assert fit.slope < 0.0
    assert abs(fit.slope - (-0.05)) < 1e-12


def test_fit_metric_skips_buckets_missing_a_dim_key():
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=0), count=2, dim_scores={"planning": 4.0}),
        _bucket(monday + timedelta(weeks=1), count=2, dim_scores={}),  # missing key
        _bucket(monday + timedelta(weeks=2), count=2, dim_scores={"planning": 6.0}),
    ]
    fit = fit_metric(buckets, lambda b: b.dim_score_means.get("planning"))
    # n is 2 (middle bucket dropped because getter returned None).
    assert fit.n == 2
    # Slope of (0, 4) -> (2, 6) is 1.0/week.
    assert abs(fit.slope - 1.0) < 1e-12


def test_fit_metric_input_order_does_not_change_result():
    monday = date(2026, 5, 4)
    in_order = [_bucket(monday + timedelta(weeks=i), eng=0.1 * i + 0.2, count=2) for i in range(5)]
    shuffled = [in_order[3], in_order[0], in_order[4], in_order[1], in_order[2]]
    a = fit_metric(in_order, lambda b: b.engagement_rate_mean)
    b = fit_metric(shuffled, lambda b: b.engagement_rate_mean)
    assert a == b


def test_fit_metric_significance_gate_blocks_subthreshold_slope():
    # |slope| = 0.04/week is below the 0.05/week floor.
    monday = date(2026, 5, 4)
    buckets = [_bucket(monday + timedelta(weeks=i), eng=0.5 + 0.04 * i, count=2) for i in range(5)]
    fit = fit_metric(buckets, lambda b: b.engagement_rate_mean)
    assert abs(fit.slope - 0.04) < 1e-12
    assert fit.significant is False


def test_fit_metric_significance_gate_blocks_noisy_slope():
    # Construct points where |slope| < 1.5 * stderr even though slope > 0.05.
    monday = date(2026, 5, 4)
    rates = [0.10, 0.80, 0.20, 0.70, 0.30]
    buckets = [_bucket(monday + timedelta(weeks=i), eng=rates[i], count=2) for i in range(5)]
    fit = fit_metric(buckets, lambda b: b.engagement_rate_mean)
    # Noisy enough that the stderr swamps the slope.
    assert abs(fit.slope) < SIGNIFICANCE_STDERR_MULTIPLIER * fit.stderr
    assert fit.significant is False


# ---- fit_weekly_trajectory --------------------------------------------------


def test_fit_weekly_trajectory_empty_returns_zero_fits():
    fit = fit_weekly_trajectory([])
    assert fit.engagement == SlopeFit(slope=0.0, stderr=0.0, n=0)
    assert fit.delegation == SlopeFit(slope=0.0, stderr=0.0, n=0)
    assert fit.independence == SlopeFit(slope=0.0, stderr=0.0, n=0)
    assert fit.dim_scores == {}


def test_fit_weekly_trajectory_independent_axes():
    # Engagement up, delegation down, independence flat.
    monday = date(2026, 5, 4)
    buckets = []
    for i in range(5):
        buckets.append(
            _bucket(
                monday + timedelta(weeks=i),
                eng=0.10 * i,
                deleg=0.50 - 0.10 * i,
                indep=0.30,
                count=2,
            )
        )
    fit = fit_weekly_trajectory(buckets)
    assert abs(fit.engagement.slope - 0.10) < 1e-12
    assert abs(fit.delegation.slope - (-0.10)) < 1e-12
    assert abs(fit.independence.slope) < 1e-12
    assert fit.engagement.significant is True
    assert fit.delegation.significant is True
    assert fit.independence.significant is False


def test_fit_weekly_trajectory_dim_scores_one_fit_per_key():
    monday = date(2026, 5, 4)
    # Each bucket reports planning rising and verification falling.
    buckets = [
        _bucket(
            monday + timedelta(weeks=i),
            count=2,
            dim_scores={"planning": 5.0 + 0.5 * i, "verification": 7.0 - 0.5 * i},
        )
        for i in range(4)
    ]
    fit = fit_weekly_trajectory(buckets)
    assert set(fit.dim_scores.keys()) == {"planning", "verification"}
    assert abs(fit.dim_scores["planning"].slope - 0.5) < 1e-12
    assert abs(fit.dim_scores["verification"].slope - (-0.5)) < 1e-12


def test_fit_weekly_trajectory_dim_keys_union_across_eligible_buckets():
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(
            monday + timedelta(weeks=0),
            count=2,
            dim_scores={"planning": 5.0},
        ),
        _bucket(
            monday + timedelta(weeks=1),
            count=2,
            dim_scores={"context": 6.0},
        ),
        _bucket(
            monday + timedelta(weeks=2),
            count=2,
            dim_scores={"planning": 7.0, "context": 8.0},
        ),
    ]
    fit = fit_weekly_trajectory(buckets)
    # Union of keys across eligible buckets.
    assert set(fit.dim_scores.keys()) == {"planning", "context"}
    # Each per-dim fit uses only the buckets reporting that dim.
    assert fit.dim_scores["planning"].n == 2
    assert fit.dim_scores["context"].n == 2


def test_fit_weekly_trajectory_via_bucket_pipeline_end_to_end():
    # Build sessions, bucket them, then fit. Locks in that
    # WeeklyBucket -> WeeklyTrajectoryFit composes without glue.
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    sessions: list[WeeklySessionInput] = []
    # Four weeks: engagement rises 0.10/week.
    for week_index in range(4):
        week_anchor = now - timedelta(days=21 - 7 * week_index)
        for offset in (0, 1):
            sessions.append(
                WeeklySessionInput(
                    started_at=week_anchor - timedelta(hours=offset),
                    engagement_rate=0.20 + 0.10 * week_index,
                    delegation_rate=0.10,
                    independence_rate=0.05,
                    dim_scores={k: 5.0 for k in RUBRIC_KEYS},
                )
            )
    buckets = bucket_sessions_by_iso_week(sessions, now=now)
    fit = fit_weekly_trajectory(buckets)
    assert isinstance(fit, WeeklyTrajectoryFit)
    assert fit.engagement.n == 4
    assert abs(fit.engagement.slope - 0.10) < 1e-9
    assert fit.engagement.stderr < 1e-9
    assert fit.engagement.significant is True
    # All dim_scores are flat at 5.0, so slope ~ 0 and not significant.
    for k in RUBRIC_KEYS:
        assert k in fit.dim_scores
        assert abs(fit.dim_scores[k].slope) < 1e-9
        assert fit.dim_scores[k].significant is False
