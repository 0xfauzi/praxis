"""Trajectory label assignment per spec section 7.2 (US-044) plus the
hysteresis gate from section 7.4 (US-045).

This module owns the v0.2 label table only. It is intentionally separate
from praxis.behavior.trajectory (the v0.1 LLM-based assessor) so the two
generations can coexist while callers migrate.

Spec section 7.2 table:

  | Condition                                                  | Label             |
  | engagement slope significantly up, delegation sig down     | Learning          |
  | engagement slope flat, delegation slope sig down           | Growing autonomy  |
  | engagement slope flat, delegation slope flat               | Steady            |
  | engagement slope flat or down, delegation slope sig up     | Drifting          |
  | engagement slope sig down, delegation slope sig up         | Atrophying        |
  | Fewer than 4 weekly buckets with data                      | Reading           |

"Significantly" is the SlopeFit.significant gate from US-043 (the 1.5*stderr
multiplier). "Flat" is the negation of significant. Atrophying is the strict
subset of the Drifting row, so it is matched first.

Combinations not in the table - (up, flat), (up, up), (down, flat),
(down, down) - fall back to Steady.

Spec section 7.4 hysteresis: a new label is only allowed to differ from
the previous week's label when the stricter 2.0*stderr gate (instead of
1.5*) is met on the relevant axis. Otherwise the previous label sticks.
This prevents a single noisy week from flipping the headline label.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable

from praxis.behavior.slope import (
    SIGNIFICANCE_MIN_SLOPE_PER_WEEK,
    SlopeFit,
    WeeklyTrajectoryFit,
    fit_weekly_trajectory,
)
from praxis.behavior.weekly import WeeklyBucket


MIN_BUCKETS_FOR_LABEL: int = 4
HYSTERESIS_STDERR_MULTIPLIER: float = 2.0


class WeeklyTrajectoryLabel(str, Enum):
    """User-facing trajectory labels.

    Values match the display strings used in spec section 7.2 so that
    `label.value` is print-ready.
    """

    LEARNING = "Learning"
    GROWING_AUTONOMY = "Growing autonomy"
    STEADY = "Steady"
    DRIFTING = "Drifting"
    ATROPHYING = "Atrophying"
    READING = "Reading"


def _direction(fit: SlopeFit) -> str:
    """Return 'up', 'down', or 'flat' for a slope fit.

    A non-significant fit is always 'flat' regardless of slope sign,
    per spec 7.2 ("flat / noisy, regardless of sign").
    """
    if not fit.significant:
        return "flat"
    return "up" if fit.slope > 0 else "down"


def label_from_fit(
    eligible_bucket_count: int,
    fit: WeeklyTrajectoryFit,
) -> WeeklyTrajectoryLabel:
    """Assign a trajectory label from an already-computed fit.

    `eligible_bucket_count` is the number of WeeklyBuckets with
    `eligible_for_fit == True` in the input window. When it is below
    MIN_BUCKETS_FOR_LABEL (4 per spec), we return READING regardless
    of what the fit says.
    """
    if eligible_bucket_count < MIN_BUCKETS_FOR_LABEL:
        return WeeklyTrajectoryLabel.READING

    eng = _direction(fit.engagement)
    deleg = _direction(fit.delegation)

    if eng == "down" and deleg == "up":
        return WeeklyTrajectoryLabel.ATROPHYING
    if eng == "flat" and deleg == "up":
        return WeeklyTrajectoryLabel.DRIFTING
    if eng == "up" and deleg == "down":
        return WeeklyTrajectoryLabel.LEARNING
    if eng == "flat" and deleg == "down":
        return WeeklyTrajectoryLabel.GROWING_AUTONOMY
    if eng == "flat" and deleg == "flat":
        return WeeklyTrajectoryLabel.STEADY
    return WeeklyTrajectoryLabel.STEADY


def label_trajectory(buckets: Iterable[WeeklyBucket]) -> WeeklyTrajectoryLabel:
    """Bucket-list convenience wrapper around `label_from_fit`.

    Counts eligible buckets, short-circuits to READING when below the
    spec threshold (so we never invoke the fit on too-few buckets),
    and otherwise computes the fit and delegates.
    """
    bucket_list = list(buckets)
    eligible_count = sum(1 for b in bucket_list if b.eligible_for_fit)
    if eligible_count < MIN_BUCKETS_FOR_LABEL:
        return WeeklyTrajectoryLabel.READING
    fit = fit_weekly_trajectory(bucket_list)
    return label_from_fit(eligible_count, fit)


def is_strongly_significant(fit: SlopeFit) -> bool:
    """Spec 7.4 hysteresis gate: |slope| >= 2.0 * stderr AND |slope| >= 0.05/week.

    This is the stricter sibling of `SlopeFit.significant` (which uses the
    1.5 multiplier from US-043). The minimum-slope-per-week floor stays at
    0.05; only the stderr multiplier tightens.
    """
    if fit.n < 2:
        return False
    return (
        abs(fit.slope) >= HYSTERESIS_STDERR_MULTIPLIER * fit.stderr
        and abs(fit.slope) >= SIGNIFICANCE_MIN_SLOPE_PER_WEEK
    )


def apply_hysteresis(
    eligible_bucket_count: int,
    fit: WeeklyTrajectoryFit,
    previous_label: WeeklyTrajectoryLabel | None,
) -> WeeklyTrajectoryLabel:
    """Spec 7.4 hysteresis: gate label changes behind the 2.0*stderr threshold.

    Returns the naive 1.5*stderr label (from `label_from_fit`) when:
      - There is no previous label (cold start), or
      - The naive label equals the previous label (no change to gate), or
      - The transition crosses the Reading boundary (data-availability
        change, not a slope-direction change - hysteresis does not apply), or
      - At least one of engagement / delegation has a strongly significant
        slope, meaning the relevant axis clears the 2.0*stderr gate.

    Otherwise the previous label is retained. This makes the function a
    fix-point operator: running it twice on the same `(buckets, label)`
    pair produces the same label - the AC #3 "no change on identical
    input" property.
    """
    naive = label_from_fit(eligible_bucket_count, fit)
    if previous_label is None or naive is previous_label:
        return naive
    # Reading <-> non-Reading transitions are driven by bucket count, not
    # slope direction. Hysteresis only protects against trajectory whiplash;
    # data-availability changes pass through.
    if (
        previous_label is WeeklyTrajectoryLabel.READING
        or naive is WeeklyTrajectoryLabel.READING
    ):
        return naive
    if is_strongly_significant(fit.engagement) or is_strongly_significant(
        fit.delegation
    ):
        return naive
    return previous_label


def label_trajectory_with_hysteresis(
    buckets: Iterable[WeeklyBucket],
    previous_label: WeeklyTrajectoryLabel | None = None,
) -> WeeklyTrajectoryLabel:
    """Bucket-list wrapper that applies spec 7.4 hysteresis.

    When `previous_label` is None (cold start, no history), this is the
    same as `label_trajectory`. When a previous label is supplied, the
    return value can stay equal to it even when the underlying fit would
    suggest a different label - see `apply_hysteresis` for the gate.
    """
    bucket_list = list(buckets)
    eligible_count = sum(1 for b in bucket_list if b.eligible_for_fit)
    if eligible_count < MIN_BUCKETS_FOR_LABEL:
        # Below the data threshold the label is forced to Reading. The
        # Reading-bypass branch in `apply_hysteresis` would also produce
        # Reading here, so short-circuit instead of fitting on too-few
        # buckets.
        return WeeklyTrajectoryLabel.READING
    fit = fit_weekly_trajectory(bucket_list)
    return apply_hysteresis(eligible_count, fit, previous_label)
