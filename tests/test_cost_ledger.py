"""Tests for the weekly cost ledger (US-047).

Acceptance criteria from the PRD:
  - Total USD spend across all priced models for the week is computed.
  - 90-day rolling weekly mean is computed alongside.

Design contract enforced by these tests:
  - The aggregator (`compute_cost_ledger`) is pure: pre-computed
    per-session costs in, ledger out. No disk I/O, no card lookups.
  - The card-resolution helper (`estimate_session_cost_usd`) lives in
    the same module but is exercised separately under `tmp_home` so
    the aggregator tests don't touch disk.
  - Baseline = mean of per-ISO-week spend totals over the 90-day
    window, counting only ISO weeks with at least one priced session.
    Quiet weeks are not zero-padded; that rule is locked in by
    `test_baseline_excludes_zero_weeks`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from praxis.scoring.cost_ledger import (
    COST_BASELINE_WINDOW_DAYS,
    COST_CHARS_PER_TOKEN,
    COST_OUTPUT_TO_INPUT_RATIO,
    CostLedger,
    CostLedgerInputSession,
    compute_cost_ledger,
    estimate_session_cost_usd,
)


# A Wednesday so the current-week start is unambiguous (Mon two days
# prior). Mirrors test_baseline.py to keep the mental model identical
# across the cost / baseline panels.
AS_OF = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
CURRENT_WEEK_MONDAY = datetime(2026, 5, 25, tzinfo=timezone.utc).date()


def _session(days_ago: int, cost_usd: float | None = 1.0) -> CostLedgerInputSession:
    return CostLedgerInputSession(
        started_at=AS_OF - timedelta(days=days_ago),
        cost_usd=cost_usd,
    )


# ---- Aggregator tests --------------------------------------------------


def test_empty_returns_zero_ledger():
    ledger = compute_cost_ledger([], as_of=AS_OF)
    assert ledger.weekly_spend_usd == 0.0
    assert ledger.baseline_weekly_mean_usd == 0.0
    assert ledger.weekly_session_count == 0
    assert ledger.baseline_session_count == 0
    assert ledger.baseline_week_count == 0


def test_window_bounds_match_baseline_module():
    """Window mirrors compute_baseline's: 90 days back, current ISO week excluded."""
    ledger = compute_cost_ledger([], as_of=AS_OF)
    assert ledger.window_end == CURRENT_WEEK_MONDAY
    assert ledger.current_week_start == CURRENT_WEEK_MONDAY
    assert ledger.window_start == AS_OF.date() - timedelta(days=COST_BASELINE_WINDOW_DAYS)


def test_current_iso_week_session_contributes_to_weekly_spend():
    s = _session(days_ago=0, cost_usd=2.5)
    ledger = compute_cost_ledger([s], as_of=AS_OF)
    assert ledger.weekly_spend_usd == 2.5
    assert ledger.weekly_session_count == 1
    # Baseline window has no sessions.
    assert ledger.baseline_weekly_mean_usd == 0.0
    assert ledger.baseline_week_count == 0


def test_sessions_in_current_iso_week_are_excluded_from_baseline():
    """Sessions on/after Monday of as_of's week land in weekly, not baseline."""
    this_week = _session(days_ago=1, cost_usd=10.0)    # Tuesday this week
    on_monday = _session(days_ago=2, cost_usd=10.0)    # Monday boundary
    in_baseline = _session(days_ago=10, cost_usd=3.0)  # prior ISO week
    ledger = compute_cost_ledger([this_week, on_monday, in_baseline], as_of=AS_OF)
    assert ledger.weekly_session_count == 2
    assert ledger.weekly_spend_usd == 20.0
    assert ledger.baseline_session_count == 1
    assert ledger.baseline_weekly_mean_usd == 3.0


def test_sessions_older_than_90_days_are_excluded():
    too_old = _session(days_ago=95, cost_usd=99.0)
    in_window = _session(days_ago=80, cost_usd=4.0)
    ledger = compute_cost_ledger([too_old, in_window], as_of=AS_OF)
    assert ledger.baseline_session_count == 1
    assert ledger.baseline_weekly_mean_usd == 4.0


def test_window_start_is_inclusive():
    """A session exactly 90 days before as_of contributes to the baseline."""
    edge = _session(days_ago=COST_BASELINE_WINDOW_DAYS, cost_usd=2.0)
    ledger = compute_cost_ledger([edge], as_of=AS_OF)
    assert ledger.baseline_session_count == 1
    assert ledger.baseline_weekly_mean_usd == 2.0


def test_baseline_is_mean_over_distinct_iso_weeks():
    """Two ISO weeks with multiple sessions -> mean of per-week totals."""
    # ISO week A (Mon 2026-05-11 .. Sun 2026-05-17):
    a1 = _session(days_ago=10, cost_usd=2.0)   # Sun 2026-05-17
    a2 = _session(days_ago=12, cost_usd=4.0)   # Fri 2026-05-15
    # ISO week B (Mon 2026-05-04 .. Sun 2026-05-10):
    b1 = _session(days_ago=20, cost_usd=1.0)   # Thu 2026-05-07
    b2 = _session(days_ago=21, cost_usd=5.0)   # Wed 2026-05-06
    ledger = compute_cost_ledger([a1, a2, b1, b2], as_of=AS_OF)
    # Per-week totals: A=6.0, B=6.0. Mean = 6.0.
    assert ledger.baseline_week_count == 2
    assert ledger.baseline_session_count == 4
    assert ledger.baseline_weekly_mean_usd == 6.0


