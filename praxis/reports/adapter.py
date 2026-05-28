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
from praxis.scoring.cost_ledger import (
    BiggestLineInputSession,
    TierFitInputSession,
    compute_biggest_line,
    compute_tier_fit_savings,
    estimate_session_cost_usd,
    estimate_tier_fit_savings_for_session,
)
from praxis.scoring.rubric import RUBRIC


def _cost_inputs(summary):
    """Build per-session cost-ledger inputs from the in-flight summary.

    Returns (BiggestLine, TierFitSavings). Both functions agree on the
    'current ISO week' anchor (session.started_at >= Monday of as_of),
    so we don't need to filter sessions ahead of time - they handle it.
    """
    task_label_by_sid: dict[str, str] = {}
    for task in summary.tasks or []:
        for sid in task.session_ids:
            task_label_by_sid[sid] = task.label

    biggest_inputs: list[BiggestLineInputSession] = []
    tier_inputs: list[TierFitInputSession] = []
    for s in summary.sessions or []:
        total_chars = sum(len(t.content) for t in s.user_turns)
        cost = estimate_session_cost_usd(s.model_hint, total_chars)
        biggest_inputs.append(BiggestLineInputSession(
            started_at=s.started_at,
            cost_usd=cost,
            model_hint=s.model_hint,
            task_label=task_label_by_sid.get(s.stable_id),
        ))
        tier_savings = estimate_tier_fit_savings_for_session(
            s.model_hint, total_chars
        )
        n_user_turns = len(s.user_turns)
        avg_chars = total_chars / n_user_turns if n_user_turns else 0.0
        tier_inputs.append(TierFitInputSession(
            started_at=s.started_at,
            user_turn_count=n_user_turns,
            avg_prompt_chars=avg_chars,
            tier_fit_savings_usd=tier_savings,
        ))
    biggest = compute_biggest_line(biggest_inputs)
    tier = compute_tier_fit_savings(tier_inputs)
    return biggest, tier


