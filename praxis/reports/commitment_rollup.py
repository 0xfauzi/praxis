"""Commitment rollup: progress-against-commitment payload for the masthead.

Spec section 2 (coaching-reposition): the weekly review opens with a
masthead block that anchors on the active commitment. This module
defines the dataclass renderers read and the helper that builds one
from the orchestrator's in-flight state.

The rollup is None when no commitment exists for the rendered week. The
two renderers (terminal + HTML) branch on identity rather than falling
back to an empty rollup, so the masthead's commitment block is omitted
cleanly rather than rendered with zero/empty fields.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from praxis.follow_up import FollowUp
    from praxis.scoring.aggregate import ProfileSnapshot
    from praxis.storage.profile_store import ProfileStore


SELF_REPORT_KEYS: tuple[str, ...] = ("yes", "no", "partial", "skip")


def _zero_tally() -> dict[str, int]:
    return {key: 0 for key in SELF_REPORT_KEYS}


@dataclass(frozen=True)
class CommitmentRollup:
    """One week's progress against the active commitment.

    `display_text` is the user-facing phrasing carried over from the
    follow-up the user picked when the commitment was set.
    `target_dim_key` is the rubric dimension the commitment focuses on
    (so renderers know which `dim_after` value to surface in the
    "data says" line).

    `sessions_this_week` / `sessions_prior_week` let the masthead show
    a session-count delta without each renderer recomputing it.
    `self_report_tally` carries the yes/no/partial/skip counts derived
    from `session_reflections.follow_up_id`. The tally is zero-filled
    when no reflections were saved (or the table has not landed yet).

    `dim_before` / `dim_after` are per-dimension means: `dim_after` is
    the current week's mean per rubric dim (from session_scores within
    the week), `dim_before` is the prior week's mean (empty when no
    prior data is on file).

    `gap_prose` (US-037) carries the constrained cheap-judge sentence(s)
    that the masthead renders under the "Gap:" field when self-report
    and dim data disagree. It stays None when no judge call was made or
    the call returned nothing; renderers fall back to the documented
    static neutral phrasing in that case.
    """

    display_text: str
    target_dim_key: str
    sessions_this_week: int
    sessions_prior_week: int
    self_report_tally: dict[str, int] = field(default_factory=_zero_tally)
    dim_before: dict[str, float] = field(default_factory=dict)
    dim_after: dict[str, float] = field(default_factory=dict)
    gap_prose: str | None = None


def fetch_self_report_tally(store: ProfileStore | None, week_iso: str) -> dict[str, int]:
    """Aggregate self_report counts for one ISO week's commitment.

    Joins `session_reflections` to `follow_ups` via `follow_up_id` and
    groups by `self_report` so we return the four-bucket tally the
    masthead reads. Returns a zero-filled tally when:
      - the store is None,
      - the `session_reflections` table is absent (e.g. an early build
        before the reflections schema lands),
      - or no reflection rows match the week.

    The masthead is meant to surface zero counts when nothing has been
    reflected on yet, so a `OperationalError` from a missing table is
    treated as "no reflections yet" rather than propagated. Future
    migrations that add the table will start populating the tally
    automatically.
    """
    tally = _zero_tally()
    if store is None:
        return tally
    try:
        with store._conn() as conn:
            rows = conn.execute(
                "SELECT sr.self_report AS report, COUNT(*) AS n "
                "FROM session_reflections sr "
                "JOIN follow_ups fu ON fu.id = sr.follow_up_id "
                "WHERE fu.week_iso = ? "
                "GROUP BY sr.self_report",
                (week_iso,),
            ).fetchall()
    except sqlite3.OperationalError:
        return tally
    for row in rows:
        report = row["report"] if hasattr(row, "keys") else row[0]
        count = row["n"] if hasattr(row, "keys") else row[1]
        if report in tally:
            tally[report] = int(count)
    return tally


def build_commitment_rollup(
    *,
    follow_up: FollowUp | None,
    snapshot: ProfileSnapshot,
    prior_week_means: Mapping[str, float] | None,
    sessions_this_week: int,
    sessions_prior_week: int,
    self_report_tally: Mapping[str, int] | None = None,
) -> CommitmentRollup | None:
    """Assemble a CommitmentRollup from in-flight pipeline state.

    Returns None when `follow_up` is None: the masthead's commitment
    block is omitted rather than rendered with placeholder copy. All
    other fields are derived from inputs already available to
    `run_weekly` (snapshot, prior-week means, session counts) so this
    helper does no extra I/O.
    """
    if follow_up is None:
        return None
    dim_after = {key: float(value) for key, value in snapshot.dimension_means.items()}
    dim_before: dict[str, float] = (
        {key: float(value) for key, value in prior_week_means.items()} if prior_week_means else {}
    )
    if self_report_tally is None:
        tally = _zero_tally()
    else:
        tally = _zero_tally()
        for key in SELF_REPORT_KEYS:
            if key in self_report_tally:
                tally[key] = int(self_report_tally[key])
    return CommitmentRollup(
        display_text=follow_up.commitment_text,
        target_dim_key=follow_up.dim_key,
        sessions_this_week=int(sessions_this_week),
        sessions_prior_week=int(sessions_prior_week),
        self_report_tally=tally,
        dim_before=dim_before,
        dim_after=dim_after,
    )
