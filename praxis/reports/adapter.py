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
from praxis.behavior.repeat_task import (
    Cluster,
    detect_repeats,
)
from praxis.behavior.signals import (
    KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER,
    KNOWLEDGE_GAP_LABELS,
    LADDER_KINDS_IN_PANEL_ORDER,
    LADDER_LABELS,
    SCAFFOLDING_KINDS_IN_PANEL_ORDER,
    SIGNAL_KINDS_IN_PANEL_ORDER,
    categorize_session_ladder_rung,
    categorize_session_verification,
    count_session_knowledge_gaps,
    detect_scaffolding_kinds,
    detect_signal_kinds,
    detect_spec_block,
)
from praxis.models_advisor.advisor import compute_counterfactual_overspend
from praxis.reports import digest_html as dh
from praxis.reports import digest_terminal as dt
from praxis.reports.panel_inputs import (
    CADENCE_WINDOW_DAYS,
    EXCERPT_CHAR_LIMIT,
    MAX_EXCERPTS_PER_SIGNAL,
    REPEAT_TASK_WINDOW_DAYS,
    SCAFFOLDING_LABELS,
    SIGNAL_CITATIONS,
    SIGNAL_LABELS,
    AugAutoBalancePanel,
    BehavioralPatternRow,
    BehavioralPatternsPanel,
    CadencePanel,
    ContextEngineeringDepthPanel,
    ContextEngineeringRow,
    KnowledgeGapDistributionPanel,
    KnowledgeGapRow,
    LadderRungRow,
    PanelInputs,
    RefinedCostEffectivenessPanel,
    RepeatTaskRadarPanel,
    RepeatTaskRow,
    SpecificationAdoptionPanel,
    ToolAgentLadderPanel,
    VerificationCalibrationPanel,
    clip_excerpt,
)
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
        total_chars = sum(len(t.content) for t in s.user_authored_turns)
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
        n_user_turns = len(s.user_authored_turns)
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
        total_chars = sum(len(t.content) for t in s.user_authored_turns)
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
        total_chars = sum(len(t.content) for t in s.user_authored_turns)
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
        chars = sum(len(t.content) for t in s.user_authored_turns)
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


def _behavioral_patterns_panel(summary) -> BehavioralPatternsPanel:
    """Aggregate per-session BehavioralSignals across the week (US-038).

    Walks every user turn in every session in the summary, asks
    ``detect_signal_kinds`` which signals fire for it, and accumulates
    (a) a running count per signal kind and (b) up to
    ``MAX_EXCERPTS_PER_SIGNAL`` raw user-turn excerpts per kind, each
    clipped to ``EXCERPT_CHAR_LIMIT`` chars. Rows always include every
    signal kind in panel-display order so the renderer can distinguish
    "no signals at all" (the empty-state path) from "some signals fired,
    some did not" (the populated table path with explicit zeros).
    """
    counts: dict[str, int] = {k: 0 for k in SIGNAL_KINDS_IN_PANEL_ORDER}
    excerpts: dict[str, list[str]] = {
        k: [] for k in SIGNAL_KINDS_IN_PANEL_ORDER
    }
    for s in summary.sessions or []:
        for turn in s.user_authored_turns:
            kinds = detect_signal_kinds(turn)
            for kind in kinds:
                counts[kind] += 1
                if len(excerpts[kind]) < MAX_EXCERPTS_PER_SIGNAL:
                    excerpts[kind].append(
                        clip_excerpt(turn.content, EXCERPT_CHAR_LIMIT)
                    )
    rows = tuple(
        BehavioralPatternRow(
            signal_kind=kind,
            label=SIGNAL_LABELS[kind],
            count=counts[kind],
            citation=SIGNAL_CITATIONS[kind],
            excerpts=tuple(excerpts[kind]),
        )
        for kind in SIGNAL_KINDS_IN_PANEL_ORDER
    )
    return BehavioralPatternsPanel(rows=rows)


_AUG_AUTO_LABELS = frozenset({"augmentation", "automation", "mixed"})


