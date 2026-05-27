"""Tests for the weekly cost ledger (US-047), biggest-line panel (US-048),
tier-fit savings estimate (US-049), and per-moment dollar impact (US-050).

Acceptance criteria from the PRD:
  - US-047: Total USD spend across all priced models for the week is
    computed; 90-day rolling weekly mean is computed alongside.
  - US-048: The (model, task) pair with the largest spend in the
    week is identified; spend amount and session count are exposed.
  - US-049: Opus/GPT-5 sessions with user_turn_count <= 3 AND
    avg_prompt_chars <= 200 are counted; savings estimate sums
    (frontier_cost - cheaper_tier_cost) per qualifying session.
  - US-050: dollar_impact_estimate per moment type per spec 10.2 --
    verification = next-2-turn cost, fit = frontier-minus-cheaper-tier
    savings, iteration = accepted-response cost, planning/context/tools
    = None.

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
  - `compute_tier_fit_savings` is pure: pre-computed per-session
    savings + threshold-signal fields in, total out. The card lookup
    happens in `estimate_tier_fit_savings_for_session`, exercised
    under tmp_home like the other helper.
  - `compute_moment_dollar_impact_usd` dispatches by dim_key to the
    existing per-session helpers, returns None for the dims the spec
    explicitly opts out of (planning/context/tools), and returns None
    when the caller has no signal to attribute.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from praxis.scoring.cost_ledger import (
    COST_BASELINE_WINDOW_DAYS,
    COST_CHARS_PER_TOKEN,
    COST_OUTPUT_TO_INPUT_RATIO,
    TIER_FIT_MAX_AVG_PROMPT_CHARS,
    TIER_FIT_MAX_USER_TURNS,
    BiggestLine,
    BiggestLineInputSession,
    CostLedger,
    CostLedgerInputSession,
    TierFitInputSession,
    TierFitSavings,
    compute_biggest_line,
    compute_cost_ledger,
    compute_moment_dollar_impact_usd,
    compute_tier_fit_savings,
    estimate_session_cost_usd,
    estimate_tier_fit_savings_for_session,
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


# ---- Tier-fit savings tests (US-049) -----------------------------------
# Spec section 10.1: surface 'you could have paid less' for frontier
# sessions whose workload was small enough that a fast-tier sibling
# would have done the job. compute_tier_fit_savings is a pure aggregator
# over (qualifying-threshold signals + pre-computed per-session savings)
# inputs. The card lookup that produces those per-session savings is
# isolated in estimate_tier_fit_savings_for_session.


def _tierfit_session(
    days_ago: int,
    user_turn_count: int = 1,
    avg_prompt_chars: float = 50.0,
    tier_fit_savings_usd: float | None = 0.5,
) -> TierFitInputSession:
    return TierFitInputSession(
        started_at=AS_OF - timedelta(days=days_ago),
        user_turn_count=user_turn_count,
        avg_prompt_chars=avg_prompt_chars,
        tier_fit_savings_usd=tier_fit_savings_usd,
    )


def test_tier_fit_thresholds_are_exposed_as_constants():
    """The qualifying thresholds are module-level constants so the spec and tests stay in lockstep."""
    assert TIER_FIT_MAX_USER_TURNS == 3
    assert TIER_FIT_MAX_AVG_PROMPT_CHARS == 200


def test_tier_fit_empty_returns_zero_savings():
    out = compute_tier_fit_savings([], as_of=AS_OF)
    assert isinstance(out, TierFitSavings)
    assert out.qualifying_session_count == 0
    assert out.estimated_savings_usd == 0.0


def test_tier_fit_single_qualifying_session_counted():
    """Acceptance: an Opus session with 2 turns and 100-char avg counts; savings exposed."""
    s = _tierfit_session(days_ago=0, user_turn_count=2, avg_prompt_chars=100.0,
                         tier_fit_savings_usd=1.25)
    out = compute_tier_fit_savings([s], as_of=AS_OF)
    assert out.qualifying_session_count == 1
    assert out.estimated_savings_usd == 1.25


def test_tier_fit_skips_sessions_with_too_many_turns():
    """user_turn_count > 3 means the workload was likely too rich for the fast tier."""
    s = _tierfit_session(days_ago=0, user_turn_count=4, avg_prompt_chars=50.0,
                         tier_fit_savings_usd=1.0)
    out = compute_tier_fit_savings([s], as_of=AS_OF)
    assert out.qualifying_session_count == 0
    assert out.estimated_savings_usd == 0.0


def test_tier_fit_skips_sessions_with_long_prompts():
    """avg_prompt_chars > 200 means the prompt was probably substantive enough to need frontier."""
    s = _tierfit_session(days_ago=0, user_turn_count=1, avg_prompt_chars=201.0,
                         tier_fit_savings_usd=1.0)
    out = compute_tier_fit_savings([s], as_of=AS_OF)
    assert out.qualifying_session_count == 0


def test_tier_fit_both_thresholds_must_be_met():
    """AND, not OR: a session with <=3 turns OR <=200 chars (but not both) does NOT qualify."""
    long_prompts = _tierfit_session(days_ago=0, user_turn_count=1, avg_prompt_chars=500.0,
                                    tier_fit_savings_usd=1.0)
    many_turns = _tierfit_session(days_ago=1, user_turn_count=10, avg_prompt_chars=50.0,
                                  tier_fit_savings_usd=1.0)
    out = compute_tier_fit_savings([long_prompts, many_turns], as_of=AS_OF)
    assert out.qualifying_session_count == 0


def test_tier_fit_thresholds_are_inclusive():
    """`<=` not `<`: a session sitting exactly on the boundary qualifies."""
    edge_turns = _tierfit_session(days_ago=0, user_turn_count=TIER_FIT_MAX_USER_TURNS,
                                  avg_prompt_chars=50.0, tier_fit_savings_usd=0.5)
    edge_chars = _tierfit_session(days_ago=1, user_turn_count=1,
                                  avg_prompt_chars=float(TIER_FIT_MAX_AVG_PROMPT_CHARS),
                                  tier_fit_savings_usd=0.5)
    out = compute_tier_fit_savings([edge_turns, edge_chars], as_of=AS_OF)
    assert out.qualifying_session_count == 2
    assert out.estimated_savings_usd == 1.0


def test_tier_fit_skips_sessions_without_savings_signal():
    """`tier_fit_savings_usd=None` -> the caller said 'no frontier+fast pairing here'."""
    qualifying_turns_but_no_savings = _tierfit_session(
        days_ago=0, user_turn_count=1, avg_prompt_chars=50.0,
        tier_fit_savings_usd=None,
    )
    out = compute_tier_fit_savings([qualifying_turns_but_no_savings], as_of=AS_OF)
    assert out.qualifying_session_count == 0
    assert out.estimated_savings_usd == 0.0


def test_tier_fit_zero_savings_still_counts_as_qualifying():
    """A frontier session with 0-char workload qualifies; the savings contribution is just 0.

    Skipping it would make qualifying_session_count drift from the
    'how many frontier-tiny sessions did you run this week' question.
    """
    s = _tierfit_session(days_ago=0, user_turn_count=1, avg_prompt_chars=0.0,
                         tier_fit_savings_usd=0.0)
    out = compute_tier_fit_savings([s], as_of=AS_OF)
    assert out.qualifying_session_count == 1
    assert out.estimated_savings_usd == 0.0


def test_tier_fit_sums_savings_across_qualifying_sessions():
    """Acceptance: savings estimate uses (frontier - cheaper_tier) per session, summed."""
    sessions = [
        _tierfit_session(days_ago=0, tier_fit_savings_usd=0.10),
        _tierfit_session(days_ago=1, tier_fit_savings_usd=0.25),
        _tierfit_session(days_ago=2, tier_fit_savings_usd=0.65),
    ]
    out = compute_tier_fit_savings(sessions, as_of=AS_OF)
    assert out.qualifying_session_count == 3
    assert out.estimated_savings_usd == 1.0


def test_tier_fit_ignores_baseline_window_sessions():
    """The panel reports current ISO week only -- prior weeks are out of scope."""
    sessions = [
        _tierfit_session(days_ago=0, tier_fit_savings_usd=0.10),    # this week
        _tierfit_session(days_ago=10, tier_fit_savings_usd=99.0),   # baseline window, ignored
        _tierfit_session(days_ago=30, tier_fit_savings_usd=99.0),   # ditto
    ]
    out = compute_tier_fit_savings(sessions, as_of=AS_OF)
    assert out.qualifying_session_count == 1
    assert out.estimated_savings_usd == 0.10


def test_tier_fit_current_week_monday_inclusive():
    """A frontier session at 00:00 on the current ISO week's Monday qualifies."""
    monday_session = TierFitInputSession(
        started_at=datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
        user_turn_count=1,
        avg_prompt_chars=50.0,
        tier_fit_savings_usd=0.40,
    )
    out = compute_tier_fit_savings([monday_session], as_of=AS_OF)
    assert out.qualifying_session_count == 1
    assert out.estimated_savings_usd == 0.40


