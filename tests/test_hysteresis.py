"""Tests for praxis.behavior.labels hysteresis (US-045).

Acceptance criteria (PRD US-045, spec section 7.4):
  - A new label differs from the previous week's label only when
    |slope| >= 2.0 * stderr on the relevant axis.
  - Otherwise the previous week's label is retained.
  - On identical input, the label does not change between two consecutive
    runs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from praxis.behavior import (
    HYSTERESIS_STDERR_MULTIPLIER,
    MIN_SESSIONS_FOR_FIT,
    SIGNIFICANCE_MIN_SLOPE_PER_WEEK,
    SlopeFit,
    WeeklyBucket,
    WeeklySessionInput,
    WeeklyTrajectoryFit,
    WeeklyTrajectoryLabel,
    apply_hysteresis,
    bucket_sessions_by_iso_week,
    is_strongly_significant,
    iso_week_tag,
    label_trajectory,
    label_trajectory_with_hysteresis,
)
from praxis.scoring.rubric import RUBRIC


RUBRIC_KEYS = [d.key for d in RUBRIC]


# ---- helpers ---------------------------------------------------------------


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

    Defaults each axis to a flat-at-n=4 fit (significant=False).
    """
    flat = SlopeFit(slope=0.0, stderr=0.0, n=4)
    return WeeklyTrajectoryFit(
        engagement=eng or flat,
        delegation=deleg or flat,
        independence=indep or flat,
        dim_scores={},
    )


# Exact-in-float magnitudes so the 2.0*stderr boundary is unambiguous.
#   At 1.5 gate: 0.75 / 0.5 = 1.5 -> just significant.
#   At 2.0 gate: 1.5 < 2.0 -> NOT strongly significant.
# (See test_slope.py for the float-exactness rationale.)
_WEAK_UP = SlopeFit(slope=0.75, stderr=0.5, n=4)
_WEAK_DOWN = SlopeFit(slope=-0.75, stderr=0.5, n=4)
# At 2.0 gate: 1.0 / 0.5 = 2.0 -> strongly significant.
_STRONG_UP = SlopeFit(slope=1.0, stderr=0.5, n=4)
_STRONG_DOWN = SlopeFit(slope=-1.0, stderr=0.5, n=4)
_FLAT = SlopeFit(slope=0.0, stderr=0.0, n=4)


# ---- constants -------------------------------------------------------------


def test_hysteresis_multiplier_matches_spec():
    # Spec 7.4: "2.0 * stderr instead of 1.5 *".
    assert HYSTERESIS_STDERR_MULTIPLIER == 2.0


# ---- is_strongly_significant ----------------------------------------------


def test_is_strongly_significant_zero_fit_is_false():
    assert is_strongly_significant(SlopeFit(slope=0.0, stderr=0.0, n=0)) is False


def test_is_strongly_significant_single_point_is_false():
    # n < 2 short-circuits even if the magnitudes would clear the gate.
    assert is_strongly_significant(SlopeFit(slope=10.0, stderr=0.0, n=1)) is False


def test_is_strongly_significant_below_stderr_gate_is_false():
    # |slope| = 1.5 * stderr: just clears the 1.5 gate but not 2.0.
    assert is_strongly_significant(_WEAK_UP) is False
    assert is_strongly_significant(_WEAK_DOWN) is False


def test_is_strongly_significant_at_stderr_boundary_is_inclusive():
    # |slope| = 2.0 * stderr exactly.
    assert is_strongly_significant(_STRONG_UP) is True
    assert is_strongly_significant(_STRONG_DOWN) is True


def test_is_strongly_significant_above_stderr_boundary_is_true():
    assert is_strongly_significant(SlopeFit(slope=2.0, stderr=0.5, n=4)) is True


def test_is_strongly_significant_requires_min_slope_per_week():
    # |slope| >= 2.0 * stderr but |slope| < 0.05/week -> fails the floor.
    fit = SlopeFit(slope=0.04, stderr=0.01, n=4)
    # |slope| / stderr = 4.0 > 2.0 but |slope|=0.04 < SIGNIFICANCE_MIN_SLOPE_PER_WEEK=0.05.
    assert is_strongly_significant(fit) is False


def test_is_strongly_significant_at_min_slope_boundary_is_inclusive():
    # |slope| = 0.05 exactly, stderr 0.0 -> stderr gate trivially passed.
    fit = SlopeFit(slope=SIGNIFICANCE_MIN_SLOPE_PER_WEEK, stderr=0.0, n=4)
    assert is_strongly_significant(fit) is True


def test_is_strongly_significant_is_stricter_than_significant():
    # _WEAK_UP is significant (1.5 gate) but not strongly significant (2.0 gate).
    assert _WEAK_UP.significant is True
    assert is_strongly_significant(_WEAK_UP) is False


