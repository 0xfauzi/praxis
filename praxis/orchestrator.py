"""Orchestrator.

Coordinates scanning, scoring, persistence, and daily consolidation.
This is the brain of the daemon.

Honest framing on "continuous learning": this runs once per invocation.
The continuous part comes from being scheduled (cron / launchd / systemd
timer / Task Scheduler), with deduplication via session stable IDs so
re-runs are idempotent and cheap.
"""
from __future__ import annotations

import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

from praxis.behavior import (
    BehavioralSignals,
    TrajectoryAssessment,
    assess as assess_trajectory,
    extract as extract_signals,
    iso_week_tag,
)
from praxis.models import Moment as JudgeMoment, Session
from praxis.models_advisor import ModelUsageProfile, build_profiles
from praxis.scanners import ALL_SCANNERS
from praxis.scoring.aggregate import (
    ProfileSnapshot,
    SessionScore,
    score_one_session,
)
from praxis.scoring.clustering import Task, cluster_sessions
from praxis.scoring.coach import Coaching, generate_coaching
from praxis.scoring.judge import JudgeResult, verify_moment_substrings
from praxis.scoring.moment_selector import (
    Moment as SelectorMoment,
    MomentCandidate,
    MomentSelection,
    select_moments_with_fallback,
)
from praxis.storage.profile_store import ProfileStore


@dataclass
class RunSummary:
    sessions_seen: int
    sessions_new: int
    sessions_scored: int
    elapsed_seconds: float
    snapshot: ProfileSnapshot
    coaching: Coaching
    consolidated_for: date | None
    trajectory: TrajectoryAssessment | None = None
    model_profiles: list[ModelUsageProfile] | None = None
    # Spec section 8.1: when 2+ weeks of data are available, per-dim
    # means from the immediately prior ISO week are surfaced to the
    # HTML renderer as a faded secondary anchor. None means the
    # precondition is not met and the renderer omits the annotation.
    # The terminal renderer ignores this field by design (it omits
    # the last-week annotation in all cases).
    last_week_means: dict[str, float] | None = None


def _gather_sessions(since_days: int | None = None) -> list[Session]:
    """Run every scanner and collect sessions."""
    since_ts: float | None = None
    if since_days is not None:
        since_ts = time.time() - since_days * 86400

    sessions: list[Session] = []
    for scanner_cls in ALL_SCANNERS:
        scanner = scanner_cls()
        try:
            for session in scanner.scan(since=since_ts):
                sessions.append(session)
        except Exception as exc:  # noqa: BLE001
            print(f"[orchestrator] {scanner.provider_name} scanner failed: {exc!r}", file=sys.stderr)
    return sessions