def test_tier_fit_savings_rounded_to_four_decimals():
    """Parity with CostLedger.weekly_spend_usd and BiggestLine.spend_usd rounding."""
    sessions = [_tierfit_session(days_ago=0, tier_fit_savings_usd=1.0 / 3) for _ in range(3)]
    out = compute_tier_fit_savings(sessions, as_of=AS_OF)
    assert out.estimated_savings_usd == round(1.0, 4)


def test_tier_fit_is_immutable():
    """Frozen dataclass guards against downstream mutation."""
    out = compute_tier_fit_savings([], as_of=AS_OF)
    try:
        out.estimated_savings_usd = 99.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("TierFitSavings should be frozen")


def test_tier_fit_default_as_of_uses_now():
    """Smoke test: when as_of is None, the current-week boundary anchors to today."""
    out = compute_tier_fit_savings([])
    assert out.qualifying_session_count == 0
    assert out.estimated_savings_usd == 0.0


# ---- Tier-fit per-session helper tests ---------------------------------
# These exercise `estimate_tier_fit_savings_for_session`, which resolves
# the frontier card + fast-tier sibling and computes the per-session
# savings under the same chars/output assumptions as the cost ledger.
# Run under tmp_home so the user-cards directory is sandboxed.


def test_estimate_tier_fit_savings_returns_none_for_unknown_model(tmp_home):
    assert estimate_tier_fit_savings_for_session("unknown-model-xyz", 1000) is None


