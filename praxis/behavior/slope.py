"""Least-squares slope and stderr over weekly buckets (US-043).

Spec section 7.2 (PRAXIS_V0_2_SPEC.md):

  - Fit a least-squares line over the weekly means.
  - Compute the standard error of the slope. If |slope| < 1.5 * stderr,
    the trajectory is flat / noisy, regardless of sign.
  - "Significantly" means |slope| >= 1.5 * stderr AND |slope| >= 0.05 per week.

This module owns the math only. Label assignment (US-044), hysteresis
(US-045), and the LLM headline (US-046) build on the SlopeFit values
produced here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import partial
from typing import Callable, Iterable

from praxis.behavior.weekly import WeeklyBucket


SIGNIFICANCE_STDERR_MULTIPLIER: float = 1.5
SIGNIFICANCE_MIN_SLOPE_PER_WEEK: float = 0.05


@dataclass(frozen=True)
class SlopeFit:
    """Result of fitting a least-squares line to weekly means.

    The x-axis is "weeks since the earliest eligible bucket", so the
    slope's unit is "metric per ISO week" - which is the unit the spec
    thresholds (0.05/week, 1.5*stderr) are written in.

    n is the number of eligible buckets that actually contributed to
    this fit (after dropping ineligible buckets and any buckets missing
    the requested metric, e.g. an absent dim_score key).
    """

    slope: float
    stderr: float
    n: int

    @property
    def significant(self) -> bool:
        """Spec 7.2 gate: |slope| >= 1.5 * stderr AND |slope| >= 0.05 per week."""
        if self.n < 2:
            return False
        return (
            abs(self.slope) >= SIGNIFICANCE_STDERR_MULTIPLIER * self.stderr
            and abs(self.slope) >= SIGNIFICANCE_MIN_SLOPE_PER_WEEK
        )


@dataclass(frozen=True)
class WeeklyTrajectoryFit:
    """Slope + stderr for every signal the v0.2 trajectory model fits.

    engagement / delegation / independence map to the three rate means
    on WeeklyBucket; dim_scores carries one SlopeFit per RUBRIC key
    that at least one eligible bucket reported.
    """

    engagement: SlopeFit
    delegation: SlopeFit
    independence: SlopeFit
    dim_scores: dict[str, SlopeFit] = field(default_factory=dict)


def _least_squares(points: list[tuple[float, float]]) -> SlopeFit:
    """Fit slope + stderr to (x, y) points.

    For n < 2 or degenerate x (all equal): slope=0, stderr=0.
    For n == 2: slope is exact, stderr=0 (no residual degrees of freedom).
    For n >= 3: standard textbook simple-linear-regression formula
        SE(slope) = sqrt( SSR / (n-2) / Sxx )
    where SSR is the sum of squared residuals and Sxx = sum((x-x_mean)^2).
    """
    n = len(points)
    if n < 2:
        return SlopeFit(slope=0.0, stderr=0.0, n=n)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    sxx = sum((x - x_mean) ** 2 for x in xs)
    if sxx == 0.0:
        return SlopeFit(slope=0.0, stderr=0.0, n=n)
    sxy = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    slope = sxy / sxx
    if n == 2:
        return SlopeFit(slope=slope, stderr=0.0, n=n)
    intercept = y_mean - slope * x_mean
    ssr = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    variance = ssr / (n - 2)
    stderr = math.sqrt(variance / sxx) if variance > 0.0 else 0.0
    return SlopeFit(slope=slope, stderr=stderr, n=n)


def _eligible_sorted(buckets: Iterable[WeeklyBucket]) -> list[WeeklyBucket]:
    eligible = [b for b in buckets if b.eligible_for_fit]
    eligible.sort(key=lambda b: b.week_start)
    return eligible


def _week_offset(b: WeeklyBucket, anchor: WeeklyBucket) -> float:
    """Whole-week offset from anchor; week_start is always a Monday so this is integral."""
    return (b.week_start - anchor.week_start).days / 7.0


def fit_metric(
    buckets: Iterable[WeeklyBucket],
    getter: Callable[[WeeklyBucket], float | None],
) -> SlopeFit:
    """Fit slope + stderr for one per-bucket scalar metric.

    Only eligible_for_fit buckets are considered. Within those, any
    bucket where getter returns None is skipped (covers the "skip-not-
    invent" contract for dim_score keys that some buckets do not report).
    """
    eligible = _eligible_sorted(buckets)
    if not eligible:
        return SlopeFit(slope=0.0, stderr=0.0, n=0)
    anchor = eligible[0]
    points: list[tuple[float, float]] = []
    for b in eligible:
        y = getter(b)
        if y is None:
            continue
        points.append((_week_offset(b, anchor), float(y)))
    return _least_squares(points)


def _get_engagement(b: WeeklyBucket) -> float | None:
    return b.engagement_rate_mean


def _get_delegation(b: WeeklyBucket) -> float | None:
    return b.delegation_rate_mean


def _get_independence(b: WeeklyBucket) -> float | None:
    return b.independence_rate_mean


def _get_dim(key: str, b: WeeklyBucket) -> float | None:
    return b.dim_score_means.get(key)


def fit_weekly_trajectory(buckets: Iterable[WeeklyBucket]) -> WeeklyTrajectoryFit:
    """Compute slope+stderr for every v0.2 trajectory signal."""
    bucket_list = list(buckets)
    engagement = fit_metric(bucket_list, _get_engagement)
    delegation = fit_metric(bucket_list, _get_delegation)
    independence = fit_metric(bucket_list, _get_independence)

    seen: set[str] = set()
    dim_keys: list[str] = []
    for b in bucket_list:
        if not b.eligible_for_fit:
            continue
        for k in b.dim_score_means:
            if k not in seen:
                seen.add(k)
                dim_keys.append(k)

    dim_fits: dict[str, SlopeFit] = {
        k: fit_metric(bucket_list, partial(_get_dim, k)) for k in dim_keys
    }

    return WeeklyTrajectoryFit(
        engagement=engagement,
        delegation=delegation,
        independence=independence,
        dim_scores=dim_fits,
    )