def run(
    since_days: int | None = 30,
    max_new_scored: int = 50,
    force_consolidate: bool = False,
) -> RunSummary:
    """Full pipeline. Idempotent: re-running won't re-score known sessions.

    Args:
        since_days: Only consider session files modified in the last N days.
        max_new_scored: Cap on how many newly-discovered sessions get the
            judge treatment in one run (protects API budgets).
        force_consolidate: Run daily consolidation even if one already
            exists for today.
    """
    started = time.time()
    store = ProfileStore()

    sessions = _gather_sessions(since_days=since_days)
    # Filter out sessions with no user turns (system-only / tool-only files).
    sessions = [s for s in sessions if s.user_turns]
    new_sessions = [s for s in sessions if not store.has_session(s.stable_id)]

    # Sort newest first so if we hit the cap, the most recent get scored.
    new_sessions.sort(key=lambda s: s.started_at, reverse=True)
    to_score = new_sessions[:max_new_scored]
    scored_count = 0
    for session in to_score:
        score = score_one_session(session)
        if score is None:
            # No judge available (no API keys, or judge errored) - skip the
            # session rather than substituting a fallback score.
            continue
        store.save_session_score(score)
        if score.judge_result is not None:
            # Persist moments only when the judge actually ran; a heuristic-only
            # rescore must not wipe a session's moments from a prior judged run.
            store.save_moments(session.stable_id, score.judge_result.moments)
        scored_count += 1

    # Daily consolidation: only run once per day unless forced.
    today = _utcnow().date()
    last_consolidation = store.latest_consolidation_date()
    should_consolidate = force_consolidate or last_consolidation != today

    rows = store.load_session_scores(
        since=_utcnow() - timedelta(days=since_days or 30)
    )
    snapshot = _snapshot_from_rows(rows)
    coaching: Coaching

    if should_consolidate:
        coaching = generate_coaching(snapshot)
        store.save_consolidation(
            for_date=today,
            snapshot=snapshot,
            coaching=asdict(coaching),
            sessions_in_window=len(rows),
        )
        consolidated_for = today
    else:
        cached = store.load_consolidation(today)
        if cached and cached["coaching"]:
            c = cached["coaching"]
            coaching = Coaching(
                headline=c.get("headline", ""),
                focus_areas=c.get("focus_areas", []),
                daily_practice=c.get("daily_practice", ""),
                generated_by=c.get("generated_by", "cached"),
            )
        else:
            coaching = generate_coaching(snapshot)
        consolidated_for = None

    # Behavioral analysis + per-model advice both need the actual Session
    # objects (not just persisted score rows), so we extract signals from
    # the freshly-scanned sessions in this run's window.
    sessions_in_window = [
        s for s in sessions
        if (_utcnow() - timedelta(days=since_days or 30)).timestamp()
        <= s.started_at.timestamp()
    ]
    sessions_with_signals: list[tuple[Session, BehavioralSignals]] = [
        (s, extract_signals(s)) for s in sessions_in_window
    ]

    trajectory = assess_trajectory(sessions_with_signals)

    # Join behavior signals with this run's score rows so the model advisor
    # has overall scores per session where available.
    score_by_stable_id = {row["stable_id"]: row["overall"] for row in rows}
    enriched_for_models: list[tuple[Session, BehavioralSignals, float | None]] = [
        (s, sig, score_by_stable_id.get(s.stable_id)) for s, sig in sessions_with_signals
    ]
    model_profiles = build_profiles(enriched_for_models)

    store.log_run(
        kind="full",
        sessions_seen=len(sessions),
        sessions_new=len(new_sessions),
        notes=f"scored={scored_count}, consolidated={'y' if should_consolidate else 'n'}, "
              f"trajectory={trajectory.label.value}, "
              f"models={len(model_profiles)}",
    )

    return RunSummary(
        sessions_seen=len(sessions),
        sessions_new=len(new_sessions),
        sessions_scored=scored_count,
        elapsed_seconds=round(time.time() - started, 2),
        snapshot=snapshot,
        coaching=coaching,
        consolidated_for=consolidated_for,
        trajectory=trajectory,
        model_profiles=model_profiles,
    )


# --------------------------------------------------------------------------
# run_weekly() - spec Section 9.4 pipeline.
#
# Step order (spec 9.4 + PRD US-070):
#   1. scan
#   2. cluster
#   3. pass 1 batched (cheap-tier judge)
#   4. pass 2 frontier (only sessions pass 1 flagged low-confidence)
#   5. moments validation (substring check)
#   6. selector
#   7. follow-up
#   8. render
#
# Each step is a module-level callable so tests can monkeypatch and verify
# ordering. The data-flow contract (US-070 AC #2) is enforced by passing
# only documented inputs into each step:
#   scan      -> cluster, pass1
#   cluster   -> pass1 (for batching constraint), render (tasks panel)
#   pass1     -> pass2 (low-confidence subset), validate
#   pass2     -> validate
#   validate  -> select
#   select    -> follow_up, render
#   follow_up -> render
# --------------------------------------------------------------------------


@dataclass
class Pass1Output:
    """Pass 1 judges every session and self-flags confidence on each.

    Splitting "results" from "low_confidence_session_ids" up front means
    pass 2 only sees the IDs of the sessions it should re-judge, not the
    pass-1 scores themselves. Spec 9.1: "The frontier judge is told
    nothing about pass 1's output."
    """

    results: dict[str, JudgeResult]
    low_confidence_session_ids: list[str]