def test_estimate_tier_fit_savings_returns_none_for_no_hint(tmp_home):
    assert estimate_tier_fit_savings_for_session(None, 1000) is None


def test_estimate_tier_fit_savings_returns_none_for_non_frontier(tmp_home):
    """Haiku is fast-tier, not frontier -- no savings to claim by routing 'down'."""
    assert estimate_tier_fit_savings_for_session("claude-haiku-4-5", 1000) is None


def test_estimate_tier_fit_savings_returns_none_for_balanced_tier(tmp_home):
    """Sonnet is balanced-tier -- the panel is about frontier-only over-spend."""
    assert estimate_tier_fit_savings_for_session("claude-sonnet-4-6", 1000) is None


def test_estimate_tier_fit_savings_returns_none_for_subscription_card(tmp_home):
    """Copilot has no per-token pricing and isn't frontier-tier anyway."""
    assert estimate_tier_fit_savings_for_session("copilot", 1000) is None


def test_estimate_tier_fit_savings_zero_chars_is_zero(tmp_home):
    """A frontier session with no input still qualifies; savings is just $0."""
    assert estimate_tier_fit_savings_for_session("claude-opus-4-7", 0) == 0.0


def test_estimate_tier_fit_savings_opus_to_haiku_pricing(tmp_home):
    """Opus -> Haiku savings for 4000 input chars.

    Opus card: $15/M input, $75/M output. Haiku card: $1/M input, $5/M output.
    4000 chars -> 1000 input tokens -> 1500 output tokens.
    Opus cost   = 1000 * 15 / 1M + 1500 * 75 / 1M    = $0.015 + $0.1125 = $0.1275
    Haiku cost  = 1000 * 1  / 1M + 1500 * 5  / 1M    = $0.001 + $0.0075 = $0.0085
    Savings     = $0.1275 - $0.0085                                       = $0.119
    """
    savings = estimate_tier_fit_savings_for_session("claude-opus-4-7", 4000)
    assert savings is not None
    assert abs(savings - 0.119) < 1e-6


def test_estimate_tier_fit_savings_gpt5_to_gpt5_mini_pricing(tmp_home):
    """GPT-5 -> GPT-5 mini savings for 4000 input chars.

    GPT-5 card: $1.25/M input, $10/M output. GPT-5 mini: $0.25/M input, $2/M output.
    4000 chars -> 1000 input tokens -> 1500 output tokens.
    GPT-5 cost    = 1000 * 1.25 / 1M + 1500 * 10 / 1M = $0.00125 + $0.015  = $0.01625
    Mini cost     = 1000 * 0.25 / 1M + 1500 * 2  / 1M = $0.00025 + $0.003  = $0.00325
    Savings       = $0.01625 - $0.00325                                     = $0.013
    """
    savings = estimate_tier_fit_savings_for_session("gpt-5", 4000)
    assert savings is not None
    assert abs(savings - 0.013) < 1e-6


def test_estimate_tier_fit_savings_gemini_returns_none_no_fast_sibling(tmp_home):
    """Gemini ships a frontier card but no fast-tier card in the family -> not comparable.

    If a user adds a Gemini fast-tier card to ~/.praxis/model_cards/ later,
    Gemini sessions would naturally start counting. Locking this in so the
    behaviour stays predictable until that card exists.
    """
    assert estimate_tier_fit_savings_for_session("gemini-2-5-pro", 4000) is None


