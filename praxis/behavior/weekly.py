"""Weekly bucketing for the v0.2 trajectory model.

Spec section 7.2 (PRAXIS_V0_2_SPEC.md):

  - Bucket all sessions in the last 90 days into weekly buckets (ISO weeks).
  - For each weekly bucket, compute the mean of each signal (engagement_rate,
    delegation_rate, independence_rate, plus the 6 dim scores).
  - Weeks with fewer than 2 sessions are excluded from the fit but still
    rendered in the cost ledger.

This module owns step 1 only: it produces a deterministic, ordered list of
WeeklyBuckets that downstream stages (US-043 slope+stderr, US-044 label
mapping, US-045 hysteresis, US-046 headline) build on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Iterable

from praxis.scoring.rubric import RUBRIC


DEFAULT_WINDOW_DAYS = 90
MIN_SESSIONS_FOR_FIT = 2


@dataclass(frozen=True)
class WeeklySessionInput:
    """One session's contribution to the weekly trajectory model.

    The shape is intentionally minimal: just the timestamp plus the three
    behavioral rates and the six rubric dim scores. Callers adapt their
    richer types (Session + BehavioralSignals + SessionScore) into this
    flat row at the boundary so the bucket logic stays decoupled.
    """

    started_at: datetime
    engagement_rate: float
    delegation_rate: float
    independence_rate: float
    dim_scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class WeeklyBucket:
    iso_week: str            # "YYYY-Www", e.g. "2026-W21"
    week_start: date         # Monday of that ISO week (UTC)
    session_count: int
    engagement_rate_mean: float
    delegation_rate_mean: float
    independence_rate_mean: float
    dim_score_means: dict[str, float]   # one entry per RUBRIC key
    eligible_for_fit: bool   # session_count >= MIN_SESSIONS_FOR_FIT


def iso_week_tag(d: date | datetime) -> str:
    """Return the ISO-week tag in the form 'YYYY-Www' (zero-padded week)."""
    if isinstance(d, datetime):
        # Convert to UTC before taking the calendar date: a timestamp with
        # a non-UTC offset must bucket by its UTC date, matching
        # bucket_sessions_by_iso_week, or a near-midnight session lands in
        # the wrong week and corrupts week-over-week deltas.
        d = _to_utc(d).date()
    year, week, _ = d.isocalendar()
    return f"{year:04d}-W{week:02d}"


def iso_week_start(d: date | datetime) -> date:
    """Return the Monday (start) of the ISO week containing d."""
    if isinstance(d, datetime):
        d = _to_utc(d).date()
    year, week, _ = d.isocalendar()
    # ISO weekday 1 is Monday.
    return date.fromisocalendar(year, week, 1)


def _to_utc(dt: datetime) -> datetime:
    """Normalize a (possibly naive) datetime to UTC.

    Naive datetimes are interpreted as UTC, matching the rest of the
    pipeline (scanners emit UTC; tests use timezone.utc).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bucket_sessions_by_iso_week(
    sessions: Iterable[WeeklySessionInput],
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[WeeklyBucket]:
    """Bucket sessions by ISO week, restricted to the last window_days days.

    Returns buckets sorted by week_start ascending (oldest first). Every
    bucket carries an `eligible_for_fit` flag: True iff session_count >=
    MIN_SESSIONS_FOR_FIT (2). Ineligible buckets are still returned so the
    cost-ledger renderer (spec 7.4) can show them.

    `now` defaults to `datetime.now(timezone.utc)`; it exists only so tests
    can pin the window. `window_days` defaults to 90 per spec 7.2.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    else:
        now = _to_utc(now)
    cutoff = now - timedelta(days=window_days)

    grouped: dict[str, list[WeeklySessionInput]] = {}
    for s in sessions:
        s_utc = _to_utc(s.started_at)
        if s_utc < cutoff or s_utc > now:
            continue
        tag = iso_week_tag(s_utc)
        grouped.setdefault(tag, []).append(s)

    buckets: list[WeeklyBucket] = []
    for tag, members in grouped.items():
        # Use the first member's timestamp to anchor the bucket's Monday.
        anchor = _to_utc(members[0].started_at)
        start = iso_week_start(anchor)

        eng_mean = mean(m.engagement_rate for m in members)
        del_mean = mean(m.delegation_rate for m in members)
        ind_mean = mean(m.independence_rate for m in members)

        dim_means: dict[str, float] = {}
        for d in RUBRIC:
            values = [
                m.dim_scores[d.key]
                for m in members
                if d.key in m.dim_scores
            ]
            if values:
                dim_means[d.key] = mean(values)

        buckets.append(
            WeeklyBucket(
                iso_week=tag,
                week_start=start,
                session_count=len(members),
                engagement_rate_mean=eng_mean,
                delegation_rate_mean=del_mean,
                independence_rate_mean=ind_mean,
                dim_score_means=dim_means,
                eligible_for_fit=len(members) >= MIN_SESSIONS_FOR_FIT,
            )
        )

    buckets.sort(key=lambda b: b.week_start)
    return buckets