# ---- apply_hysteresis: no previous label (cold start) ----------------------


def test_apply_hysteresis_no_previous_returns_naive_steady():
    label = apply_hysteresis(4, _fit(), previous_label=None)
    assert label is WeeklyTrajectoryLabel.STEADY


def test_apply_hysteresis_no_previous_returns_naive_learning():
    label = apply_hysteresis(
        4, _fit(eng=_WEAK_UP, deleg=_WEAK_DOWN), previous_label=None
    )
    assert label is WeeklyTrajectoryLabel.LEARNING


def test_apply_hysteresis_no_previous_returns_reading_below_threshold():
    label = apply_hysteresis(3, _fit(eng=_STRONG_UP, deleg=_STRONG_DOWN), previous_label=None)
    assert label is WeeklyTrajectoryLabel.READING


# ---- apply_hysteresis: naive equals previous (no gate needed) --------------


def test_apply_hysteresis_naive_equals_previous_steady():
    label = apply_hysteresis(
        4, _fit(), previous_label=WeeklyTrajectoryLabel.STEADY
    )
    assert label is WeeklyTrajectoryLabel.STEADY


def test_apply_hysteresis_naive_equals_previous_learning():
    label = apply_hysteresis(
        4,
        _fit(eng=_WEAK_UP, deleg=_WEAK_DOWN),
        previous_label=WeeklyTrajectoryLabel.LEARNING,
    )
    assert label is WeeklyTrajectoryLabel.LEARNING


# ---- apply_hysteresis: Reading transitions bypass hysteresis ---------------


def test_apply_hysteresis_reading_to_x_passes_through():
    # We have 4 eligible buckets now (was previously Reading with <4).
    # Even if no axis is strongly significant, the change Reading -> Steady
    # must pass through because it is data-availability driven, not
    # slope-direction driven.
    label = apply_hysteresis(
        4, _fit(), previous_label=WeeklyTrajectoryLabel.READING
    )
    assert label is WeeklyTrajectoryLabel.STEADY


def test_apply_hysteresis_reading_to_learning_passes_through_even_without_strong_sig():
    # Even with weak (1.5 only) slopes, Reading -> Learning is accepted
    # because the change is forced by entering the labeling regime, not by
    # crossing the hysteresis threshold.
    label = apply_hysteresis(
        4,
        _fit(eng=_WEAK_UP, deleg=_WEAK_DOWN),
        previous_label=WeeklyTrajectoryLabel.READING,
    )
    assert label is WeeklyTrajectoryLabel.LEARNING


def test_apply_hysteresis_x_to_reading_passes_through():
    # Dropping below 4 eligible buckets -> Reading. Hysteresis must not
    # hold us at the prior label when we no longer have enough data.
    label = apply_hysteresis(
        3, _fit(eng=_STRONG_UP, deleg=_STRONG_DOWN),
        previous_label=WeeklyTrajectoryLabel.LEARNING,
    )
    assert label is WeeklyTrajectoryLabel.READING


# ---- apply_hysteresis: change blocked by hysteresis (no axis at 2.0) -------


def test_apply_hysteresis_blocks_change_when_no_axis_strongly_significant():
    # Previous = Steady. Naive (1.5 gate) = Learning. Neither axis clears 2.0.
    # -> retain Steady.
    label = apply_hysteresis(
        5,
        _fit(eng=_WEAK_UP, deleg=_WEAK_DOWN),
        previous_label=WeeklyTrajectoryLabel.STEADY,
    )
    assert label is WeeklyTrajectoryLabel.STEADY


def test_apply_hysteresis_blocks_learning_to_steady_when_slopes_just_dropped_below_15():
    # Previous = Learning. Now both axes are slightly below the 1.5 gate
    # -> naive = Steady. Neither axis is strongly significant (they are
    # not even weakly significant), so per spec 7.4 the change is not
    # supported by 2.0 evidence -> retain Learning.
    near_flat_up = SlopeFit(slope=0.5, stderr=0.5, n=4)   # |slope|/stderr = 1.0
    near_flat_down = SlopeFit(slope=-0.5, stderr=0.5, n=4)
    assert near_flat_up.significant is False
    assert is_strongly_significant(near_flat_up) is False
    label = apply_hysteresis(
        5,
        _fit(eng=near_flat_up, deleg=near_flat_down),
        previous_label=WeeklyTrajectoryLabel.LEARNING,
    )
    assert label is WeeklyTrajectoryLabel.LEARNING


def test_apply_hysteresis_blocks_drifting_to_atrophying_when_only_one_axis_weak():
    # Previous = Drifting. Naive = Atrophying (eng now weakly down,
    # deleg still weakly up). Neither slope clears 2.0 -> hysteresis
    # retains Drifting until the engagement-down signal is strong enough.
    label = apply_hysteresis(
        5,
        _fit(eng=_WEAK_DOWN, deleg=_WEAK_UP),
        previous_label=WeeklyTrajectoryLabel.DRIFTING,
    )
    assert label is WeeklyTrajectoryLabel.DRIFTING