def test_estimate_tier_fit_savings_resolves_aliased_hints(tmp_home):
    """Aliased model hints route to the same frontier card as `find_card_for_model_hint`.

    Exercises the alias path through `find_card_for_model_hint`. The
    specific aliases ('claude-opus', 'gpt5') are read off the built-in
    cards; if those change the assertion still checks 'the resolver is
    actually consulted' rather than a literal string.
    """
    assert estimate_tier_fit_savings_for_session("claude-opus", 4000) is not None
    assert estimate_tier_fit_savings_for_session("gpt5", 4000) is not None


def test_estimate_tier_fit_savings_uses_shared_token_constants(tmp_home):
    """The helper uses the same chars-per-token + output ratio as estimate_session_cost_usd.

    Proven indirectly: the Opus frontier_cost half of the savings equals
    estimate_session_cost_usd('claude-opus-4-7', chars), so frontier minus
    haiku cost equals savings.
    """
    frontier = estimate_session_cost_usd("claude-opus-4-7", 4000)
    haiku = estimate_session_cost_usd("claude-haiku-4-5", 4000)
    savings = estimate_tier_fit_savings_for_session("claude-opus-4-7", 4000)
    assert frontier is not None and haiku is not None and savings is not None
    assert abs(savings - (frontier - haiku)) < 1e-9


# ---- Per-moment dollar impact tests (US-050) ---------------------------
# Spec section 10.2: dollar_impact_estimate per moment type. The
# dispatcher delegates to estimate_session_cost_usd /
# estimate_tier_fit_savings_for_session, so these tests run under
# tmp_home for card lookups. They assert each dim_key path, the
# None-for-no-signal contract, and the planning/context/tools "we do
# not invent a number" rule.


def test_moment_impact_planning_is_none(tmp_home):
    """Planning moments never carry a dollar impact (spec 10.2)."""
    assert compute_moment_dollar_impact_usd(
        "planning", "claude-opus-4-7",
        next_two_turn_chars=10_000,
        accepted_response_chars=10_000,
        session_total_input_chars=10_000,
    ) is None


def test_moment_impact_context_is_none(tmp_home):
    """Context moments never carry a dollar impact (spec 10.2)."""
    assert compute_moment_dollar_impact_usd(
        "context", "claude-opus-4-7",
        next_two_turn_chars=10_000,
        accepted_response_chars=10_000,
        session_total_input_chars=10_000,
    ) is None


def test_moment_impact_tools_is_none(tmp_home):
    """Tools moments never carry a dollar impact (spec 10.2)."""
    assert compute_moment_dollar_impact_usd(
        "tools", "claude-opus-4-7",
        next_two_turn_chars=10_000,
        accepted_response_chars=10_000,
        session_total_input_chars=10_000,
    ) is None


def test_moment_impact_unknown_dim_key_is_none(tmp_home):
    """Defensive default for any dim_key not named in the spec."""
    assert compute_moment_dollar_impact_usd(
        "structured_output", "claude-opus-4-7",
        next_two_turn_chars=4000,
    ) is None


def test_moment_impact_verification_uses_next_two_turn_chars(tmp_home):
    """Acceptance: verification = token cost of the next 2 turns after the lapse.

    Mirrors `estimate_session_cost_usd("claude-opus-4-7", 4000)` so the
    per-moment number and the weekly ledger agree on the same token
    formula.
    """
    expected = estimate_session_cost_usd("claude-opus-4-7", 4000)
    got = compute_moment_dollar_impact_usd(
        "verification", "claude-opus-4-7", next_two_turn_chars=4000,
    )
    assert expected is not None and got is not None
    assert abs(got - expected) < 1e-9


def test_moment_impact_verification_returns_none_without_signal(tmp_home):
    """Caller passed no `next_two_turn_chars` -> no impact attributable."""
    assert compute_moment_dollar_impact_usd(
        "verification", "claude-opus-4-7",
    ) is None


def test_moment_impact_verification_returns_none_for_unknown_model(tmp_home):
    """No card on file -> the underlying helper returns None -> we surface that."""
    assert compute_moment_dollar_impact_usd(
        "verification", "unknown-model-xyz", next_two_turn_chars=4000,
    ) is None


def test_moment_impact_verification_returns_none_for_no_model_hint(tmp_home):
    """model_hint=None -> can't price it."""
    assert compute_moment_dollar_impact_usd(
        "verification", None, next_two_turn_chars=4000,
    ) is None


