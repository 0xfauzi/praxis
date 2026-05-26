"""Behavioral analysis - measures skill formation vs atrophy over time."""

from praxis.behavior.signals import BehavioralSignals, extract
from praxis.behavior.trajectory import (
    TrajectoryAssessment,
    TrajectoryLabel,
    assess,
)
from praxis.behavior.slope import (
    SIGNIFICANCE_MIN_SLOPE_PER_WEEK,
    SIGNIFICANCE_STDERR_MULTIPLIER,
    SlopeFit,
    WeeklyTrajectoryFit,
    fit_metric,
    fit_weekly_trajectory,
)
from praxis.behavior.labels import (
    MIN_BUCKETS_FOR_LABEL,
    WeeklyTrajectoryLabel,
    label_from_fit,
    label_trajectory,
)
from praxis.behavior.weekly import (
    DEFAULT_WINDOW_DAYS,
    MIN_SESSIONS_FOR_FIT,
    WeeklyBucket,
    WeeklySessionInput,
    bucket_sessions_by_iso_week,
    iso_week_start,
    iso_week_tag,
)

__all__ = [
    "BehavioralSignals",
    "extract",
    "TrajectoryAssessment",
    "TrajectoryLabel",
    "assess",
    "DEFAULT_WINDOW_DAYS",
    "MIN_SESSIONS_FOR_FIT",
    "WeeklyBucket",
    "WeeklySessionInput",
    "bucket_sessions_by_iso_week",
    "iso_week_start",
    "iso_week_tag",
    "SIGNIFICANCE_MIN_SLOPE_PER_WEEK",
    "SIGNIFICANCE_STDERR_MULTIPLIER",
    "SlopeFit",
    "WeeklyTrajectoryFit",
    "fit_metric",
    "fit_weekly_trajectory",
    "MIN_BUCKETS_FOR_LABEL",
    "WeeklyTrajectoryLabel",
    "label_from_fit",
    "label_trajectory",
]
