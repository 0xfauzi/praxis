"""Weekly cost ledger.

Per spec section 10.1 the cost panel of the weekly digest shows:
  - This week's spend: total estimated USD across all priced models.
  - Baseline: 90-day rolling weekly mean.

Costs are rough estimates from token volume + tier pricing (spec
section 10.2). Sessions on subscription-only models (Copilot) and
sessions whose model_hint resolves to no card carry no cost
contribution -- they are skipped, not zero-imputed.

The 90-day baseline is the mean of per-ISO-week spend totals over
the window, counting only ISO weeks that contained at least one
priced session. Weeks with no priced activity are not folded in as
zero: otherwise a user who took time off would see an artificially
deflated baseline and every active week would read as 'over
baseline', which defeats the comparison.

Like `praxis.scoring.baseline`, this module is pure. Pass in
pre-computed per-session costs and an `as_of` moment, get back a
CostLedger value. Resolving model_hint -> cost lives in
`estimate_session_cost_usd` so the aggregator stays decoupled from
disk I/O and the model-card system.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from praxis.models_advisor.cards import find_card_for_model_hint


# 90 days mirrors the engagement baseline (spec section 8.2): long
# enough to be a real anchor, short enough to reflect the user's
# current era.
COST_BASELINE_WINDOW_DAYS = 90

# Rough estimate constants. Kept in lockstep with
# `praxis.models_advisor.advisor._estimate_cost` so the per-session
# cost shown next to a model's profile and the per-session cost folded
# into the weekly ledger agree. The disclaimer that lands in the
# rendered digest (spec section 10.2) is intentionally explicit so
# users do not read these numbers as invoiced costs.
COST_CHARS_PER_TOKEN = 4.0
COST_OUTPUT_TO_INPUT_RATIO = 1.5


@dataclass(frozen=True)
class CostLedgerInputSession:
    """One session's contribution to the cost ledger.

    Decoupled from Session and SessionScore so the aggregator stays
    pure: callers may compute `cost_usd` from any source (live token
    counts, billing API, the heuristic in
    `estimate_session_cost_usd`). `cost_usd=None` marks a session
    whose cost is unknown -- no card on file, or a subscription-only
    model with no per-token rates. Such sessions are skipped, not
    counted as zero, so the weekly total and the baseline both
    reflect 'priced spend' only.
    """

    started_at: datetime
    cost_usd: float | None


@dataclass(frozen=True)
class BiggestLineInputSession:
    """One session's contribution to the biggest-(model, task) line.

    Adds model + task identity to the per-session cost so the panel
    can name a specific pair. `model_hint` is the resolved model
    string (e.g. 'claude-opus-4-7'); `task_label` is the human-readable
    cluster label produced by `praxis.scoring.clustering` (e.g. 'auth
    migration debugging'). Sessions with a missing model_hint, missing
    task_label, or `cost_usd=None` are skipped: the panel exists to
    point at a concrete (model, task) pair the user can recognize,
    so an 'unknown / unknown' bucket would be a regression in clarity,
    not a fallback.
    """

    started_at: datetime
    cost_usd: float | None
    model_hint: str | None
    task_label: str | None


@dataclass(frozen=True)
class BiggestLine:
    """The (model, task) pair that drove the largest spend in the current ISO week.

    `model_hint` and `task_label` are None (and `spend_usd`/
    `session_count` are 0) when no qualifying sessions exist in the
    current ISO week -- the renderer omits the panel row in that case
    rather than printing a misleading 'no winner' line.
    """

    model_hint: str | None
    task_label: str | None
    spend_usd: float
    session_count: int


@dataclass(frozen=True)
class CostLedger:
    """Inputs for the weekly cost panel.

    `baseline_weekly_mean_usd` is the mean of per-ISO-week spend
    totals over the 90-day window, counting only weeks with at least
    one priced session (see module docstring for the rationale). The
    `*_session_count` fields count priced sessions only -- unpriced
    sessions on subscription models are not folded in. The window
    boundaries are exposed so renderers can show the date range and
    the comparison anchor without re-deriving them.
    """

    weekly_spend_usd: float
    baseline_weekly_mean_usd: float
    weekly_session_count: int
    baseline_session_count: int
    baseline_week_count: int
    window_start: date    # inclusive: as_of_date - 90 days
    window_end: date      # exclusive: Monday of current ISO week
    current_week_start: date  # = window_end


def _iso_week_start(d: date) -> date:
    """The Monday of the ISO week containing d."""
    return d - timedelta(days=d.weekday())


def estimate_session_cost_usd(
    model_hint: str | None,
    total_input_chars: int,
) -> float | None:
    """Rough USD cost for one session from input char volume + card pricing.

    Returns None when the model has no card or the card lacks
    per-token pricing (e.g. subscription-only Copilot). Matches the
    assumptions documented in
    `praxis.models_advisor.advisor._estimate_cost`: ~4 chars/token,
    output tokens ~= 1.5x input, no caching/batch discounts.
    """
    card = find_card_for_model_hint(model_hint)
    if card is None:
        return None
    if card.input_per_million_usd is None or card.output_per_million_usd is None:
        return None
    if total_input_chars <= 0:
        return 0.0
    input_tokens = total_input_chars / COST_CHARS_PER_TOKEN
    output_tokens = input_tokens * COST_OUTPUT_TO_INPUT_RATIO
    return (
        input_tokens * card.input_per_million_usd / 1_000_000
        + output_tokens * card.output_per_million_usd / 1_000_000
    )


def compute_cost_ledger(
    sessions: list[CostLedgerInputSession],
    as_of: datetime | None = None,
) -> CostLedger:
    """Aggregate per-session costs into a weekly + 90-day-baseline panel.

    See the module docstring for the baseline rule. The current ISO
    week is excluded from the baseline so the user's own this-week
    activity cannot dilute or inflate the comparison anchor (same
    logic as `praxis.scoring.baseline.compute_baseline`).
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    as_of_date = as_of.date()
    current_week_start = _iso_week_start(as_of_date)
    window_start = as_of_date - timedelta(days=COST_BASELINE_WINDOW_DAYS)
    window_end = current_week_start

    weekly_total = 0.0
    weekly_session_count = 0
    baseline_session_count = 0
    baseline_by_week: dict[date, float] = defaultdict(float)

    for s in sessions:
        if s.cost_usd is None:
            continue
        session_date = s.started_at.date()
        if session_date >= current_week_start:
            weekly_total += s.cost_usd
            weekly_session_count += 1
            continue
        if window_start <= session_date < window_end:
            baseline_by_week[_iso_week_start(session_date)] += s.cost_usd
            baseline_session_count += 1

    if baseline_by_week:
        baseline_mean = sum(baseline_by_week.values()) / len(baseline_by_week)
    else:
        baseline_mean = 0.0

    return CostLedger(
        weekly_spend_usd=round(weekly_total, 4),
        baseline_weekly_mean_usd=round(baseline_mean, 4),
        weekly_session_count=weekly_session_count,
        baseline_session_count=baseline_session_count,
        baseline_week_count=len(baseline_by_week),
        window_start=window_start,
        window_end=window_end,
        current_week_start=current_week_start,
    )


def _empty_biggest_line() -> BiggestLine:
    return BiggestLine(
        model_hint=None,
        task_label=None,
        spend_usd=0.0,
        session_count=0,
    )


def compute_biggest_line(
    sessions: list[BiggestLineInputSession],
    as_of: datetime | None = None,
) -> BiggestLine:
    """Identify the (model, task) pair with the largest spend in the current ISO week.

    Spec section 10.1: the cost panel names the (model, task) pair
    that drove the week's spend. Buckets sessions in the current ISO
    week (`as_of`'s Monday onward) by (model_hint, task_label), sums
    per-session USD within each bucket, and returns the winning
    bucket's pair plus its spend and session count.

    A session contributes iff `cost_usd is not None`, `model_hint is
    not None`, and `task_label is not None`. Unpriced or unidentified
    sessions are silently skipped (same rule as `compute_cost_ledger`
    plus the identification requirement). Sessions in the 90-day
    baseline window but not the current ISO week are ignored: the
    panel reports on the current week only, since the baseline answer
    lives in `CostLedger.baseline_weekly_mean_usd`.

    Tiebreak: maximum spend (descending) -> maximum session_count
    (descending) -> ascending (model_hint, task_label). The last
    two tiers exist purely so the function is deterministic when
    two buckets land at the exact same spend; floating-point ties
    are unlikely in practice but the test suite locks the rule in.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    current_week_start = _iso_week_start(as_of.date())

    spend_by_pair: dict[tuple[str, str], float] = defaultdict(float)
    sessions_by_pair: dict[tuple[str, str], int] = defaultdict(int)

    for s in sessions:
        if s.cost_usd is None or s.model_hint is None or s.task_label is None:
            continue
        if s.started_at.date() < current_week_start:
            continue
        key = (s.model_hint, s.task_label)
        spend_by_pair[key] += s.cost_usd
        sessions_by_pair[key] += 1

    if not spend_by_pair:
        return _empty_biggest_line()

    # Tiebreak: -spend, then -session_count, then (model, task) ascending.
    # Negating spend/count flips the natural sort into descending order
    # so the lexicographic min over the key tuple is the winner.
    winner_key = min(
        spend_by_pair,
        key=lambda k: (-spend_by_pair[k], -sessions_by_pair[k], k[0], k[1]),
    )
    return BiggestLine(
        model_hint=winner_key[0],
        task_label=winner_key[1],
        spend_usd=round(spend_by_pair[winner_key], 4),
        session_count=sessions_by_pair[winner_key],
    )
