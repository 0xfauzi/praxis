"""Tests for the weekly cost ledger (US-047) and biggest-line panel (US-048).

Acceptance criteria from the PRD:
  - US-047: Total USD spend across all priced models for the week is
    computed; 90-day rolling weekly mean is computed alongside.
  - US-048: The (model, task) pair with the largest spend in the
    week is identified; spend amount and session count are exposed.

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
  - `compute_biggest_line` is pure too: it groups by (model_hint,
    task_label) within the current ISO week only, skips sessions with
    a missing identity or unknown cost, and tiebreaks deterministically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from praxis.scoring.cost_ledger import (
    COST_BASELINE_WINDOW_DAYS,
    COST_CHARS_PER_TOKEN,
    COST_OUTPUT_TO_INPUT_RATIO,
    BiggestLine,
    BiggestLineInputSession,
    CostLedger,
    CostLedgerInputSession,
    compute_biggest_line,
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


# ---- Biggest (model, task) line tests ----------------------------------
# Spec section 10.1: the cost panel names the (model, task) pair that
# drove the week's spend. compute_biggest_line is the same shape as
# compute_cost_ledger -- pure aggregator over (model_hint, task_label,
# cost_usd, started_at) inputs. The current ISO week is the window;
# the baseline weeks are out of scope (CostLedger already reports them).


def _biggest_session(
    days_ago: int,
    cost_usd: float | None = 1.0,
    model_hint: str | None = "claude-opus-4-7",
    task_label: str | None = "auth migration debugging",
) -> BiggestLineInputSession:
    return BiggestLineInputSession(
        started_at=AS_OF - timedelta(days=days_ago),
        cost_usd=cost_usd,
        model_hint=model_hint,
        task_label=task_label,
    )


def test_biggest_line_empty_returns_no_winner():
    line = compute_biggest_line([], as_of=AS_OF)
    assert isinstance(line, BiggestLine)
    assert line.model_hint is None
    assert line.task_label is None
    assert line.spend_usd == 0.0
    assert line.session_count == 0


def test_biggest_line_single_session_wins():
    s = _biggest_session(days_ago=0, cost_usd=2.5, model_hint="opus", task_label="task A")
    line = compute_biggest_line([s], as_of=AS_OF)
    assert line.model_hint == "opus"
    assert line.task_label == "task A"
    assert line.spend_usd == 2.5
    assert line.session_count == 1


def test_biggest_line_sums_within_pair_then_picks_max():
    """Acceptance: identify the (model, task) pair with the LARGEST spend.

    Two pairs in-week: (opus, A) has 2 sessions summing to $3, (sonnet, B)
    has 1 session at $5. Sonnet/B wins on spend even though Opus/A has
    more sessions -- spend is the primary criterion.
    """
    sessions = [
        _biggest_session(days_ago=0, cost_usd=1.0, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=1, cost_usd=2.0, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=2, cost_usd=5.0, model_hint="sonnet", task_label="B"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "sonnet"
    assert line.task_label == "B"
    assert line.spend_usd == 5.0
    assert line.session_count == 1


def test_biggest_line_session_count_exposed_for_winner():
    """Acceptance: spend amount AND session count are exposed to renderer.

    Winning pair has three sessions; session_count must reflect just
    the winner's sessions, not the total weekly session count.
    """
    sessions = [
        _biggest_session(days_ago=0, cost_usd=1.0, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=1, cost_usd=1.0, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=2, cost_usd=1.0, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=0, cost_usd=2.0, model_hint="sonnet", task_label="B"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "opus"
    assert line.task_label == "A"
    assert line.spend_usd == 3.0
    assert line.session_count == 3


def test_biggest_line_skips_sessions_without_model_hint():
    """A session with model_hint=None cannot be named in the panel -> skipped."""
    sessions = [
        _biggest_session(days_ago=0, cost_usd=10.0, model_hint=None, task_label="A"),
        _biggest_session(days_ago=1, cost_usd=1.0, model_hint="opus", task_label="A"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "opus"
    assert line.task_label == "A"
    assert line.spend_usd == 1.0
    assert line.session_count == 1


def test_biggest_line_skips_sessions_without_task_label():
    """A session with task_label=None cannot be named in the panel -> skipped."""
    sessions = [
        _biggest_session(days_ago=0, cost_usd=10.0, model_hint="opus", task_label=None),
        _biggest_session(days_ago=1, cost_usd=1.0, model_hint="opus", task_label="A"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "opus"
    assert line.task_label == "A"


def test_biggest_line_skips_unpriced_sessions():
    """`cost_usd=None` sessions are silently ignored (same rule as compute_cost_ledger)."""
    sessions = [
        _biggest_session(days_ago=0, cost_usd=None, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=1, cost_usd=2.0, model_hint="sonnet", task_label="B"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "sonnet"
    assert line.task_label == "B"


def test_biggest_line_ignores_baseline_window_sessions():
    """Sessions in the 90-day baseline window but before the current ISO week are out of scope.

    Per spec section 10.1 the 'biggest line' is for the current week.
    The baseline mean covers the historical span; this panel does not.
    """
    sessions = [
        # In current week:
        _biggest_session(days_ago=0, cost_usd=1.0, model_hint="opus", task_label="A"),
        # In baseline window only -- must be ignored even though larger:
        _biggest_session(days_ago=20, cost_usd=99.0, model_hint="sonnet", task_label="B"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "opus"
    assert line.task_label == "A"
    assert line.spend_usd == 1.0


def test_biggest_line_no_current_week_sessions_returns_empty():
    """If every priced+identified session is in a prior ISO week, no winner."""
    sessions = [
        _biggest_session(days_ago=10, cost_usd=5.0, model_hint="opus", task_label="A"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint is None
    assert line.task_label is None
    assert line.spend_usd == 0.0
    assert line.session_count == 0


def test_biggest_line_current_week_boundary_is_monday_inclusive():
    """A session at 00:00 on the current ISO week's Monday is in-week."""
    monday_session = BiggestLineInputSession(
        started_at=datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),  # Mon 00:00
        cost_usd=4.0,
        model_hint="opus",
        task_label="A",
    )
    line = compute_biggest_line([monday_session], as_of=AS_OF)
    assert line.model_hint == "opus"
    assert line.spend_usd == 4.0