def _aug_auto_balance_panel(summary) -> AugAutoBalancePanel:
    """Aggregate per-session aug_auto labels across the week (US-039).

    Reads ``aug_auto_classification`` from the session_scores table for
    each session in ``summary.sessions``. The classifier writes labels
    to the DB (via ``store.set_session_aug_auto``); ``Session`` objects
    themselves do NOT carry the label, so looking it up through
    ``ProfileStore.get_session_aug_auto`` is the only correct source.

    Sessions that carry no label (e.g. because the user had no API key,
    or the classifier hit ``AugAutoParseError`` and the orchestrator
    left the columns NULL) fall into ``unclassified_count``. When the
    week has at least one session but no session has a label,
    ``classifier_unavailable`` is set so the renderer surfaces the
    explicit "Classifier unavailable" message rather than misleading
    0/0/0 percentages (US-039 AC).

    When the week has zero sessions, ``classifier_unavailable`` is
    False - that case is structurally different from "no API key" and
    the renderer surfaces a generic zero-sessions empty state instead.
    """
    from praxis.storage.profile_store import ProfileStore

    counts: dict[str, int] = {label: 0 for label in _AUG_AUTO_LABELS}
    unclassified = 0
    session_count = 0
    store: ProfileStore | None = None
    for s in summary.sessions or []:
        session_count += 1
        # Prefer an inline `aug_auto_classification` attribute when the
        # caller injects one (test stubs do this). In production the
        # classifier writes to session_scores, not the Session object,
        # so we fall back to the store lookup keyed by stable_id.
        # Degrades gracefully on any failure (missing attribute,
        # store-open failure, missing row) -> treated as unclassified.
        label = getattr(s, "aug_auto_classification", None)
        if label is None:
            try:
                if store is None:
                    store = ProfileStore()
                stable_id = getattr(s, "stable_id", None)
                if stable_id:
                    label, _confidence = store.get_session_aug_auto(stable_id)
            except Exception:  # noqa: BLE001
                label = None
        if isinstance(label, str) and label in _AUG_AUTO_LABELS:
            counts[label] += 1
        else:
            unclassified += 1
    classified_total = sum(counts.values())
    classifier_unavailable = session_count > 0 and classified_total == 0
    return AugAutoBalancePanel(
        augmentation_count=counts["augmentation"],
        automation_count=counts["automation"],
        mixed_count=counts["mixed"],
        unclassified_count=unclassified,
        classifier_unavailable=classifier_unavailable,
    )


# US-039 high-adopter thresholds anchor to arXiv 2509.19708's spectrum
# over a rolling window. Distinct weekdays active in the window divided
# by the window length yields a 0..1 share; <1/3 is low, 1/3..2/3 is
# moderate, >=2/3 is high. The cadence-detector story will harden these
# thresholds with citations and parameterised tests; this is the panel-
# layer placeholder so the empty-state path is exercisable today.
_CADENCE_LOW_THRESHOLD = 1 / 3
_CADENCE_HIGH_THRESHOLD = 2 / 3


def _classify_high_adopter(streak: int, window_days: int) -> str | None:
    """Resolve a weekday-streak to a high-adopter spectrum position.

    Returns ``None`` when the streak is zero (no activity to position
    on the spectrum) so the renderer omits the label rather than
    misclassifying an inactive week as low-adopter. The cadence-detector
    story owns the canonical thresholds; this placeholder uses 1/3 and
    2/3 of the window so the function is deterministic and bounded.
    """
    if window_days <= 0 or streak <= 0:
        return None
    ratio = streak / window_days
    if ratio < _CADENCE_LOW_THRESHOLD:
        return "low"
    if ratio < _CADENCE_HIGH_THRESHOLD:
        return "moderate"
    return "high"


def _cadence_panel(summary) -> CadencePanel:
    """Build the cadence panel from the in-flight summary (US-039).

    Uses the summary's sessions list as the rolling window: each
    session contributes its calendar weekday (Mon..Sun) to a set, and
    the streak is the size of that set. "Substantive" matches the
    cadence-detector definition: at least two user turns. When zero
    substantive sessions fell in the window the renderer surfaces an
    explicit empty-state message and omits the high-adopter label.

    The window defaults to ``CADENCE_WINDOW_DAYS`` (21 days). When the
    summary's sessions span less than 21 days (typical for a fresh
    install), the streak is still measured over the same denominator so
    the high-adopter position is calibrated against the canonical
    window length, not against whatever happens to be on file.
    """
    weekdays: set[int] = set()
    substantive = 0
    for s in summary.sessions or []:
        user_turns = getattr(s, "user_turns", None) or []
        if len(user_turns) < 2:
            continue
        substantive += 1
        try:
            weekdays.add(s.started_at.weekday())
        except (AttributeError, ValueError):
            # Defensive: a session with a malformed started_at must not
            # crash the panel build. The session is still counted as
            # substantive but contributes no weekday to the streak.
            continue
    streak = len(weekdays)
    position = (
        _classify_high_adopter(streak, CADENCE_WINDOW_DAYS)
        if substantive > 0
        else None
    )
    return CadencePanel(
        weekday_streak=streak,
        substantive_session_count=substantive,
        window_days=CADENCE_WINDOW_DAYS,
        high_adopter_position=position,
    )


