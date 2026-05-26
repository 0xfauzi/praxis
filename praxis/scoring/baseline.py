"""90-day rolling baseline of session scores.

Per spec sections 8.1 and 8.4, the baseline is computed as:
  - the mean of session scores over the last 90 days
  - excluding the current ISO week (so this week's data does not leak
    into its own baseline)
  - with each session weighted equally regardless of length (length
    shows up in the cost ledger; the baseline should not double-count it)
  - with each session's `overall` clipped to [1.0, 9.0] to bound the
    influence of outlier marathon sessions

The function is pure: pass in the candidate sessions plus an `as_of`
moment, get back a Baseline value. Wiring this into the weekly digest
pipeline is a separate concern handled by later stories.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from praxis.scoring.rubric import RUBRIC


OVERALL_CLIP_LOW = 1.0
OVERALL_CLIP_HIGH = 9.0

BASELINE_WINDOW_DAYS = 90


@dataclass(frozen=True)
class BaselineInputSession:
    """One session's contribution to the baseline.

    Decoupled from SessionScore + BehavioralSignals so the baseline
    function stays pure: callers can build this from any source (DB
    rows, live scoring output, replay fixtures) without dragging the
    full session model along.
    """

    started_at: datetime
    overall: float
    dimension_scores: dict[str, float]
    engagement_rate: float
    delegation_rate: float
    independence_rate: float


@dataclass(frozen=True)
class Baseline:
    """90-day rolling baseline values, with the current ISO week excluded."""

    overall_mean: float
    dimension_means: dict[str, float]
    engagement_mean: float
    delegation_mean: float
    independence_mean: float
    session_count: int
    window_start: date    # inclusive: as_of_date - 90 days
    window_end: date      # exclusive: start of as_of's ISO week (Monday)


def _iso_week_start(d: date) -> date:
    """The Monday of the ISO week containing d."""
    return d - timedelta(days=d.weekday())


def _clip_overall(value: float) -> float:
    return max(OVERALL_CLIP_LOW, min(OVERALL_CLIP_HIGH, value))


def _empty_baseline(window_start: date, window_end: date) -> Baseline:
    return Baseline(
        overall_mean=0.0,
        dimension_means={d.key: 0.0 for d in RUBRIC},
        engagement_mean=0.0,
        delegation_mean=0.0,
        independence_mean=0.0,
        session_count=0,
        window_start=window_start,
        window_end=window_end,
    )


def compute_baseline(
    sessions: list[BaselineInputSession],
    as_of: datetime | None = None,
) -> Baseline:
    """Mean of session scores over [as_of - 90 days, current ISO week start).

    Sessions are weighted equally regardless of length. Each session's
    `overall` is clipped to [1.0, 9.0] before contributing; dimension
    scores and behavioral rates are averaged as-is (they are already
    in bounded ranges by construction).

    The returned Baseline carries `window_start` and `window_end` so
    callers can render the panel with the correct date range. When no
    sessions fall in the window, all means are 0.0 and session_count
    is 0; callers decide whether to render "baseline forming" prose
    based on that signal (see US-035).
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    as_of_date = as_of.date()
    window_end = _iso_week_start(as_of_date)
    window_start = as_of_date - timedelta(days=BASELINE_WINDOW_DAYS)

    included = [
        s for s in sessions
        if window_start <= s.started_at.date() < window_end
    ]
    n = len(included)
    if n == 0:
        return _empty_baseline(window_start, window_end)

    overall_mean = sum(_clip_overall(s.overall) for s in included) / n
    dim_means: dict[str, float] = {
        d.key: sum(s.dimension_scores.get(d.key, 5.0) for s in included) / n
        for d in RUBRIC
    }
    engagement_mean = sum(s.engagement_rate for s in included) / n
    delegation_mean = sum(s.delegation_rate for s in included) / n
    independence_mean = sum(s.independence_rate for s in included) / n

    return Baseline(
        overall_mean=round(overall_mean, 2),
        dimension_means={k: round(v, 2) for k, v in dim_means.items()},
        engagement_mean=round(engagement_mean, 4),
        delegation_mean=round(delegation_mean, 4),
        independence_mean=round(independence_mean, 4),
        session_count=n,
        window_start=window_start,
        window_end=window_end,
    )
