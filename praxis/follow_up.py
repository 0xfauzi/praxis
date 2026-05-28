"""Follow-up engine.

Closes the weekly coaching loop. Each weekly digest persists one commitment
(taken from this week's headline moment) so next week's run can measure
whether the commitment was kept (spec section 6.3, table in section 14).

This module is intentionally narrow: it carries the data shape (`FollowUp`),
picks the target metric for a given rubric dim, captures the baseline value
at digest generation, assembles the row that gets written, and -- on the
following week -- compares the new value against that baseline to decide
the outcome. The CLI/render surface (US-047) lives elsewhere.

Outcome is computed purely from data (spec sections 2, 6.3, 17.3): the LLM
may write prose around the outcome but is never asked to decide it.

The `HeadlineMoment` here is the smallest object the engine needs. When
the broader v0.2 `Moment` dataclass (spec 4.1) lands via the moments-engine
PRD, it is a superset and satisfies the same call sites.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.rubric import RUBRIC


Outcome = Literal["improved", "unchanged", "worse", "pending", "superseded"]


_RUBRIC_KEYS: frozenset[str] = frozenset(d.key for d in RUBRIC)


@dataclass(frozen=True)
class HeadlineMoment:
    """The two fields the follow-up engine reads from the headline moment.

    Kept narrow so this story does not pre-empt the broader Moment dataclass
    that the moments-engine PRD will introduce per spec 4.1.
    """

    dim_key: str
    suggested_alternative: str


@dataclass
class FollowUp:
    """One row of the `follow_ups` table (spec section 6.3 / section 14).

    ``user_chosen`` flags rows the user explicitly committed to via
    ``praxis commit`` (1) versus rows the orchestrator synthesised from the
    weekly headline moment (0). ``display_text`` is the verbatim user-facing
    string the CLI printed and the user picked; for system-generated rows
    it stays None and the renderer falls back to ``commitment_text``.
    """

    week_iso: str
    dim_key: str
    commitment_text: str
    target_metric: str
    baseline_value: float
    measured_value: float | None = None
    outcome: Outcome = "pending"
    user_chosen: int = 0
    display_text: str | None = None


def target_metric_for(dim_key: str) -> str:
    """Pick the metric whose movement next week proves the commitment landed.

    The spec (sections 6.3, 14) restricts the value to one of:
      - 'verification_rate' for verification-dim commitments, since we already
        have a per-session signal that maps 1:1 to the behavior we're asking
        the user to change.
      - 'delegation_rate' for iteration-dim commitments. The iteration dim is
        about pushing back / refining rather than accepting first drafts; the
        atrophy-side delegation_rate (from BehavioralSignals) is its inverse.
      - '<dim>_dim_mean' for every other rubric dim, since their improvement
        shows up most directly in the next week's mean score for that dim.
    """
    if dim_key == "verification":
        return "verification_rate"
    if dim_key == "iteration":
        return "delegation_rate"
    if dim_key in _RUBRIC_KEYS:
        return f"{dim_key}_dim_mean"
    raise ValueError(f"unknown dim_key: {dim_key!r}")


def compute_baseline_value(
    target_metric: str,
    snapshot: ProfileSnapshot,
    verification_rate: float,
    delegation_rate: float,
) -> float:
    """Read this week's value of `target_metric`, which becomes the baseline."""
    if target_metric == "verification_rate":
        return verification_rate
    if target_metric == "delegation_rate":
        return delegation_rate
    if target_metric.endswith("_dim_mean"):
        dim_key = target_metric[: -len("_dim_mean")]
        if dim_key not in _RUBRIC_KEYS:
            raise ValueError(f"unknown dim in target_metric: {target_metric!r}")
        return float(snapshot.dimension_means.get(dim_key, 0.0))
    raise ValueError(f"unknown target_metric: {target_metric!r}")


def build_follow_up(
    week_iso: str,
    headline_moment: HeadlineMoment,
    snapshot: ProfileSnapshot,
    verification_rate: float,
    delegation_rate: float,
    *,
    user_chosen: int = 0,
    display_text: str | None = None,
) -> FollowUp:
    """Assemble the FollowUp row for the current weekly digest.

    The result has `outcome='pending'` and `measured_value=None`; next week's
    run fills those in (US-046).

    ``user_chosen`` defaults to 0 (the orchestrator's synthesised row);
    ``praxis commit`` passes ``user_chosen=1`` with ``display_text`` set to
    the verbatim user-facing string the user picked, so the next renderer /
    write can reproduce it without paraphrasing.
    """
    metric = target_metric_for(headline_moment.dim_key)
    baseline = compute_baseline_value(
        metric, snapshot, verification_rate, delegation_rate
    )
    return FollowUp(
        week_iso=week_iso,
        dim_key=headline_moment.dim_key,
        commitment_text=headline_moment.suggested_alternative,
        target_metric=metric,
        baseline_value=baseline,
        measured_value=None,
        outcome="pending",
        user_chosen=user_chosen,
        display_text=display_text,
    )


_OUTCOME_THRESHOLD = 0.5


def compute_outcome(
    target_metric: str,
    baseline_value: float,
    measured_value: float,
) -> Outcome:
    """Decide the outcome by comparing this week's measurement to the baseline.

    Per spec section 6.3:
      - "improved" iff measured_value > baseline_value + 0.5
      - "worse"    iff measured_value < baseline_value - 0.5
      - otherwise  "unchanged"

    For `delegation_rate` the comparison is inverted because lower is better
    on that metric (atrophy signal); a drop is improvement, a rise is worse.

    Pure function: only floats and a string in, an Outcome literal out. No
    I/O, no LLM. This is the structural enforcement of the spec rule that
    the LLM never decides the outcome.
    """
    delta = measured_value - baseline_value
    if target_metric == "delegation_rate":
        delta = -delta
    if delta > _OUTCOME_THRESHOLD:
        return "improved"
    if delta < -_OUTCOME_THRESHOLD:
        return "worse"
    return "unchanged"


def close_follow_up(
    follow_up: FollowUp,
    snapshot: ProfileSnapshot,
    verification_rate: float,
    delegation_rate: float,
) -> FollowUp:
    """Close a pending follow-up against the new week's data.

    Reads the current value of the follow-up's `target_metric` from the new
    week's snapshot/signals, then decides the outcome via `compute_outcome`.
    Returns a new FollowUp; does not mutate the input and does not persist.
    """
    measured = compute_baseline_value(
        follow_up.target_metric, snapshot, verification_rate, delegation_rate
    )
    outcome = compute_outcome(
        follow_up.target_metric, follow_up.baseline_value, measured
    )
    return replace(follow_up, measured_value=measured, outcome=outcome)