@dataclass
class WeeklyRunSummary:
    """Outcome of one `run_weekly()` invocation.

    Holds the data the digest renderer needs plus a small audit field
    (`steps_executed`) used by the pipeline-ordering test (US-070). The
    persistence and dry-run stories (US-071, US-072) read the same
    fields; render output is filled in by the render step (US-073/render).
    """

    week_iso: str
    sessions: list[Session]
    tasks: list[Task]
    judge_results: dict[str, JudgeResult]
    moments: list[JudgeMoment]
    selection: MomentSelection | None
    snapshot: ProfileSnapshot
    rendered_html: str
    rendered_terminal: str
    elapsed_seconds: float
    trajectory: TrajectoryAssessment | None = None
    # cost_*_usd are wired in by US-073 (perf/cost story) - reserved here so
    # the persistence story (US-071) can plumb them through to weekly_digests.
    cost_total_usd: float | None = None
    cost_baseline_usd: float | None = None
    digest_persisted: bool = False
    steps_executed: list[str] = field(default_factory=list)


def _step_scan(since_days: int) -> list[Session]:
    """Step 1: gather sessions from every provider scanner.

    Output feeds: cluster (step 2), pass1 (step 3).
    """
    sessions = _gather_sessions(since_days=since_days)
    return [s for s in sessions if s.user_turns]


def _step_cluster(sessions: list[Session]) -> list[Task]:
    """Step 2: cluster sessions into tasks via one cheap-tier LLM call.

    Output feeds: pass1 (batching constraint, spec 9.4), render (tasks panel).
    """
    if not sessions:
        return []
    tasks = cluster_sessions(sessions)
    return tasks or []


def _step_pass1(sessions: list[Session], tasks: list[Task]) -> Pass1Output:
    """Step 3: batched cheap-tier judge with same-task exclusion (spec 9.4).

    Per spec 9.1 the cheap judge runs on every session; this story (US-070)
    wires the ordering but reuses the per-session judge path (US-073 will
    add the batching mechanic). `tasks` is accepted here so the batching
    constraint can be enforced when the batched path lands.

    Output feeds: pass2 (low-confidence subset only), validate.
    """
    _ = tasks  # batching mechanic lands in a follow-up story.
    results: dict[str, JudgeResult] = {}
    low_confidence: list[str] = []
    for session in sessions:
        score = score_one_session(session)
        if score is None or score.judge_result is None:
            continue
        results[session.stable_id] = score.judge_result
        if score.judge_result.confidence == "low":
            low_confidence.append(session.stable_id)
    return Pass1Output(results=results, low_confidence_session_ids=low_confidence)


def _step_pass2(
    sessions: list[Session], pass1: Pass1Output
) -> dict[str, JudgeResult]:
    """Step 4: frontier judge re-scores low-confidence sessions only.

    Per spec 9.1 the frontier judge sees nothing of pass 1's output; only
    the sessions whose IDs pass 1 self-flagged "low" are re-judged. This
    function takes only those IDs from `pass1`, not the scores.

    Output feeds: validate (its scores/moments override pass 1's).
    """
    flagged = set(pass1.low_confidence_session_ids)
    if not flagged:
        return {}
    by_id = {s.stable_id: s for s in sessions if s.stable_id in flagged}
    out: dict[str, JudgeResult] = {}
    for sid, session in by_id.items():
        score = score_one_session(session)
        if score is None or score.judge_result is None:
            continue
        out[sid] = score.judge_result
    return out