# US-040 repeat-task radar:
# When a session has no turn timestamps we estimate its duration from
# the user-turn count. The factor matches the rough cadence the spec
# uses for the "skills could reclaim ~N min/week" callout (each user
# turn ~ 2 minutes of focused work). The value is a renderer-side
# heuristic; the detector treats whatever the caller hands it as wall
# clock truth, so calibrating the heuristic here keeps the detector pure.
_MINUTES_PER_USER_TURN_FALLBACK = 2.0


def _session_duration_minutes(session) -> float:
    """Estimate one session's wall-clock duration in minutes.

    Prefers the timestamp delta between the session's first and last
    user turns (when both carry ``timestamp``). Falls back to
    ``len(user_turns) * _MINUTES_PER_USER_TURN_FALLBACK`` otherwise, so
    a session with no turn-level timestamps still gets a positive
    estimate and the repeat-task radar still surfaces a meaningful
    "reclaimable minutes" number.

    Returns 0.0 for a session with no user turns - the caller (the
    radar adapter) drops such sessions so they cannot inflate the
    median estimate.
    """
    user_turns = getattr(session, "user_turns", None) or []
    if not user_turns:
        return 0.0
    timestamps = [getattr(t, "timestamp", None) for t in user_turns]
    has_timestamps = [ts for ts in timestamps if ts is not None]
    if len(has_timestamps) >= 2:
        try:
            span_seconds = (
                max(has_timestamps) - min(has_timestamps)
            ).total_seconds()
        except (AttributeError, TypeError):
            span_seconds = 0.0
        if span_seconds > 0:
            return span_seconds / 60.0
    return len(user_turns) * _MINUTES_PER_USER_TURN_FALLBACK


def _first_user_turn_text(session) -> str:
    """Return the content of the session's first user turn, or empty."""
    user_turns = getattr(session, "user_turns", None) or []
    if not user_turns:
        return ""
    content = getattr(user_turns[0], "content", "")
    return content or ""


def _repeat_task_radar_panel(summary) -> RepeatTaskRadarPanel:
    """Aggregate recurring task clusters across the week (US-040).

    Walks ``summary.tasks`` and builds a ``Cluster`` per task, seeding
    each cluster's first sentence from one of its session's first user
    turn and its session durations from the per-session estimate
    above. The clusters feed ``detect_repeats`` which returns
    ``RepeatTask`` rows for any cluster recurring 3+ times within the
    window. When the detector returns an empty list the panel surfaces
    the explicit "No repeat tasks detected this week." copy via
    ``has_repeats`` rather than rendering an empty table.

    Tasks with no associated sessions are skipped: a cluster with zero
    sessions cannot anchor a recurrence and would either crash the
    detector or produce a meaningless RepeatTask. This is structurally
    different from "task list was empty" - the latter still produces
    an empty RepeatTaskRadarPanel.
    """
    tasks = getattr(summary, "tasks", None) or []
    sessions = getattr(summary, "sessions", None) or []
    sessions_by_id = {s.stable_id: s for s in sessions}
    clusters: list[Cluster] = []
    for task in tasks:
        task_sessions = [
            sessions_by_id[sid]
            for sid in getattr(task, "session_ids", None) or []
            if sid in sessions_by_id
        ]
        if not task_sessions:
            continue
        # Seed the cluster's first sentence from the earliest session's
        # first user turn so the canonical sentence is reproducible
        # across runs (sorting by started_at; falls back to the first
        # iteration order when timestamps tie).
        try:
            seed_session = min(
                task_sessions,
                key=lambda s: getattr(s, "started_at", None) or 0,
            )
        except TypeError:
            seed_session = task_sessions[0]
        first_sentence = _first_user_turn_text(seed_session)
        if not first_sentence:
            first_sentence = getattr(task, "label", "") or ""
        durations = [
            _session_duration_minutes(s) for s in task_sessions
        ]
        clusters.append(
            Cluster(
                first_sentence=first_sentence,
                session_ids=[s.stable_id for s in task_sessions],
                session_durations_minutes=durations,
            )
        )
    if not clusters:
        return RepeatTaskRadarPanel()
    repeats = detect_repeats(clusters, REPEAT_TASK_WINDOW_DAYS)
    rows = tuple(
        RepeatTaskRow(
            canonical_first_sentence=repeat.canonical_first_sentence,
            occurrences=repeat.occurrences,
            estimated_minutes_per_occurrence=repeat.estimated_minutes_per_occurrence,
        )
        for repeat in repeats
    )
    return RepeatTaskRadarPanel(rows=rows)