def test_biggest_line_tiebreak_by_session_count_then_alphabetical():
    """When two pairs have equal spend, more sessions wins; then alphabetical.

    The exact tiebreak rule is implementation choice; this test locks it
    in so renderers + the spec stay deterministic across runs.
    """
    sessions = [
        # Pair (opus, A): one session at $4.
        _biggest_session(days_ago=0, cost_usd=4.0, model_hint="opus", task_label="A"),
        # Pair (sonnet, B): two sessions summing to $4 -- same spend, more sessions.
        _biggest_session(days_ago=1, cost_usd=2.0, model_hint="sonnet", task_label="B"),
        _biggest_session(days_ago=2, cost_usd=2.0, model_hint="sonnet", task_label="B"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "sonnet"
    assert line.task_label == "B"
    assert line.session_count == 2


def test_biggest_line_tiebreak_alphabetical_when_spend_and_count_match():
    """Final tier: pure alphabetical on (model_hint, task_label)."""
    sessions = [
        _biggest_session(days_ago=0, cost_usd=3.0, model_hint="opus", task_label="zeta"),
        _biggest_session(days_ago=1, cost_usd=3.0, model_hint="opus", task_label="alpha"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    assert line.model_hint == "opus"
    assert line.task_label == "alpha"


def test_biggest_line_rounds_spend_consistently_with_cost_ledger():
    """Spend USD rounded to 4 decimals so the panel doesn't show 8.99999..."""
    sessions = [
        _biggest_session(days_ago=0, cost_usd=1.0 / 3, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=1, cost_usd=1.0 / 3, model_hint="opus", task_label="A"),
        _biggest_session(days_ago=2, cost_usd=1.0 / 3, model_hint="opus", task_label="A"),
    ]
    line = compute_biggest_line(sessions, as_of=AS_OF)
    # Exact rounding parity with CostLedger.weekly_spend_usd.
    assert line.spend_usd == round(1.0, 4)


def test_biggest_line_is_immutable():
    """Frozen dataclass guards against downstream mutation."""
    line = compute_biggest_line([], as_of=AS_OF)
    try:
        line.spend_usd = 99.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("BiggestLine should be frozen")


def test_biggest_line_default_as_of_uses_now():
    """Smoke test: when as_of is None, the current-week boundary anchors to today."""
    line = compute_biggest_line([])
    assert line.model_hint is None
    assert line.spend_usd == 0.0