def test_baseline_excludes_zero_weeks():
    """Weeks with no priced sessions in the window do NOT contribute as zero.

    A user with sessions in two non-adjacent ISO weeks should see a
    baseline of the mean of those two weeks, not an average that
    pads in the silent weeks at $0. Otherwise sporadic users would
    see an artificially deflated baseline and every active week
    would read as 'over baseline'.
    """
    # days_ago=14 -> Wed 2026-05-13 (ISO week Mon 2026-05-11 .. Sun 2026-05-17).
    # days_ago=28 -> Wed 2026-04-29 (ISO week Mon 2026-04-27 .. Sun 2026-05-03).
    # Between them is one silent ISO week (Mon 2026-05-04 .. Sun 2026-05-10).
    s_recent = _session(days_ago=14, cost_usd=10.0)
    s_earlier = _session(days_ago=28, cost_usd=20.0)
    ledger = compute_cost_ledger([s_recent, s_earlier], as_of=AS_OF)
    assert ledger.baseline_week_count == 2
    assert ledger.baseline_weekly_mean_usd == 15.0  # (10+20)/2, not (10+0+20)/3 = 10


def test_unpriced_sessions_skipped():
    """`cost_usd=None` sessions are silently ignored in both windows."""
    priced = _session(days_ago=10, cost_usd=3.0)
    unpriced_baseline = _session(days_ago=12, cost_usd=None)
    unpriced_weekly = _session(days_ago=1, cost_usd=None)
    ledger = compute_cost_ledger(
        [priced, unpriced_baseline, unpriced_weekly], as_of=AS_OF,
    )
    assert ledger.baseline_session_count == 1
    assert ledger.baseline_weekly_mean_usd == 3.0
    assert ledger.weekly_session_count == 0
    assert ledger.weekly_spend_usd == 0.0


def test_only_unpriced_sessions_yields_zero_ledger():
    """A user only on subscription-only models sees zero across the board."""
    sessions = [
        _session(days_ago=10, cost_usd=None),
        _session(days_ago=0, cost_usd=None),
    ]
    ledger = compute_cost_ledger(sessions, as_of=AS_OF)
    assert ledger.baseline_weekly_mean_usd == 0.0
    assert ledger.weekly_spend_usd == 0.0
    assert ledger.baseline_week_count == 0
    assert ledger.weekly_session_count == 0


def test_weekly_spend_sums_across_priced_models():
    """Acceptance: total USD spend across all priced models for the week."""
    # Two sessions in this ISO week with different per-session costs --
    # i.e. the caller resolved them against different model cards.
    opus_today = _session(days_ago=0, cost_usd=0.5)
    sonnet_today = _session(days_ago=1, cost_usd=0.1)
    ledger = compute_cost_ledger([opus_today, sonnet_today], as_of=AS_OF)
    assert ledger.weekly_spend_usd == 0.6
    assert ledger.weekly_session_count == 2


def test_default_as_of_uses_now():
    """Smoke test: when as_of is None, the window anchors to today."""
    ledger = compute_cost_ledger([])
    today = datetime.now(timezone.utc).date()
    expected_week_start = today - timedelta(days=today.weekday())
    assert ledger.window_end == expected_week_start
    assert ledger.current_week_start == expected_week_start


def test_cost_ledger_is_immutable():
    """Frozen dataclass guards against downstream mutation."""
    ledger = compute_cost_ledger([], as_of=AS_OF)
    assert isinstance(ledger, CostLedger)
    try:
        ledger.weekly_spend_usd = 99.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("CostLedger should be frozen")


# ---- Per-session cost helper tests -------------------------------------
# These exercise the card-resolution helper that produces
# CostLedgerInputSession inputs. They run under tmp_home so the
# user-cards directory is sandboxed.


def test_estimate_session_cost_returns_none_for_no_card(tmp_home):
    assert estimate_session_cost_usd("unknown-model-xyz", 1000) is None


def test_estimate_session_cost_returns_none_for_subscription_card(tmp_home):
    """Copilot has no per-token pricing -> None signals 'unpriced'."""
    assert estimate_session_cost_usd("copilot", 1000) is None


def test_estimate_session_cost_returns_none_for_no_hint(tmp_home):
    assert estimate_session_cost_usd(None, 1000) is None


def test_estimate_session_cost_zero_chars_is_zero(tmp_home):
    """A priced model with no input still returns 0.0 -- priced, just empty."""
    assert estimate_session_cost_usd("claude-opus-4-7", 0) == 0.0


def test_estimate_session_cost_uses_card_pricing(tmp_home):
    """Opus card: $15/M input, $75/M output. 4 chars/token, 1.5x output ratio.

    4000 chars -> 1000 input tokens -> 1500 output tokens.
    input cost = 1000 * 15 / 1M = $0.015
    output cost = 1500 * 75 / 1M = $0.1125
    total = $0.1275
    """
    cost = estimate_session_cost_usd("claude-opus-4-7", 4000)
    assert cost is not None
    assert abs(cost - 0.1275) < 1e-6


def test_estimate_session_cost_constants_match_advisor(tmp_home):
    """Constants stay in lockstep with `models_advisor/advisor.py`."""
    assert COST_CHARS_PER_TOKEN == 4.0
    assert COST_OUTPUT_TO_INPUT_RATIO == 1.5
