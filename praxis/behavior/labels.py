"""Trajectory label assignment per spec section 7.2 (US-044).

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
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable

from praxis.behavior.slope import (
    SlopeFit,
    WeeklyTrajectoryFit,
    fit_weekly_trajectory,
)
from praxis.behavior.weekly import WeeklyBucket


MIN_BUCKETS_FOR_LABEL: int = 4


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