_TRAJECTORY_DISPLAY: dict[TrajectoryLabel, str] = {
    # v0.2 spec section 7.2 labels (weekly-bucketed model)
    TrajectoryLabel.LEARNING: "Learning",
    TrajectoryLabel.GROWING_AUTONOMY: "Growing autonomy",
    TrajectoryLabel.STEADY: "Steady",
    TrajectoryLabel.DRIFTING: "Drifting",
    TrajectoryLabel.ATROPHYING: "Atrophying",
    TrajectoryLabel.READING: "Reading",
    # v0.1 legacy labels (per-session heuristic, kept for early-week fallback)
    TrajectoryLabel.STABLE_ENGAGED: "Engaged",
    TrajectoryLabel.STABLE_PASSIVE: "Passive",
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


def _dim_title_from_key(dim_key: str) -> str:
    """Look up a rubric dimension's display title from its key."""
    try:
        from praxis.scoring.rubric import by_key
        return by_key(dim_key).title
    except Exception:  # noqa: BLE001
        return dim_key.title() if dim_key else ""


def _format_session_started_at(sessions, stable_id: str) -> str:
    """Find a session's started_at and render as 'Tuesday, May 27, 4:32 PM UTC'."""
    if not stable_id:
        return ""
    for s in sessions or []:
        if s.stable_id == stable_id:
            try:
                return s.started_at.strftime("%A, %B %-d, %-I:%M %p UTC")
            except (AttributeError, ValueError):
                return ""
    return ""


def _headline_moment_panel_html(summary, recurrence_count: int = 0) -> dh.MomentPanel | None:
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
                dim_title=_dim_title_from_key(m.dim_key),
                session_started_at=_format_session_started_at(
                    summary.sessions, m.session_stable_id
                ),
                recurrence_count=int(recurrence_count or 0),
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
    """Cost ledger view including biggest-(model, task) line and tier-fit savings.

    Both helpers operate on the same per-session inputs computed once
    by _cost_inputs(); cost is estimated from the user-turn char volume
    against the model card's pricing.
    """
    if summary.cost_total_usd is None and not summary.sessions:
        return None
    biggest, tier = _cost_inputs(summary)
    return dt.CostLedgerView(
        this_week_usd=float(summary.cost_total_usd or 0.0),
        baseline_usd=summary.cost_baseline_usd,
        biggest_model=biggest.model_hint or "",
        biggest_task_label=biggest.task_label or "",
        biggest_line_usd=float(biggest.spend_usd or 0.0),
        biggest_line_sessions=int(biggest.session_count or 0),
        over_tier_sessions=int(tier.qualifying_session_count or 0),
        tier_fit_savings_usd=float(tier.estimated_savings_usd or 0.0),
    )


def _cost_ledger_html(summary) -> dh.CostLedger | None:
    if summary.cost_total_usd is None and not summary.sessions:
        return None
    biggest, tier = _cost_inputs(summary)
    biggest_line = ""
    if biggest.model_hint and biggest.task_label:
        biggest_line = (
            f"{biggest.model_hint} on {biggest.task_label} "
            f"(${biggest.spend_usd:.2f} over {biggest.session_count} sessions)"
        )
    sonnet_note = ""
    if tier.qualifying_session_count > 0:
        sonnet_note = (
            f"{tier.qualifying_session_count} of those would have worked on a cheaper tier, "
            f"saving ~${tier.estimated_savings_usd:.2f}"
        )
    return dh.CostLedger(
        this_week_dollars=float(summary.cost_total_usd or 0.0),
        baseline_dollars=float(summary.cost_baseline_usd or 0.0),
        biggest_line=biggest_line,
        sonnet_swap_note=sonnet_note,
        model_split=_model_split_html(summary),
    )


def _task_rows_terminal(summary) -> list[dt.TaskRowView] | None:
    if not summary.tasks:
        return None
    rows: list[dt.TaskRowView] = []
    # Per-session USD by session id, used to populate per-task totals so
    # 'WHERE THE WEEK WENT' no longer renders $0.00 next to every row.
    cost_by_sid: dict[str, float] = {}
    for s in summary.sessions or []:
        total_chars = sum(len(t.content) for t in s.user_turns)
        cost = estimate_session_cost_usd(s.model_hint, total_chars)
        if cost is not None:
            cost_by_sid[s.stable_id] = cost
    # Spec section 5.5: top 3 tasks ranked by session count, tiebreak cost.
    ranked = sorted(
        summary.tasks,
        key=lambda t: (
            len(t.session_ids),
            sum(cost_by_sid.get(sid, 0.0) for sid in t.session_ids),
        ),
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
        task_total = sum(cost_by_sid.get(sid, 0.0) for sid in task.session_ids)
        rows.append(dt.TaskRowView(
            label=task.label,
            sessions=len(task.session_ids),
            total_usd=task_total,
            worst_dim_key=worst_dim,
        ))
    return rows


def _task_rows_html(summary) -> tuple[dh.TaskRow, ...]:
    if not summary.tasks:
        return ()
    cost_by_sid: dict[str, float] = {}
    for s in summary.sessions or []:
        total_chars = sum(len(t.content) for t in s.user_turns)
        cost = estimate_session_cost_usd(s.model_hint, total_chars)
        if cost is not None:
            cost_by_sid[s.stable_id] = cost
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
        task_total = sum(cost_by_sid.get(sid, 0.0) for sid in task.session_ids)
        rows.append(dh.TaskRow(
            label=task.label,
            session_count=len(task.session_ids),
            dollars=task_total,
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
        evidence=tuple(traj.evidence or ()),
        risks=tuple(traj.risks or ()),
        interventions=tuple(traj.interventions or ()),
        engagement_slope=float(traj.engagement_slope or 0.0),
        delegation_slope=float(traj.delegation_slope or 0.0),
    )


def _vital_signs_html(summary) -> dh.VitalSigns | None:
    """Aggregate engagement_rate and delegation_rate across the week."""
    if not summary.sessions:
        return None
    from praxis.behavior import extract as extract_signals
    eng_rates: list[float] = []
    del_rates: list[float] = []
    for s in summary.sessions:
        sig = extract_signals(s)
        eng_rates.append(sig.engagement_rate)
        del_rates.append(sig.delegation_rate)
    if not eng_rates:
        return None
    eng = sum(eng_rates) / len(eng_rates)
    deleg = sum(del_rates) / len(del_rates)
    spend = float(summary.cost_total_usd or 0.0)
    return dh.VitalSigns(
        engagement_rate=eng,
        delegation_rate=deleg,
        session_count=len(summary.sessions),
        spend_usd=spend,
    )


def _weekly_trajectory_html(summary) -> tuple:
    """Build multi-week (week_iso, engagement, delegation) tuples.

    Reads the same 90-day window the trajectory model reads. Returns an
    empty tuple when no signals are persisted (the renderer shows a
    placeholder in that case).
    """
    from praxis.behavior.weekly import (
        WeeklySessionInput,
        bucket_sessions_by_iso_week,
    )
    import json
    from datetime import datetime, timedelta, timezone
    from praxis.storage.profile_store import ProfileStore

    try:
        store = ProfileStore()
    except Exception:  # noqa: BLE001
        return tuple()
    since = datetime.now(timezone.utc) - timedelta(days=90)
    try:
        rows = store.load_session_scores(since=since)
    except Exception:  # noqa: BLE001
        return tuple()
    # We bucket two streams in parallel here:
    #   1. The standard WeeklyBucket model (used for hysteresis / labels)
    #      which doesn't include a verification-marker rate.
    #   2. A per-week tally of verification_marker_rate computed from
    #      each row's features_json (marker_hit_counts) so the small-
    #      multiples chart can plot that signal alongside the others.
    from praxis.behavior.weekly import iso_week_tag as _iso_week_tag

    inputs: list[WeeklySessionInput] = []
    # Per ISO week: (verification_hit_total, user_turn_total)
    verify_tally: dict[str, tuple[int, int]] = {}
    for row in rows:
        raw = row.get("signals_json")
        if not raw:
            continue
        try:
            sig = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        started_at = datetime.fromisoformat(row["started_at"])
        inputs.append(WeeklySessionInput(
            started_at=started_at,
            engagement_rate=float(sig.get("engagement_rate", 0.0)),
            delegation_rate=float(sig.get("delegation_rate", 0.0)),
            independence_rate=float(sig.get("independence_rate", 0.0)),
            dim_scores=row.get("dimension_scores") or {},
        ))
        # Per-session verification marker rate = marker_hit_counts.verification / turn_count.
        feats = row.get("features") or {}
        markers = (feats.get("marker_hit_counts") or {}) if isinstance(feats, dict) else {}
        verify_hits = int(markers.get("verification", 0) or 0)
        # The features dict stores turn_count (total turns) but for the
        # rate we want share of USER turns. Use the signals dict's
        # user_turn_count as the divisor; fall back to features.turn_count
        # so older rows still produce a usable number.
        ut = int(sig.get("user_turn_count", feats.get("turn_count", 0)) or 0)
        if ut > 0:
            iso = _iso_week_tag(started_at)
            cur_hits, cur_turns = verify_tally.get(iso, (0, 0))
            verify_tally[iso] = (cur_hits + verify_hits, cur_turns + ut)

    if not inputs:
        return tuple()
    buckets = bucket_sessions_by_iso_week(inputs)
    points = []
    for b in buckets:
        if not b.eligible_for_fit:
            continue
        v_hits, v_turns = verify_tally.get(b.iso_week, (0, 0))
        verification_rate = (v_hits / v_turns) if v_turns > 0 else 0.0
        points.append(dh.WeeklyTrajectoryPoint(
            week_iso=b.iso_week,
            engagement_rate=b.engagement_rate_mean,
            delegation_rate=b.delegation_rate_mean,
            independence_rate=b.independence_rate_mean,
            verification_marker_rate=verification_rate,
        ))
    return tuple(points)


def _behavioral_signals_html(
    summary, points: tuple
) -> tuple:
    """Build the 4 behavioral-signal cards (Engagement, Delegation,
    Independence, Verification) shown beneath the rubric dim cards.

    Uses the same weekly trajectory points the small-multiples chart
    uses, so the cards and the chart are computed from the same data.
    The "current" row is this week (the last point); the baseline is the
    mean of earlier weeks. With <2 prior weeks the baseline reads
    "forming" rather than guessing.
    """
    if not points:
        return tuple()
    current = points[-1]
    history = points[:-1]

    def baseline_for(getter) -> float | None:
        if not history:
            return None
        return sum(getter(p) for p in history) / len(history)

    def row(
        title: str,
        getter,
        frame: str,
    ) -> dh.BehavioralRow:
        rate = float(getter(current))
        baseline = baseline_for(getter)
        delta = (rate - baseline) if baseline is not None else None
        return dh.BehavioralRow(
            title=title,
            rate=rate,
            baseline_rate=baseline,
            delta=delta,
            frame=frame,
        )

    return (
        row("Engagement", lambda p: p.engagement_rate, "supportive"),
        row("Delegation", lambda p: p.delegation_rate, "counter"),
        row("Independence", lambda p: p.independence_rate, "supportive"),
        row("Verification", lambda p: p.verification_marker_rate, "supportive"),
    )


def _recurrence_count_for_headline(summary) -> int:
    """How many of the prior 3 weekly_digests had the same suggested_alternative
    as this week's headline moment."""
    sel = summary.selection
    if sel is None:
        return 0
    # Find this week's headline moment's suggested_alternative
    headline_id = sel.headline_moment_id
    target_alt = ""
    for m in summary.moments:
        if m.moment_id == headline_id:
            target_alt = m.suggested_alternative or ""
            break
    if not target_alt:
        return 0
    # Look at the prior 3 weekly_digests
    from praxis.storage.profile_store import ProfileStore
    from praxis.orchestrator import _prior_iso_week  # type: ignore
    try:
        store = ProfileStore()
    except Exception:  # noqa: BLE001
        return 0
    iso = summary.week_iso
    count = 0
    for _ in range(3):
        iso = _prior_iso_week(iso)
        try:
            digest_row = store.load_weekly_digest(iso)
        except Exception:  # noqa: BLE001
            continue
        if digest_row is None:
            continue
        moment_id = digest_row.get("headline_moment_id")
        if not moment_id:
            continue
        m_row = store.load_moment_by_id(moment_id)
        if m_row is None:
            continue
        if (m_row.get("suggested_alternative") or "") == target_alt:
            count += 1
    return count


def _model_split_html(summary) -> tuple:
    """Per-model spend tuples for the cost stacked-bar chart."""
    if not summary.sessions:
        return tuple()
    spend_by_model: dict[str, float] = {}
    for s in summary.sessions:
        chars = sum(len(t.content) for t in s.user_turns)
        c = estimate_session_cost_usd(s.model_hint, chars)
        if c is None or c <= 0:
            continue
        key = s.model_hint or "unknown"
        spend_by_model[key] = spend_by_model.get(key, 0.0) + c
    return tuple(
        dh.ModelSpend(model=m, dollars=v)
        for m, v in spend_by_model.items()
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
        commitment_rollup=getattr(summary, "commitment_rollup", None),
    )


def build_html_digest(summary, follow_up=None) -> dh.WeeklyDigest:
    """Adapter: WeeklyRunSummary -> digest_html.WeeklyDigest.

    Populates the rich v0.2+ fields (vital signs, trajectory evidence /
    risks / interventions, model-split cost breakdown, recurrence count,
    multi-week trajectory points) in addition to the original section
    inputs. Each helper is best-effort: a missing DB or partial data
    yields an empty field rather than raising, so the renderer always
    gets a valid WeeklyDigest.
    """
    recurrence = _recurrence_count_for_headline(summary)
    # Trajectory points are computed once and shared with the behavioral
    # signal cards so the cards and the small-multiples chart agree.
    trajectory_points = _weekly_trajectory_html(summary)
    return dh.WeeklyDigest(
        week_iso=summary.week_iso or "",
        generated_at=datetime.now(timezone.utc),
        trajectory=_trajectory_html(summary),
        headline_moment=_headline_moment_panel_html(summary, recurrence_count=recurrence),
        cost_ledger=_cost_ledger_html(summary),
        task_breakdown=_task_rows_html(summary),
        dimensions=_dim_rows_html(summary),
        follow_up=_follow_up_panel_html(follow_up),
        one_thing_to_try=_one_thing_to_try(summary),
        vital_signs=_vital_signs_html(summary),
        weekly_trajectory=trajectory_points,
        behavioral_signals=_behavioral_signals_html(summary, trajectory_points),
        commitment_rollup=getattr(summary, "commitment_rollup", None),
    )