def _verification_calibration_panel(
    summary,
) -> VerificationCalibrationPanel:
    """Categorize the week's sessions by verification rigor (US-040).

    Each session is bucketed by its highest-rigor verification activity
    via ``categorize_session_verification``: source_check (highest) >
    test_run > spot_check > blanket_accept (default when no signal
    fires). The panel surfaces the four counts so the renderer can show
    a histogram. When the week has zero sessions all four counts are
    zero and ``has_sessions`` is False; the renderer falls through to
    a generic empty state in that case.
    """
    counts = {
        "source_check": 0,
        "test_run": 0,
        "spot_check": 0,
        "blanket_accept": 0,
    }
    for s in getattr(summary, "sessions", None) or []:
        kind = categorize_session_verification(s)
        if kind in counts:
            counts[kind] += 1
        else:
            # Defensive: unknown kind defaults to blanket_accept so the
            # panel never silently drops a session.
            counts["blanket_accept"] += 1
    return VerificationCalibrationPanel(
        source_check_count=counts["source_check"],
        test_run_count=counts["test_run"],
        spot_check_count=counts["spot_check"],
        blanket_accept_count=counts["blanket_accept"],
    )


def _specification_adoption_panel(summary) -> SpecificationAdoptionPanel:
    """Count sessions opening with a structured spec block (US-041).

    Walks every session in the summary and counts how many opened with
    a Markdown spec heading or label-colon form per
    ``detect_spec_block``. The renderer surfaces the adoption SHARE so
    the count is paired with a denominator (total sessions in the week)
    to keep the share interpretable.
    """
    total_sessions = 0
    sessions_with_spec = 0
    for s in getattr(summary, "sessions", None) or []:
        total_sessions += 1
        try:
            if detect_spec_block(s):
                sessions_with_spec += 1
        except (AttributeError, TypeError):
            # Defensive: a session with a malformed user_turns must not
            # crash the panel build. Treat as no-signal and continue.
            continue
    return SpecificationAdoptionPanel(
        sessions_with_spec=sessions_with_spec,
        total_sessions=total_sessions,
    )


def _context_engineering_panel(summary) -> ContextEngineeringDepthPanel:
    """Count sessions referencing scaffolding artifacts (US-041).

    Walks every session in the summary, asks
    ``detect_scaffolding_kinds`` which scaffolding artifacts (CLAUDE.md,
    AGENTS.md, copilot-instructions.md, Projects, Skills) were named,
    and accumulates a per-kind session count. A row is always present
    for every kind in panel-display order so the renderer can
    distinguish "no scaffolding at all this week" from "scaffolding for
    some kinds, none for others".
    """
    counts: dict[str, int] = {kind: 0 for kind in SCAFFOLDING_KINDS_IN_PANEL_ORDER}
    total_sessions = 0
    for s in getattr(summary, "sessions", None) or []:
        total_sessions += 1
        try:
            kinds = detect_scaffolding_kinds(s)
        except (AttributeError, TypeError):
            continue
        for kind in kinds:
            if kind in counts:
                counts[kind] += 1
    rows = tuple(
        ContextEngineeringRow(
            kind=kind,
            label=SCAFFOLDING_LABELS.get(kind, kind),
            sessions_with_artifact=counts[kind],
        )
        for kind in SCAFFOLDING_KINDS_IN_PANEL_ORDER
    )
    return ContextEngineeringDepthPanel(
        rows=rows,
        total_sessions=total_sessions,
    )


def _knowledge_gap_distribution_panel(
    summary,
) -> KnowledgeGapDistributionPanel:
    """Aggregate per-turn knowledge gaps across the week (US-041).

    For every session, accumulates the per-kind knowledge-gap counts
    from ``count_session_knowledge_gaps``. The four arXiv 2501.11709
    categories are always present in the returned rows even when their
    counts are zero (US-041 acceptance: no silent drops); the renderer
    surfaces the empty-state copy only when every category is zero.
    """
    counts: dict[str, int] = {
        kind: 0 for kind in KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER
    }
    for s in getattr(summary, "sessions", None) or []:
        try:
            per_session = count_session_knowledge_gaps(s)
        except (AttributeError, TypeError):
            continue
        for kind, value in per_session.items():
            if kind in counts:
                counts[kind] += value
    rows = tuple(
        KnowledgeGapRow(
            kind=kind,
            label=KNOWLEDGE_GAP_LABELS.get(kind, kind),
            count=counts[kind],
        )
        for kind in KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER
    )
    return KnowledgeGapDistributionPanel(rows=rows)