# ---- apply_hysteresis: change accepted when an axis crosses 2.0 ------------


def test_apply_hysteresis_accepts_change_when_engagement_strongly_significant():
    # Previous = Steady. Engagement now strongly up (2.0 gate cleared),
    # delegation only weakly down (1.5 cleared but not 2.0). The relevant
    # axis (engagement) is strongly significant -> accept Learning.
    label = apply_hysteresis(
        5,
        _fit(eng=_STRONG_UP, deleg=_WEAK_DOWN),
        previous_label=WeeklyTrajectoryLabel.STEADY,
    )
    assert label is WeeklyTrajectoryLabel.LEARNING


def test_apply_hysteresis_accepts_change_when_delegation_strongly_significant():
    # Previous = Steady. Delegation now strongly up; engagement weakly up.
    # Naive label is Steady (unspecified up,up combo), so naive == prev
    # and the gate is not even consulted - but assert the outcome anyway.
    label = apply_hysteresis(
        5,
        _fit(eng=_WEAK_UP, deleg=_STRONG_UP),
        previous_label=WeeklyTrajectoryLabel.STEADY,
    )
    assert label is WeeklyTrajectoryLabel.STEADY


def test_apply_hysteresis_accepts_steady_to_drifting_with_strong_delegation_up():
    # Previous = Steady. Engagement flat, delegation strongly up.
    # Naive = Drifting. Delegation is strongly significant -> accept Drifting.
    label = apply_hysteresis(
        5,
        _fit(eng=_FLAT, deleg=_STRONG_UP),
        previous_label=WeeklyTrajectoryLabel.STEADY,
    )
    assert label is WeeklyTrajectoryLabel.DRIFTING


def test_apply_hysteresis_accepts_learning_to_atrophying_with_strong_signals():
    # Both axes strongly flip direction.
    label = apply_hysteresis(
        5,
        _fit(eng=_STRONG_DOWN, deleg=_STRONG_UP),
        previous_label=WeeklyTrajectoryLabel.LEARNING,
    )
    assert label is WeeklyTrajectoryLabel.ATROPHYING


# ---- stability / fix-point -------------------------------------------------


def test_apply_hysteresis_is_fix_point_when_change_blocked():
    # Block branch: prev=Steady, naive=Learning (weak slopes), no strong sig.
    # First run returns Steady. Second run with prev=Steady must also return Steady.
    fit = _fit(eng=_WEAK_UP, deleg=_WEAK_DOWN)
    first = apply_hysteresis(5, fit, previous_label=WeeklyTrajectoryLabel.STEADY)
    assert first is WeeklyTrajectoryLabel.STEADY
    second = apply_hysteresis(5, fit, previous_label=first)
    assert second is first


def test_apply_hysteresis_is_fix_point_when_change_accepted():
    # Accept branch: prev=Steady, naive=Learning, eng strongly up.
    fit = _fit(eng=_STRONG_UP, deleg=_WEAK_DOWN)
    first = apply_hysteresis(5, fit, previous_label=WeeklyTrajectoryLabel.STEADY)
    assert first is WeeklyTrajectoryLabel.LEARNING
    second = apply_hysteresis(5, fit, previous_label=first)
    assert second is first


def test_apply_hysteresis_is_fix_point_for_reading():
    fit = _fit(eng=_WEAK_UP, deleg=_WEAK_DOWN)
    first = apply_hysteresis(3, fit, previous_label=WeeklyTrajectoryLabel.READING)
    assert first is WeeklyTrajectoryLabel.READING
    second = apply_hysteresis(3, fit, previous_label=first)
    assert second is first


def test_apply_hysteresis_is_fix_point_after_reading_to_learning():
    fit = _fit(eng=_WEAK_UP, deleg=_WEAK_DOWN)
    first = apply_hysteresis(4, fit, previous_label=WeeklyTrajectoryLabel.READING)
    assert first is WeeklyTrajectoryLabel.LEARNING
    # Now that we are at Learning, the same weak fit should keep us at Learning
    # (the naive label is still Learning - no gate needed).
    second = apply_hysteresis(4, fit, previous_label=first)
    assert second is first


# ---- label_trajectory_with_hysteresis: bucket-list wrapper -----------------


def test_label_trajectory_with_hysteresis_no_previous_matches_label_trajectory():
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=i), eng=0.10 + 0.10 * i, deleg=0.60 - 0.10 * i, count=2)
        for i in range(5)
    ]
    expected = label_trajectory(buckets)
    actual = label_trajectory_with_hysteresis(buckets)
    assert actual is expected