def test_moment_impact_verification_returns_none_for_subscription_card(tmp_home):
    """Copilot has no per-token pricing -> None signals 'unpriced'."""
    assert compute_moment_dollar_impact_usd(
        "verification", "copilot", next_two_turn_chars=4000,
    ) is None


def test_moment_impact_iteration_uses_accepted_response_chars(tmp_home):
    """Acceptance: iteration = token cost of the assistant response the user accepted prematurely.

    Same chars-per-token + output-multiplier as verification + the
    weekly ledger.
    """
    expected = estimate_session_cost_usd("claude-opus-4-7", 800)
    got = compute_moment_dollar_impact_usd(
        "iteration", "claude-opus-4-7", accepted_response_chars=800,
    )
    assert expected is not None and got is not None
    assert abs(got - expected) < 1e-9


def test_moment_impact_iteration_returns_none_without_signal(tmp_home):
    """Caller passed no `accepted_response_chars` -> nothing to attribute."""
    assert compute_moment_dollar_impact_usd(
        "iteration", "claude-opus-4-7",
    ) is None


def test_moment_impact_iteration_returns_none_for_unknown_model(tmp_home):
    assert compute_moment_dollar_impact_usd(
        "iteration", "unknown-model-xyz", accepted_response_chars=800,
    ) is None


def test_moment_impact_fit_uses_session_total_input_chars(tmp_home):
    """Acceptance: fit (over-tier) = (frontier_cost - cheaper_tier_cost) for the session.

    Equals `estimate_tier_fit_savings_for_session` on the same inputs,
    so the per-moment fit estimate and the tier-fit panel agree.
    """
    expected = estimate_tier_fit_savings_for_session("claude-opus-4-7", 4000)
    got = compute_moment_dollar_impact_usd(
        "fit", "claude-opus-4-7", session_total_input_chars=4000,
    )
    assert expected is not None and got is not None
    assert abs(got - expected) < 1e-9


def test_moment_impact_fit_returns_none_without_signal(tmp_home):
    """Caller passed no `session_total_input_chars` -> nothing to attribute."""
    assert compute_moment_dollar_impact_usd(
        "fit", "claude-opus-4-7",
    ) is None


def test_moment_impact_fit_returns_none_for_non_frontier(tmp_home):
    """A fit moment on a fast-tier model has no cheaper sibling to compare against.

    The underlying helper returns None for non-frontier cards;
    `compute_moment_dollar_impact_usd` surfaces that None.
    """
    assert compute_moment_dollar_impact_usd(
        "fit", "claude-haiku-4-5", session_total_input_chars=4000,
    ) is None


def test_moment_impact_fit_returns_none_for_no_fast_sibling(tmp_home):
    """Gemini ships frontier-only in the built-in cards -> no savings to claim."""
    assert compute_moment_dollar_impact_usd(
        "fit", "gemini-2-5-pro", session_total_input_chars=4000,
    ) is None


def test_moment_impact_only_relevant_kwarg_consulted(tmp_home):
    """A verification moment ignores the `accepted_response_chars` /
    `session_total_input_chars` kwargs even when they are populated.

    Guards against future refactors that accidentally cross-wire the
    dim_key dispatch. Each dim_key has exactly one chars input that
    matters; the others are noise.
    """
    only_verification = compute_moment_dollar_impact_usd(
        "verification", "claude-opus-4-7",
        next_two_turn_chars=4000,
        accepted_response_chars=999_999,
        session_total_input_chars=999_999,
    )
    just_verification = compute_moment_dollar_impact_usd(
        "verification", "claude-opus-4-7", next_two_turn_chars=4000,
    )
    assert only_verification == just_verification


def test_moment_impact_verification_zero_chars_is_zero(tmp_home):
    """A priced model with no chars -> $0, not None.

    Zero is a legitimate signal ('the user redid no work') and matches
    `estimate_session_cost_usd`'s 0-chars-is-zero contract.
    """
    assert compute_moment_dollar_impact_usd(
        "verification", "claude-opus-4-7", next_two_turn_chars=0,
    ) == 0.0


def test_moment_impact_iteration_zero_chars_is_zero(tmp_home):
    """Same 0-chars-is-zero contract for iteration moments."""
    assert compute_moment_dollar_impact_usd(
        "iteration", "claude-opus-4-7", accepted_response_chars=0,
    ) == 0.0


def test_moment_impact_fit_zero_chars_is_zero(tmp_home):
    """Frontier session with empty workload qualifies; savings is just $0."""
    assert compute_moment_dollar_impact_usd(
        "fit", "claude-opus-4-7", session_total_input_chars=0,
    ) == 0.0
