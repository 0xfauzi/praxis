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
from datetime import UTC, date, datetime, timedelta

from praxis.scoring.rubric import RUBRIC

OVERALL_CLIP_LOW = 1.0
OVERALL_CLIP_HIGH = 9.0

BASELINE_WINDOW_DAYS = 90

# Spec section 8.4: when the user has less than this many days of data,
# the baseline does not yet exist. Renderers show "--" in place of the
# baseline number and skip deltas; a "Baseline forming" note replaces
# the per-dim annotation. 14 days = 2 weeks, matching the "Come back in
# 2 more weeks for week-over-week" copy in the panel.
MIN_DAYS_FOR_BASELINE = 14


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
    window_start: date  # inclusive: as_of_date - 90 days
    window_end: date  # exclusive: start of as_of's ISO week (Monday)


@dataclass(frozen=True)
class LastWeekMean:
    """Mean of session scores from the immediately prior ISO week.

    Returned by `compute_last_week_mean` when the user has at least
    one session in the prior ISO week (Monday-to-Sunday before
    `as_of`'s week). When no sessions fall in that window the function
    returns None, which is the renderer signal to omit the last-week
    annotation entirely.

    Per spec section 8.1, this is rendered only in the HTML digest as
    a faded secondary anchor next to the 90-day baseline; the terminal
    digest omits it. The data shape mirrors Baseline so callers can
    treat the two annotations symmetrically at the call site.
    """

    overall_mean: float
    dimension_means: dict[str, float]
    engagement_mean: float
    delegation_mean: float
    independence_mean: float
    session_count: int
    week_start: date  # inclusive: Monday of prior ISO week
    week_end: date  # exclusive: Monday of current ISO week


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
        as_of = datetime.now(UTC)
    as_of_date = as_of.date()
    window_end = _iso_week_start(as_of_date)
    window_start = as_of_date - timedelta(days=BASELINE_WINDOW_DAYS)

    included = [s for s in sessions if window_start <= s.started_at.date() < window_end]
    n = len(included)
    if n == 0:
        return _empty_baseline(window_start, window_end)

    overall_mean = sum(_clip_overall(s.overall) for s in included) / n
    dim_means: dict[str, float] = {
        d.key: sum(s.dimension_scores.get(d.key, 5.0) for s in included) / n for d in RUBRIC
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


def data_span_days(
    sessions: list[BaselineInputSession],
    as_of: datetime | None = None,
) -> int:
    """Calendar-day span from the earliest session to `as_of`.

    Returns 0 if no sessions. Otherwise returns the number of calendar
    days between the earliest session's date and `as_of`'s date. A user
    whose earliest session is today has a span of 0; yesterday is 1; two
    weeks ago is 14. This is the signal spec section 8.4 keys off when
    deciding whether the baseline is still "forming".

    The span uses ALL sessions in the input, not just those inside the
    90-day baseline window. A user with one session 200 days ago and
    nothing since still has 200 days of data, even though that session
    will not contribute to the baseline mean.
    """
    if not sessions:
        return 0
    if as_of is None:
        as_of = datetime.now(UTC)
    earliest_date = min(s.started_at.date() for s in sessions)
    return max(0, (as_of.date() - earliest_date).days)


def is_baseline_forming(
    sessions: list[BaselineInputSession],
    as_of: datetime | None = None,
) -> bool:
    """True iff the user has less than 14 days of data (spec section 8.4).

    When True, callers MUST render the baseline value as "--" and MUST
    NOT render a delta. The "Baseline forming. Come back in 2 more
    weeks for week-over-week." note (see
    `praxis.reports.baseline_panel.BASELINE_FORMING_MESSAGE`) replaces
    the per-dim baseline annotation. The last-week mean still renders if
    `has_prior_week_sessions` is True for the same input.
    """
    return data_span_days(sessions, as_of) < MIN_DAYS_FOR_BASELINE


def has_prior_week_sessions(
    sessions: list[BaselineInputSession],
    as_of: datetime | None = None,
) -> bool:
    """True iff at least one session occurred before `as_of`'s ISO week.

    Per spec section 8.4, even when the baseline is forming the last-week
    mean MUST still render "if there is at least one prior week of
    data". This helper makes that condition explicit so renderers can
    decide independently of `is_baseline_forming`: a user can have <14
    days of data AND a prior-week session (e.g., started using the tool
    8 days ago).
    """
    if not sessions:
        return False
    if as_of is None:
        as_of = datetime.now(UTC)
    current_week_start = _iso_week_start(as_of.date())
    return any(s.started_at.date() < current_week_start for s in sessions)


def compute_last_week_mean(
    sessions: list[BaselineInputSession],
    as_of: datetime | None = None,
) -> LastWeekMean | None:
    """Mean over the prior ISO week (Monday-to-Sunday before `as_of`'s week).

    Returns None when no sessions fall in that window -- the renderer
    omits the last-week annotation entirely in that case (the "2+ weeks
    of data" precondition from spec section 8.1 is not met).

    Applies the same outlier clipping as `compute_baseline` (overall
    clipped to [1.0, 9.0] before averaging) and the same equal-weight
    per session, so the last-week annotation rendered next to the
    baseline annotation is on the same scale and the two numbers are
    directly comparable.

    Week boundaries are aligned with the baseline window: ``week_end``
    matches ``Baseline.window_end`` (the Monday of the current ISO
    week), and ``week_start`` is the Monday of the prior ISO week
    (``week_end - 7 days``). A session at 00:00 on ``week_start`` is
    included; a session at 00:00 on ``week_end`` is NOT (that session
    belongs to the current ISO week and would land in this-week's mean).
    """
    if as_of is None:
        as_of = datetime.now(UTC)
    week_end = _iso_week_start(as_of.date())
    week_start = week_end - timedelta(days=7)

    included = [s for s in sessions if week_start <= s.started_at.date() < week_end]
    n = len(included)
    if n == 0:
        return None

    overall_mean = sum(_clip_overall(s.overall) for s in included) / n
    dim_means: dict[str, float] = {
        d.key: sum(s.dimension_scores.get(d.key, 5.0) for s in included) / n for d in RUBRIC
    }
    engagement_mean = sum(s.engagement_rate for s in included) / n
    delegation_mean = sum(s.delegation_rate for s in included) / n
    independence_mean = sum(s.independence_rate for s in included) / n

    return LastWeekMean(
        overall_mean=round(overall_mean, 2),
        dimension_means={k: round(v, 2) for k, v in dim_means.items()},
        engagement_mean=round(engagement_mean, 4),
        delegation_mean=round(delegation_mean, 4),
        independence_mean=round(independence_mean, 4),
        session_count=n,
        week_start=week_start,
        week_end=week_end,
    )