def _step_validate_moments(
    sessions: list[Session],
    pass1: Pass1Output,
    pass2_results: dict[str, JudgeResult],
) -> list[JudgeMoment]:
    """Step 5: substring-verify moments against the real transcript.

    Per spec 9.1 pass 2 wins on the sessions it ran for; for the rest,
    pass 1 stands. Output feeds: select (step 6).
    """
    final_results: dict[str, JudgeResult] = dict(pass1.results)
    final_results.update(pass2_results)
    by_id = {s.stable_id: s for s in sessions}
    survivors: list[JudgeMoment] = []
    for sid, judge in final_results.items():
        session = by_id.get(sid)
        if session is None:
            continue
        verified = verify_moment_substrings(session, judge.moments)
        survivors.extend(verified)
    return survivors


def _step_select_moments(
    sessions: list[Session], moments: list[JudgeMoment]
) -> MomentSelection | None:
    """Step 6: one LLM call picks headline + up to two supporting moments.

    Output feeds: follow_up (commitment derives from headline), render.
    """
    if not moments:
        return None
    started_by_id = {s.stable_id: s.started_at for s in sessions}
    candidates: list[MomentCandidate] = []
    for jm in moments:
        sid = jm.session_stable_id or ""
        started = started_by_id.get(sid)
        if started is None or jm.moment_id is None:
            continue
        selector_moment = SelectorMoment(
            moment_id=jm.moment_id,
            session_stable_id=sid,
            dim_key=jm.dim_key,
            turn_index=jm.turn_index,
            quoted_excerpt=jm.quoted_excerpt,
            why_it_lost_score=jm.why_it_lost_score,
            suggested_alternative=jm.suggested_alternative,
            severity=jm.severity,
            created_at=jm.created_at or _utcnow(),
            dollar_impact_estimate=jm.dollar_impact_estimate,
            minutes_impact_estimate=jm.minutes_impact_estimate,
        )
        candidates.append(
            MomentCandidate(
                moment=selector_moment,
                session_started_at=started,
                recurrence_count=0,
            )
        )
    if not candidates:
        return None
    return select_moments_with_fallback(candidates)


def _step_follow_up(
    selection: MomentSelection | None,
    moments: list[JudgeMoment],
    snapshot: ProfileSnapshot,
    week_iso: str,
) -> object | None:
    """Step 7: build this week's commitment from the headline moment.

    Output feeds: render (follow-up panel). Persistence of the follow-up
    row and closing of the prior week's row are handled by the persistence
    story (US-071).
    """
    if selection is None:
        return None
    from praxis.follow_up import HeadlineMoment, build_follow_up

    headline = next(
        (
            m
            for m in moments
            if m.moment_id == selection.headline_moment_id
        ),
        None,
    )
    if headline is None:
        return None
    return build_follow_up(
        week_iso=week_iso,
        headline_moment=HeadlineMoment(
            dim_key=headline.dim_key,
            suggested_alternative=headline.suggested_alternative,
        ),
        snapshot=snapshot,
        verification_rate=0.0,
        delegation_rate=0.0,
    )


def _step_render(
    sessions: list[Session],
    tasks: list[Task],
    selection: MomentSelection | None,
    follow_up: object | None,
    snapshot: ProfileSnapshot,
) -> tuple[str, str]:
    """Step 8: produce HTML + terminal renderings of the digest.

    The actual layout is owned by `praxis.reports`; this step's job is
    to assemble inputs from the steps above and call the renderers. The
    full wiring lands in a follow-up story (US-071+); this version
    returns empty strings so the ordering contract can be tested first.
    """
    _ = sessions, tasks, selection, follow_up, snapshot
    return "", ""