def _tool_agent_ladder_panel(summary) -> ToolAgentLadderPanel:
    """Aggregate per-session ladder rungs across the week (US-042).

    Walks each session, asks ``categorize_session_ladder_rung`` for the
    highest rung the session reached (prompt-only -> tools-on -> skills
    -> hooks -> subagents), accumulates per-rung session counts, and
    tracks the highest rung any session reached this week. The renderer
    surfaces the max rung as the headline plus a per-rung breakdown.

    When the week has zero sessions, ``max_rung_kind`` stays ``None``
    and the panel's ``has_activity`` flag flips False so the renderer
    surfaces an empty-state placeholder instead of misleading zero rows.
    """
    counts: dict[str, int] = {k: 0 for k in LADDER_KINDS_IN_PANEL_ORDER}
    max_rung_idx = -1
    max_rung_kind: str | None = None
    for s in getattr(summary, "sessions", None) or []:
        try:
            rung = categorize_session_ladder_rung(s)
        except (AttributeError, TypeError):
            # Defensive: a session with no turns attribute must not crash
            # the panel build.
            continue
        if rung in counts:
            counts[rung] += 1
        else:
            counts["prompt_only"] += 1
            rung = "prompt_only"
        idx = LADDER_KINDS_IN_PANEL_ORDER.index(rung)
        if idx > max_rung_idx:
            max_rung_idx = idx
            max_rung_kind = rung
    rows = tuple(
        LadderRungRow(
            kind=kind,
            label=LADDER_LABELS[kind],
            session_count=counts[kind],
        )
        for kind in LADDER_KINDS_IN_PANEL_ORDER
    )
    max_rung_label = (
        LADDER_LABELS.get(max_rung_kind, "") if max_rung_kind else ""
    )
    return ToolAgentLadderPanel(
        rows=rows,
        max_rung_kind=max_rung_kind,
        max_rung_label=max_rung_label,
    )


def _refined_cost_effectiveness_panel(
    summary,
) -> RefinedCostEffectivenessPanel:
    """Apply the deterministic counterfactual rule to the week (US-042).

    Delegates to ``compute_counterfactual_overspend`` (documented in
    praxis/models_advisor/advisor.py) which:

      - resolves each session's model_hint to a card,
      - filters frontier-tier sessions with small workloads (<=3 user
        turns AND <=200 avg prompt chars),
      - computes (frontier_cost - fast_cost) per qualifying session,
      - sums per (higher, lower) tier pair and returns the dominant
        pair plus the totals.

    The panel's ``has_cost_data`` mirrors the counterfactual rule's
    ``had_any_priced_session`` so the renderer can distinguish "zero
    overspend because nothing qualified" from "no priced sessions at
    all this week" (US-042 AC).
    """
    sessions = list(getattr(summary, "sessions", None) or [])
    result = compute_counterfactual_overspend(sessions)
    return RefinedCostEffectivenessPanel(
        higher_tier_display=result.higher_tier_display,
        lower_tier_display=result.lower_tier_display,
        spent_on_higher_tier_usd=result.spent_on_higher_tier_usd,
        overspend_usd=result.overspend_usd,
        qualifying_session_count=result.qualifying_session_count,
        has_cost_data=result.had_any_priced_session,
    )


def build_panel_inputs(summary) -> PanelInputs:
    """Build the v0.3 expansion-panel inputs from a WeeklyRunSummary.

    Each panel is independently optional; this helper populates the
    fields it can build from the in-flight summary. US-038 wires the
    behavioral-patterns panel; US-039 wires the augmentation/automation
    balance and the cadence panel; US-040 wires the repeat-task radar
    and the verification-calibration panel; US-041 wires the
    specification-adoption, context-engineering-depth, and
    knowledge-gap distribution panels; US-042 wires the tool/agent
    ladder and the refined cost-effectiveness panels.
    """
    return PanelInputs(
        behavioral_signals=_behavioral_patterns_panel(summary),
        aug_auto_balance=_aug_auto_balance_panel(summary),
        cadence=_cadence_panel(summary),
        repeat_task_radar=_repeat_task_radar_panel(summary),
        verification_calibration=_verification_calibration_panel(summary),
        specification_adoption=_specification_adoption_panel(summary),
        context_engineering=_context_engineering_panel(summary),
        knowledge_gap_distribution=_knowledge_gap_distribution_panel(summary),
        tool_agent_ladder=_tool_agent_ladder_panel(summary),
        refined_cost_effectiveness=_refined_cost_effectiveness_panel(summary),
    )


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
        panel_inputs=build_panel_inputs(summary),
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
        panel_inputs=build_panel_inputs(summary),
    )
