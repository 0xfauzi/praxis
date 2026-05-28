"""Cadence detection.

Identifies "substantive" coding sessions and computes how many distinct
weekdays in a rolling window contained at least one such session. This
is the foundation for the high-adopter positioning (US-013) and for any
future coaching that wants to nudge a user back into a regular cadence.

A session is "substantive" iff:
  - It has at least MIN_USER_TURNS user turns (default 2), and
  - It lasted at least MIN_ELAPSED_SECONDS seconds (default 60).

These thresholds intentionally filter out throwaway one-shot prompts
("write me X") so they don't inflate a streak. Both thresholds are
exposed as module-level constants so callers can read them but must
not duplicate them.

The streak window defaults to DEFAULT_STREAK_WINDOW_DAYS = 21 days
(three rolling weeks): long enough to capture weekly users and short
enough to react to a recent drop-off.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Mapping

if TYPE_CHECKING:
    from praxis.storage.profile_store import ProfileStore


# A session must have at least this many user turns to be substantive.
MIN_USER_TURNS: int = 2

# A session must have lasted at least this many seconds to be substantive.
MIN_ELAPSED_SECONDS: int = 60

# Default rolling window for the weekday streak.
DEFAULT_STREAK_WINDOW_DAYS: int = 21

# High-adopter spectrum thresholds, expressed as ``streak / window_days``.
#
# Derived from Kumar et al. (2025), "Intuition to Evidence: Measuring AI's
# True Impact on Developer Productivity" (arXiv 2509.19708), an empirical
# study of 300 engineers using the DeputyDev enterprise coding assistant
# over twelve months. The paper defines its "High Adoption Cohort" as
# the top quartile of users (above the 75th percentile of code-generation
# requests per month) and its "Low Adoption Cohort" as the bottom quartile
# (below the 25th percentile). We translate those quartile cutoffs into a
# streak-to-window ratio so the same trichotomy generalises to any rolling
# window size used by :func:`compute_weekday_streak`.
LOW_STREAK_RATIO: float = 0.25
HIGH_STREAK_RATIO: float = 0.75

# Literal alias for the three possible high-adopter positions.
HighAdopterPosition = Literal["low", "moderate", "high"]


def is_substantive_session(row: Mapping[str, Any] | None) -> bool:
    """Return True iff the session_score row meets substantive-session thresholds.

    A row is substantive iff:
      - It exists (not None / not an empty mapping),
      - row["user_turns"] >= MIN_USER_TURNS, and
      - row["elapsed_seconds"] >= MIN_ELAPSED_SECONDS.

    Missing or non-numeric fields are treated as 0 so callers can pass in
    partial rows without the function raising. The thresholds are sourced
    from the module-level MIN_USER_TURNS and MIN_ELAPSED_SECONDS constants;
    callers must not duplicate them.
    """
    if not row:
        return False
    user_turns_raw = row.get("user_turns", 0)
    elapsed_raw = row.get("elapsed_seconds", 0)
    user_turns = _coerce_int(user_turns_raw)
    elapsed_seconds = _coerce_float(elapsed_raw)
    if user_turns is None or elapsed_seconds is None:
        return False
    return user_turns >= MIN_USER_TURNS and elapsed_seconds >= MIN_ELAPSED_SECONDS


def compute_weekday_streak(
    profile_store: "ProfileStore",
    ending_on_date: date,
    *,
    window_days: int = DEFAULT_STREAK_WINDOW_DAYS,
) -> int:
    """Count distinct weekdays with >=1 substantive session in the rolling window.

    The window is the `window_days` calendar days ending on (and including)
    ``ending_on_date``. Returns the number of distinct dates in that window
    where at least one persisted session_score row is substantive per
    :func:`is_substantive_session`.

    Always returns ``int``; never ``None``. Returns 0 when:
      - ``window_days`` is non-positive,
      - the profile_store has no session_scores at all,
      - the window contains no rows, or
      - no rows in the window are substantive.

    The function tolerates an empty profile_store (no sessions ever
    written) without raising. Rows whose ``started_at`` cannot be parsed
    are skipped rather than aborting the loop.
    """
    if window_days <= 0:
        return 0

    window_start_date = ending_on_date - timedelta(days=window_days - 1)
    window_start_dt = datetime.combine(
        window_start_date, datetime.min.time(), tzinfo=timezone.utc
    )

    try:
        rows = profile_store.load_session_scores(since=window_start_dt)
    except Exception:  # noqa: BLE001 - defensive: fresh/broken stores must not crash
        return 0

    substantive_days: set[date] = set()
    for row in rows:
        cadence_row = _adapt_score_row(row)
        if not is_substantive_session(cadence_row):
            continue
        d = _coerce_to_utc_date(row.get("started_at"))
        if d is None:
            continue
        if d < window_start_date or d > ending_on_date:
            continue
        substantive_days.add(d)

    return len(substantive_days)


def high_adopter_position(streak: int, window_days: int) -> HighAdopterPosition:
    """Classify the user's adoption tier from a weekday streak and its window.

    The streak / window_days ratio is bucketed against quartile-derived
    cutoffs:

    - ratio < :data:`LOW_STREAK_RATIO` (0.25) -> ``"low"``
    - :data:`LOW_STREAK_RATIO` <= ratio < :data:`HIGH_STREAK_RATIO` (0.75) -> ``"moderate"``
    - ratio >= :data:`HIGH_STREAK_RATIO` -> ``"high"``

    Thresholds are sourced from Kumar et al. (2025), "Intuition to
    Evidence: Measuring AI's True Impact on Developer Productivity"
    (arXiv 2509.19708), which separates a "High Adoption Cohort" at the
    top quartile of users (>75th percentile of code-generation requests)
    from a "Low Adoption Cohort" at the bottom quartile (<25th
    percentile). Those cohort cutoffs are re-expressed here as a
    streak/window ratio so the trichotomy scales with any window size.

    Args:
        streak: The number of distinct weekdays with a substantive
            session inside the window, as returned by
            :func:`compute_weekday_streak`. Must be ``<= window_days``.
        window_days: The rolling window size in days. Must be ``> 0``.

    Returns:
        One of ``"low"``, ``"moderate"``, or ``"high"``.

    Raises:
        ValueError: If ``window_days <= 0`` (invariant: a non-positive
            window has no quartile semantics). Raised before any further
            work so the function is safe to call before any DB read.
        ValueError: If ``streak > window_days`` (invariant: a streak
            cannot count more distinct days than the window itself
            contains).
    """
    if window_days <= 0:
        raise ValueError(
            f"window_days must be positive; got {window_days}"
        )
    if streak > window_days:
        raise ValueError(
            f"streak ({streak}) cannot exceed window_days ({window_days})"
        )

    ratio = streak / window_days
    if ratio < LOW_STREAK_RATIO:
        return "low"
    if ratio < HIGH_STREAK_RATIO:
        return "moderate"
    return "high"


def _adapt_score_row(score_row: Mapping[str, Any]) -> dict[str, Any]:
    """Build a cadence row (user_turns + elapsed_seconds) from a session_scores row.

    `user_turns` is sourced from ``signals_json["user_turn_count"]`` (the
    name used by BehavioralSignals); `elapsed_seconds` is sourced from
    ``signals_json["elapsed_seconds"]`` or, as a fallback, from
    ``features["elapsed_seconds"]`` (so future iterations that persist the
    field in either place will work without a cadence-module change).
    Missing fields stay at 0, which causes :func:`is_substantive_session`
    to reject the row.
    """
    user_turns = 0
    elapsed_seconds = 0.0

    parsed_signals = _parse_signals(score_row.get("signals_json"))
    if parsed_signals is not None:
        utc = _coerce_int(parsed_signals.get("user_turn_count"))
        if utc is not None:
            user_turns = utc
        es = _coerce_float(parsed_signals.get("elapsed_seconds"))
        if es is not None:
            elapsed_seconds = es

    if elapsed_seconds == 0.0:
        features = score_row.get("features")
        if isinstance(features, Mapping):
            es = _coerce_float(features.get("elapsed_seconds"))
            if es is not None:
                elapsed_seconds = es

    return {
        "user_turns": user_turns,
        "elapsed_seconds": elapsed_seconds,
    }


def _parse_signals(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(decoded, dict):
            return decoded
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_to_utc_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.date()
        return dt.astimezone(timezone.utc).date()
    return None