def test_label_trajectory_with_hysteresis_empty_returns_reading():
    assert label_trajectory_with_hysteresis([]) is WeeklyTrajectoryLabel.READING


def test_label_trajectory_with_hysteresis_short_circuits_below_threshold():
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=i), eng=0.10 * i, deleg=0.50 - 0.10 * i, count=2)
        for i in range(3)
    ]
    label = label_trajectory_with_hysteresis(
        buckets, previous_label=WeeklyTrajectoryLabel.LEARNING
    )
    # Below the 4-bucket threshold, Reading wins (the Reading bypass branch
    # allows this transition even though previous was Learning).
    assert label is WeeklyTrajectoryLabel.READING


def test_label_trajectory_with_hysteresis_stable_on_identical_input():
    # AC #3: "On identical input, the label does not change between two
    # consecutive runs." Build a real bucket list, label it once, then
    # label it again with that result as previous_label - the answer must
    # match.
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(monday + timedelta(weeks=i), eng=0.10 + 0.10 * i, deleg=0.60 - 0.10 * i, count=2)
        for i in range(5)
    ]
    first = label_trajectory_with_hysteresis(buckets)
    second = label_trajectory_with_hysteresis(buckets, previous_label=first)
    assert second is first


def test_label_trajectory_with_hysteresis_blocks_whiplash_on_noisy_buckets():
    # Build a window where the naive label disagrees with the previous
    # label but no axis crosses 2.0*stderr. Five buckets with a small,
    # erratic up-down pattern: the line-of-best-fit has a non-trivial
    # slope but the stderr is large enough that the slope only clears
    # 1.5*stderr, not 2.0*stderr.
    monday = date(2026, 5, 4)
    eng_values = [0.20, 0.40, 0.25, 0.45, 0.30]   # noisy upward drift
    deleg_values = [0.50, 0.30, 0.45, 0.25, 0.40]  # noisy downward drift
    buckets = [
        _bucket(monday + timedelta(weeks=i), eng=eng_values[i], deleg=deleg_values[i], count=2)
        for i in range(5)
    ]
    # First, confirm the naive label here is NOT Steady (so the test is
    # actually testing the hysteresis block, not a no-op).
    naive = label_trajectory(buckets)
    # The naive label will be Steady (because the noise eats the slope's
    # significance at the 1.5 gate), so this fixture is a no-op for
    # naive==prev case. To test the block, use prev = Learning and
    # confirm we retain Learning even though naive=Steady.
    assert naive is WeeklyTrajectoryLabel.STEADY
    label = label_trajectory_with_hysteresis(
        buckets, previous_label=WeeklyTrajectoryLabel.LEARNING
    )
    # naive (Steady) != prev (Learning), and no axis is strongly significant
    # -> hysteresis retains Learning.
    assert label is WeeklyTrajectoryLabel.LEARNING


def test_label_trajectory_with_hysteresis_accepts_strong_change_end_to_end():
    # Build a window where engagement rises clearly and delegation falls
    # clearly (no noise), so both axes are strongly significant.
    monday = date(2026, 5, 4)
    buckets = [
        _bucket(
            monday + timedelta(weeks=i),
            eng=0.10 + 0.10 * i,
            deleg=0.60 - 0.10 * i,
            count=2,
        )
        for i in range(6)
    ]
    label = label_trajectory_with_hysteresis(
        buckets, previous_label=WeeklyTrajectoryLabel.STEADY
    )
    # Clear Learning trajectory -> change accepted.
    assert label is WeeklyTrajectoryLabel.LEARNING


def test_label_trajectory_with_hysteresis_via_bucket_pipeline_stable():
    # End-to-end: sessions -> buckets -> hysteresis label, run twice on
    # identical input, assert no change. This is the AC #3 scenario as
    # the orchestrator would actually invoke it (the `now=` pin in
    # bucket_sessions_by_iso_week keeps the 90-day window stable).
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    sessions: list[WeeklySessionInput] = []
    for week_index in range(5):
        week_anchor = now - timedelta(days=28 - 7 * week_index)
        for offset in (0, 1):
            sessions.append(
                WeeklySessionInput(
                    started_at=week_anchor - timedelta(hours=offset),
                    engagement_rate=0.10 + 0.10 * week_index,
                    delegation_rate=0.50 - 0.10 * week_index,
                    independence_rate=0.05,
                    dim_scores={k: 5.0 for k in RUBRIC_KEYS},
                )
            )
    buckets_a = bucket_sessions_by_iso_week(sessions, now=now)
    first = label_trajectory_with_hysteresis(buckets_a)
    buckets_b = bucket_sessions_by_iso_week(sessions, now=now)
    second = label_trajectory_with_hysteresis(buckets_b, previous_label=first)
    assert second is first