def run_weekly(
    since_days: int = 7,
    store: ProfileStore | None = None,
) -> WeeklyRunSummary:
    """Run the weekly pipeline in spec Section 9.4 order.

    Order (US-070):
      1. scan
      2. cluster
      3. pass 1 batched
      4. pass 2 frontier (low-confidence only)
      5. moments validation
      6. selector
      7. follow-up
      8. render

    Outputs flow strictly forward: each step only receives data from its
    documented upstream steps (US-070 AC #2). After render, the digest row
    is UPSERTed into weekly_digests (US-071); --dry-run (US-072) and perf
    (US-073) hang off this same ordering.
    """
    started = time.time()
    steps: list[str] = []

    sessions = _step_scan(since_days)
    steps.append("scan")

    tasks = _step_cluster(sessions)
    steps.append("cluster")

    pass1 = _step_pass1(sessions, tasks)
    steps.append("pass1")

    pass2_results = _step_pass2(sessions, pass1)
    steps.append("pass2")

    moments = _step_validate_moments(sessions, pass1, pass2_results)
    steps.append("validate_moments")

    selection = _step_select_moments(sessions, moments)
    steps.append("select")

    final_results: dict[str, JudgeResult] = dict(pass1.results)
    final_results.update(pass2_results)
    snapshot = ProfileSnapshot.from_scores([])

    week_iso = iso_week_tag(_utcnow())
    follow_up = _step_follow_up(selection, moments, snapshot, week_iso)
    steps.append("follow_up")

    rendered_html, rendered_terminal = _step_render(
        sessions, tasks, selection, follow_up, snapshot
    )
    steps.append("render")

    # Trajectory is computed off the scanned sessions, NOT off any step's
    # output: it is summary metadata for the digest row, not part of the
    # spec 9.4 pipeline. Compute it here so the digest write below can
    # populate trajectory_label / trajectory_headline (spec section 14).
    sessions_with_signals = [(s, extract_signals(s)) for s in sessions]
    trajectory = assess_trajectory(sessions_with_signals)

    cost_total_usd: float | None = None
    cost_baseline_usd: float | None = None

    digest_persisted = False
    if store is None:
        store = ProfileStore()
    headline_moment_id = (
        selection.headline_moment_id if selection is not None else None
    )
    html_path = rendered_html if rendered_html else None
    store.save_weekly_digest(
        week_iso=week_iso,
        trajectory_label=trajectory.label.value,
        trajectory_headline=trajectory.headline,
        snapshot=snapshot,
        headline_moment_id=headline_moment_id,
        cost_total_usd=cost_total_usd,
        cost_baseline_usd=cost_baseline_usd,
        html_path=html_path,
    )
    digest_persisted = True

    return WeeklyRunSummary(
        week_iso=week_iso,
        sessions=sessions,
        tasks=tasks,
        judge_results=final_results,
        moments=moments,
        selection=selection,
        snapshot=snapshot,
        rendered_html=rendered_html,
        rendered_terminal=rendered_terminal,
        elapsed_seconds=round(time.time() - started, 2),
        trajectory=trajectory,
        cost_total_usd=cost_total_usd,
        cost_baseline_usd=cost_baseline_usd,
        digest_persisted=digest_persisted,
        steps_executed=steps,
    )


def _snapshot_from_rows(rows: list[dict]) -> ProfileSnapshot:
    """Rebuild a ProfileSnapshot from persisted session rows."""
    if not rows:
        return ProfileSnapshot.from_scores([])

    scores: list[SessionScore] = []
    from praxis.scoring.features import SessionFeatures
    from praxis.scoring.judge import JudgeResult

    for row in rows:
        if not row["judge_result"]:
            # Sessions can only be persisted via the judge path; rows missing
            # a judge result come from earlier builds and are not scoreable.
            continue
        features = SessionFeatures(**row["features"])
        jr = row["judge_result"]
        judge = JudgeResult(
            dimension_scores=jr["dimension_scores"],
            rationale=jr.get("rationale", {}),
            standout_moments=jr.get("standout_moments", []),
            failure_modes=jr.get("failure_modes", []),
            overall_note=jr.get("overall_note", ""),
            judge_model=jr.get("judge_model", ""),
        )
        scores.append(
            SessionScore(
                session_stable_id=row["stable_id"],
                provider=row["provider"],
                started_at=datetime.fromisoformat(row["started_at"]),
                dimension_scores=row["dimension_scores"],
                overall=row["overall"],
                judge_result=judge,
                features=features,
                source_path=row["source_path"],
            )
        )
    return ProfileSnapshot.from_scores(scores)
