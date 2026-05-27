"""Follow-up engine.

Closes the weekly coaching loop. Each weekly digest persists one commitment
(taken from this week's headline moment) so next week's run can measure
whether the commitment was kept (spec section 6.3, table in section 14).

This module is intentionally narrow: it carries the data shape (`FollowUp`),
picks the target metric for a given rubric dim, captures the baseline value
at digest generation, and assembles the row that gets written. The
"compute outcome from next week's data" step (US-046) and the CLI/render
surface (US-047) live elsewhere.

The `HeadlineMoment` here is the smallest object the engine needs. When
the broader v0.2 `Moment` dataclass (spec 4.1) lands via the moments-engine
PRD, it is a superset and satisfies the same call sites.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.rubric import RUBRIC


Outcome = Literal["improved", "unchanged", "worse", "pending"]


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
    """One row of the `follow_ups` table (spec section 6.3 / section 14)."""

    week_iso: str
    dim_key: str
    commitment_text: str
    target_metric: str
    baseline_value: float
    measured_value: float | None = None
    outcome: Outcome = "pending"


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
) -> FollowUp:
    """Assemble the FollowUp row for the current weekly digest.

    The result has `outcome='pending'` and `measured_value=None`; next week's
    run fills those in (US-046).
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
    )
