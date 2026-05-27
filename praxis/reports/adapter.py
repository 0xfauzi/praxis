"""WeeklyRunSummary -> WeeklyDigest adapters.

The two digest renderers (`digest_html.py` and `digest_terminal.py`) shipped
with independent `WeeklyDigest` schemas; this module converts the orchestrator's
`WeeklyRunSummary` into each shape so a single call can produce both.

Why this lives here (rather than inside the orchestrator): keeps rendering
concerns out of `praxis/orchestrator.py`, which already has more than its
share of merge artifacts. The adapter is the only place that knows both
sides of the boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone

from praxis.behavior import TrajectoryLabel
from praxis.reports import digest_html as dh
from praxis.reports import digest_terminal as dt
from praxis.scoring.rubric import RUBRIC


_TRAJECTORY_DISPLAY: dict[TrajectoryLabel, str] = {
    TrajectoryLabel.LEARNING: "Learning",
    TrajectoryLabel.STABLE_ENGAGED: "Engaged",
    TrajectoryLabel.STABLE_PASSIVE: "Passive",
    TrajectoryLabel.ATROPHYING: "Atrophying",
    TrajectoryLabel.INSUFFICIENT_DATA: "Reading",
}


def _week_label_from_iso(week_iso: str) -> str:
    """Render '2026-W21' as 'Week of <Mon date>-<Sun date>'."""
    try:
        year, week_part = week_iso.split("-W")
        year_i = int(year)
        week_i = int(week_part)
        start = datetime.fromisocalendar(year_i, week_i, 1)
        end = datetime.fromisocalendar(year_i, week_i, 7)
        return f"Week of {start:%b %-d}-{end:%-d}, {year_i}"
    except (ValueError, AttributeError):
        return week_iso or "This week"


def _headline_moment_view_terminal(summary) -> dt.HeadlineMomentView | None:
    sel = summary.selection
    if sel is None:
        return None
    headline_id = sel.headline_moment_id
    for m in summary.moments:
        if m.moment_id == headline_id:
            return dt.HeadlineMomentView(
                dim_key=m.dim_key,
                quoted_excerpt=m.quoted_excerpt,
                why_it_lost_score=m.why_it_lost_score,
                suggested_alternative=m.suggested_alternative,
            )
    return None


def _headline_moment_panel_html(summary) -> dh.MomentPanel | None:
    sel = summary.selection
    if sel is None:
        return None
    headline_id = sel.headline_moment_id
    for m in summary.moments:
        if m.moment_id == headline_id:
            return dh.MomentPanel(
                quoted_excerpt=m.quoted_excerpt,
                why_lost_score=m.why_it_lost_score,
                next_time_try=m.suggested_alternative,
                cost_dollars=m.dollar_impact_estimate,
                cost_minutes=m.minutes_impact_estimate,
            )
    return None


def _follow_up_view_terminal(follow_up) -> dt.FollowUpView | None:
    if follow_up is None:
        return None
    return dt.FollowUpView(
        commitment_text=follow_up.commitment_text,
        target_metric=follow_up.target_metric,
        baseline_value=follow_up.baseline_value,
        measured_value=follow_up.measured_value,
        outcome=follow_up.outcome,
    )


def _follow_up_panel_html(follow_up) -> dh.FollowUpPanel | None:
    if follow_up is None:
        return None
    return dh.FollowUpPanel(
        commitment_text=follow_up.commitment_text,
        outcome=follow_up.outcome,
    )


def _cost_ledger_terminal(summary) -> dt.CostLedgerView | None:
    """Minimal cost-ledger view: this-week vs baseline only.

    The biggest-(model, task) line and tier-fit savings require building
    BiggestLineInputSession / TierFitInputSession lists from sessions +
    judge_results + tasks; deferring to a later wiring pass keeps the
    adapter from doing the per-session cost math twice. Renderer treats
    zero biggest_line_usd as "no biggest line on file" and omits it.
    """
    if summary.cost_total_usd is None and not summary.sessions:
        return None
    return dt.CostLedgerView(
        this_week_usd=float(summary.cost_total_usd or 0.0),
        baseline_usd=summary.cost_baseline_usd,
    )


def _cost_ledger_html(summary) -> dh.CostLedger | None:
    if summary.cost_total_usd is None and not summary.sessions:
        return None
    return dh.CostLedger(
        this_week_dollars=float(summary.cost_total_usd or 0.0),
        baseline_dollars=float(summary.cost_baseline_usd or 0.0),
        biggest_line="",
        sonnet_swap_note="",
    )


def _task_rows_terminal(summary) -> list[dt.TaskRowView] | None:
    if not summary.tasks:
        return None
    rows: list[dt.TaskRowView] = []
    # Spec section 5.5: top 3 tasks ranked by session count (tiebreak cost).
    ranked = sorted(
        summary.tasks,
        key=lambda t: (len(t.session_ids), 0.0),
        reverse=True,
    )[:3]
    sid_to_score = {sid: r.dimension_scores
                    for sid, r in summary.judge_results.items()}
    for task in ranked:
        # Worst dim across the task's sessions
        worst_dim = "verification"  # default fallback
        worst_mean = 11.0
        for d in RUBRIC:
            scores = []
            for sid in task.session_ids:
                ds = sid_to_score.get(sid)
                if ds and d.key in ds:
                    scores.append(ds[d.key])
            if scores:
                m = sum(scores) / len(scores)
                if m < worst_mean:
                    worst_mean = m
                    worst_dim = d.key
        rows.append(dt.TaskRowView(
            label=task.label,
            sessions=len(task.session_ids),
            total_usd=0.0,
            worst_dim_key=worst_dim,
        ))
    return rows


def _task_rows_html(summary) -> tuple[dh.TaskRow, ...]:
    if not summary.tasks:
        return ()
    ranked = sorted(
        summary.tasks,
        key=lambda t: len(t.session_ids),
        reverse=True,
    )[:3]
    sid_to_score = {sid: r.dimension_scores
                    for sid, r in summary.judge_results.items()}
    rows: list[dh.TaskRow] = []
    for task in ranked:
        worst = None
        for d in RUBRIC:
            scores = [sid_to_score[sid][d.key]
                      for sid in task.session_ids
                      if sid in sid_to_score and d.key in sid_to_score[sid]]
            if scores:
                m = sum(scores) / len(scores)
                if worst is None or m < worst:
                    worst = m
        rows.append(dh.TaskRow(
            label=task.label,
            session_count=len(task.session_ids),
            dollars=0.0,
            worst_score=worst,
        ))
    return tuple(rows)


def _dim_rows_terminal(summary) -> list[dt.DimRowView] | None:
    means = summary.snapshot.dimension_means
    if not means:
        return None
    return [
        dt.DimRowView(
            dim_key=d.key,
            score=float(means.get(d.key, 0.0)),
            baseline=(summary.last_week_means or {}).get(d.key),
        )
        for d in RUBRIC
    ]


def _dim_rows_html(summary) -> tuple[dh.DimRow, ...]:
    means = summary.snapshot.dimension_means
    if not means:
        return ()
    last = summary.last_week_means or {}
    rows: list[dh.DimRow] = []
    for d in RUBRIC:
        score = float(means.get(d.key, 0.0))
        baseline = last.get(d.key)
        delta = (score - baseline) if baseline is not None else None
        rows.append(dh.DimRow(
            title=d.title,
            score=score,
            baseline=baseline,
            delta=delta,
        ))
    return tuple(rows)


def _trajectory_html(summary) -> dh.Trajectory | None:
    traj = summary.trajectory
    if traj is None:
        return None
    label = _TRAJECTORY_DISPLAY.get(traj.label, str(traj.label))
    return dh.Trajectory(
        label=label,
        headline=traj.headline or "",
        confidence_band="",
    )


def _trajectory_headline_terminal(summary) -> str | None:
    traj = summary.trajectory
    if traj is None:
        return None
    label = _TRAJECTORY_DISPLAY.get(traj.label, str(traj.label))
    headline = (traj.headline or "").strip()
    if headline:
        return f"{label}. {headline}"
    return label


def _one_thing_to_try(summary) -> str:
    sel = summary.selection
    if sel is None:
        return ""
    headline_id = sel.headline_moment_id
    for m in summary.moments:
        if m.moment_id == headline_id:
            return m.suggested_alternative
    return ""


def build_terminal_digest(summary, follow_up=None) -> dt.WeeklyDigest:
    """Adapter: WeeklyRunSummary -> digest_terminal.WeeklyDigest."""
    return dt.WeeklyDigest(
        week_label=_week_label_from_iso(summary.week_iso),
        trajectory_headline=_trajectory_headline_terminal(summary),
        headline_moment=_headline_moment_view_terminal(summary),
        follow_up=_follow_up_view_terminal(follow_up),
        cost_ledger=_cost_ledger_terminal(summary),
        tasks=_task_rows_terminal(summary),
        dimensions=_dim_rows_terminal(summary),
    )


def build_html_digest(summary, follow_up=None) -> dh.WeeklyDigest:
    """Adapter: WeeklyRunSummary -> digest_html.WeeklyDigest."""
    return dh.WeeklyDigest(
        week_iso=summary.week_iso or "",
        generated_at=datetime.now(timezone.utc),
        trajectory=_trajectory_html(summary),
        headline_moment=_headline_moment_panel_html(summary),
        cost_ledger=_cost_ledger_html(summary),
        task_breakdown=_task_rows_html(summary),
        dimensions=_dim_rows_html(summary),
        follow_up=_follow_up_panel_html(follow_up),
        one_thing_to_try=_one_thing_to_try(summary),
    )
