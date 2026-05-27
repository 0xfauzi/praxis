"""Orchestrator.

Coordinates scanning, scoring, persistence, and daily consolidation.
This is the brain of the daemon.

Honest framing on "continuous learning": this runs once per invocation.
The continuous part comes from being scheduled (cron / launchd / systemd
timer / Task Scheduler), with deduplication via session stable IDs so
re-runs are idempotent and cheap.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

from praxis.behavior import (
    BehavioralSignals,
    TrajectoryAssessment,
    assess as assess_trajectory,
    extract as extract_signals,
    iso_week_tag,
)
from pathlib import Path

from praxis.models import Moment as JudgeMoment, Session
from praxis.models_advisor import ModelUsageProfile, build_profiles
from praxis.scanners import ALL_SCANNERS
from praxis.scoring.aggregate import (
    ProfileSnapshot,
    SessionScore,
    score_one_session_pass1,
    score_one_session_pass2,
)
from praxis.scoring.clustering import Task, cluster_sessions
from praxis.scoring.coach import Coaching, generate_coaching
from praxis.scoring.cost import estimate_weekly_pipeline_cost
from praxis.scoring.judge import JudgeResult, verify_moment_substrings
from praxis.scoring.moment_selector import (
    Moment as SelectorMoment,
    MomentCandidate,
    MomentSelection,
    select_moments_with_fallback,
)
from praxis.storage.profile_store import ProfileStore


NO_API_KEY_MESSAGE = (
    "No API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY "
    "in your environment and retry."
)


def has_api_key_configured() -> bool:
    """Return True when at least one judge API key is in the environment.

    Spec section 11 makes API keys a hard requirement in v0.2: heuristic-
    only mode is gone and the CLI must refuse to run any command that
    would call the judge when neither ``ANTHROPIC_API_KEY`` nor
    ``OPENAI_API_KEY`` is set. Read-only verbs (baseline, history, show,
    follow-up) do not consult this; only commands that actually invoke
    the judge (``praxis week`` on the current week, ``praxis scan``,
    ``praxis re-score``) should gate on it before doing work.
    """
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def no_sessions_message(week_iso: str | None) -> str:
    """Compose the exit-3 message for ``praxis week`` / ``praxis show``.

    Spec section 12.3 reserves exit code 3 for "no sessions found in the
    window". The CLI surfaces this via a clear message that names the
    affected ISO week so the user knows which window was searched.
    Past-week branches pass the explicit tag; the current-week branch
    passes the current ISO week so the message stays specific.
    """
    label = week_iso if week_iso else "the current week"
    return (
        f"No sessions found in {label}. "
        f"Use an AI coding tool (Claude / Codex / Copilot) "
        f"and run 'praxis scan' to populate the digest."
    )


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
    # Spec §9.6 (US-031): one-line digest banner surfaced when the rolling
    # 4-week pass-1 confidence distribution shows the cheap-tier judge has
    # been over-confident (high > 90%) and the prompt was auto-sharpened
    # for this run. None means no auto-tune was applied.
    calibration_notice: str | None = None
    # Set when run_weekly() targets a specific ISO week (either via
    # --week <iso> for a past week, or by default for the current
    # week). Renderers display this in the masthead. None means the
    # legacy run() entry point was used (since_days window, no week
    # framing).
    week_iso: str | None = None
    # Set by run_weekly(explain_judging=True) so the CLI can print
    # the pass-1 confidence distribution after the run. Maps a label
    # ("high"/"medium"/"low") to the count of sessions in that bucket
    # during pass-1. None means --explain-judging was not requested
    # for this run.
    judging_confidence: dict[str, int] | None = None
    # True when run_weekly(frontier_only=True) was used, so renderers
    # and the explain-judging block can surface that pass-1 was skipped.
    # The two-pass judge lands in a later story; this flag is the
    # stable seam future judge code reads to skip pass-1.
    forced_frontier: bool = False


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
    max_new_scored: int | None = None,
    force_consolidate: bool = False,
) -> RunSummary:
    """Full pipeline. Idempotent: re-running won't re-score known sessions.

    Args:
        since_days: Only consider session files modified in the last N days.
        max_new_scored: Optional cap on how many newly-discovered sessions
            get the judge treatment in one run. Defaults to ``None`` (no
            cap): spec §9.1 mandates that pass 1 runs on every session in
            the weekly window, with no skip path. The parameter is retained
            so power users can override for ad-hoc CLI runs (``praxis scan``
            with a tight budget), but the weekly pipeline always passes None.
        force_consolidate: Run daily consolidation even if one already
            exists for today.
    """
    started = time.time()
    store = ProfileStore()

    sessions = _gather_sessions(since_days=since_days)
    # Filter out sessions with no user turns (system-only / tool-only files).
    # This is a structural filter (nothing to judge), not a heuristic skip.
    sessions = [s for s in sessions if s.user_turns]
    new_sessions = [s for s in sessions if not store.has_session(s.stable_id)]

    # Sort newest first so if a caller overrides the cap, the most recent
    # sessions win. The default has no cap (spec §9.1: pass 1 on every session).
    new_sessions.sort(key=lambda s: s.started_at, reverse=True)
    if max_new_scored is None:
        to_score = new_sessions
    else:
        to_score = new_sessions[:max_new_scored]

    # Spec §9.6 (US-031): consult the rolling 4-week pass-1 confidence
    # distribution before this run's pass-1 calls. If "high" is running
    # above 90%, the cheap-tier judge is over-confident; sharpen the
    # calibration block in the prompt and surface a digest banner. If
    # "low" is running above 70%, the cheap-tier judge is over-flagging;
    # tighten the low-confidence definition. The flags only adjust the
    # pass-1 prompt - pass 2 always re-judges fresh.
    rolling = store.recent_pass1_confidence(weeks=4)
    rolling_total = rolling["low"] + rolling["medium"] + rolling["high"]
    sharpen_calibration = (
        rolling_total > 0 and rolling["high"] / rolling_total > 0.9
    )
    stricter_low = (
        rolling_total > 0 and rolling["low"] / rolling_total > 0.7
    )
    calibration_notice: str | None = (
        "calibration was off; re-tuned" if sharpen_calibration else None
    )

    pass1_confidence_counts = {"low": 0, "medium": 0, "high": 0}
    scored_count = 0
    for session in to_score:
        # Spec §9.1 (US-027): pass 1 runs on every session in the window using
        # the cheap-tier judge. No heuristic features gate this call.
        pass1_score = score_one_session_pass1(
            session,
            sharpen_calibration=sharpen_calibration,
            stricter_low=stricter_low,
        )
        if pass1_score is None:
            # No judge available (no API keys, or judge errored) - skip the
            # session rather than substituting a fallback score.
            continue
        # Spec §9.6 (US-031): count the pass-1 self-confidence before any
        # downstream override, so the rolling 4-week telemetry reflects what
        # the cheap-tier judge actually emitted (pass 2 may overturn the
        # scores, but the calibration signal we tune on is pass 1's read).
        pass1_confidence_counts[pass1_score.judge_result.confidence] += 1
        # Spec §9.6 (US-029): persist pass-1 first so the cheap-tier read is
        # always recorded (judge_pass=1), even when escalation will later add
        # a pass-2 row. Both rows then coexist for audit / disagreement
        # analysis instead of being overwritten.
        store.save_session_score(pass1_score)
        winning_score = pass1_score
        # Spec §9.1 (US-028): when pass 1 self-flags as low confidence, re-judge
        # on the frontier model in a fresh call (no pass-1 context). The pass-2
        # result overrides pass-1's scores, rationale, and moments. If pass 2
        # fails (no key, transient error), keep the pass-1 score rather than
        # leaving the session unjudged.
        if pass1_score.judge_result.confidence == "low":
            pass2_score = score_one_session_pass2(session)
            if pass2_score is not None:
                # Persist pass-2 alongside the existing pass-1 row (composite
                # primary key on (stable_id, judge_pass) keeps both alive).
                store.save_session_score(pass2_score)
                winning_score = pass2_score
        if winning_score.judge_result is not None:
            # Moments come from the winning judgment (pass 2 when escalation
            # happened, pass 1 otherwise). Only one set of moments per session
            # is persisted to avoid surfacing duplicate coaching items.
            store.save_moments(session.stable_id, winning_score.judge_result.moments)
        scored_count += 1

    # Spec §9.6 (US-031): persist this run's pass-1 confidence distribution
    # so future weekly runs can compute the rolling 4-week share. Skip the
    # write when no pass-1 calls succeeded (no API key, empty window) so the
    # run_log isn't polluted with zero rows.
    if any(pass1_confidence_counts.values()):
        store.record_pass1_confidence(
            low=pass1_confidence_counts["low"],
            medium=pass1_confidence_counts["medium"],
            high=pass1_confidence_counts["high"],
        )

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
        calibration_notice=calibration_notice,
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
        score = score_one_session_pass1(session)
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
        score = score_one_session_pass2(session)
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
    *,
    dry_run: bool = False,
) -> tuple[str, str]:
    """Step 8: produce HTML + terminal renderings of the digest.

    The actual layout is owned by `praxis.reports`; this step's job is
    to assemble inputs from the steps above and call the renderers. The
    full wiring lands in a follow-up story (US-071+); this version
    returns empty strings so the ordering contract can be tested first.

    `dry_run` is plumbed through so the future file-writing step (US-073
    or render follow-up) can skip the on-disk write while still computing
    and returning the rendered strings for terminal display.
    """
    _ = sessions, tasks, selection, follow_up, snapshot, dry_run
    return "", ""


def run_weekly(
    since_days: int = 7,
    store: ProfileStore | None = None,
    dry_run: bool = False,
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

    `dry_run=True` (US-072) computes the full pipeline (terminal output
    included via `rendered_terminal`) but writes nothing: no DB row, no
    on-disk file, no ProfileStore construction. Passing an explicit
    `store=` does not override `dry_run`; the contract is that dry-run
    never persists, no matter how it was called.
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
        sessions, tasks, selection, follow_up, snapshot, dry_run=dry_run,
    )
    steps.append("render")

    # Trajectory is computed off the scanned sessions, NOT off any step's
    # output: it is summary metadata for the digest row, not part of the
    # spec 9.4 pipeline. Compute it here so the digest write below can
    # populate trajectory_label / trajectory_headline (spec section 14).
    sessions_with_signals = [(s, extract_signals(s)) for s in sessions]
    trajectory = assess_trajectory(sessions_with_signals)

    # Spec 10.1 / 15.2: cost_total_usd is praxis's own LLM spend on this
    # week's pipeline (cluster + pass1 + pass2 + selector), estimated
    # from per-call token volumes and the judge_model recorded on each
    # JudgeResult. The estimate is rough by design; we do not bill
    # against live invoices. None means "no priced calls happened" -
    # e.g. an empty week, or every judged call used an unpriced model.
    estimated_cost = estimate_weekly_pipeline_cost(
        sessions=sessions,
        pass1_results=pass1.results,
        pass2_results=pass2_results,
        moment_count=len(moments),
    )
    cost_total_usd: float | None = estimated_cost if estimated_cost > 0.0 else None

    cost_baseline_usd: float | None = None

    digest_persisted = False
    if not dry_run:
        if store is None:
            store = ProfileStore()
        # Spec 10.1: baseline is the 90-day rolling weekly mean of prior
        # cost_total_usd. Read BEFORE we UPSERT this week's row so the
        # current week is excluded by data, not just by the < filter.
        cost_baseline_usd = store.weekly_cost_baseline(before_week_iso=week_iso)
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
                judge_pass=row.get("judge_pass", 1),
            )
        )
    return ProfileSnapshot.from_scores(scores)


_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


class InvalidWeekError(ValueError):
    """Raised when --week receives a string that is not 'YYYY-Www'."""


def parse_iso_week(week_iso: str) -> tuple[date, date]:
    """Parse an ISO-week tag (e.g. '2026-W21') to its [Monday, next Monday) range.

    Returns (week_start, week_end) as dates, both UTC. The end is the
    Monday of the following week, so callers can treat [start, end) as a
    half-open interval. Raises InvalidWeekError on malformed input.
    """
    match = _ISO_WEEK_RE.match(week_iso)
    if match is None:
        raise InvalidWeekError(
            f"invalid --week value {week_iso!r}: expected 'YYYY-Www' "
            f"(for example, 2026-W21)."
        )
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        week_start = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise InvalidWeekError(
            f"invalid --week value {week_iso!r}: {exc}."
        ) from exc
    return week_start, week_start + timedelta(days=7)


def current_iso_week(now: datetime | None = None) -> str:
    """Return the current ISO-week tag ('YYYY-Www')."""
    if now is None:
        now = _utcnow()
    year, week, _ = now.date().isocalendar()
    return f"{year:04d}-W{week:02d}"


def run_weekly(
    week_iso: str | None = None,
    dry_run: bool = False,
    frontier_only: bool = False,
    explain_judging: bool = False,
) -> RunSummary:
    """Weekly-cadence entry point used by ``praxis week``.

    The spec's v0.2 pipeline (scan -> cluster -> two-pass judge -> moments
    -> trajectory -> render) is being assembled story by story; this
    function is the stable CLI-facing seam those stories will plug into.
    The flags below define the contract the CLI promises today so the
    surface stays stable as the underlying pipeline lands.

    Args:
        week_iso: ISO-week tag (e.g. ``"2026-W21"``). When set, the run
            renders that past week's persisted data only: scanning and
            scoring are skipped and the snapshot is rebuilt from rows
            whose ``started_at`` falls inside the ISO-week range. The
            week defaults to the current ISO week, in which case the
            full scan+score pipeline runs over the last 7 days.
        dry_run: Compute the digest but skip every write side effect.
            For the current week this means the scan+score pipeline is
            not invoked at all (so no new rows are persisted to
            ``session_scores``/``moments``); the snapshot is rebuilt
            from whatever is already in the DB for the target window.
            ``--write-html`` and ``--notify`` are CLI-level concerns
            and are also honored by the caller.
        frontier_only: Force every session through the frontier judge
            (spec section 9.6's ``praxis week --frontier-only`` switch).
            The two-pass judge lands in a separate story; the flag is
            wired here so future judge code can read it from the same
            run context without another CLI change. No behavioral
            effect today beyond being recorded on the run.
        explain_judging: When true, the returned RunSummary carries a
            ``judging_confidence`` dict so the CLI can print the
            pass-1 confidence distribution per spec 9.6. The two-pass
            judge has not landed yet, so today the dict is empty and
            the CLI documents that explicitly to avoid implying a
            measurement that did not happen.

    Returns:
        A :class:`RunSummary` whose ``week_iso`` field is set to the
        target week (the requested ``--week`` value if provided, else
        the current ISO week). When ``explain_judging`` is true the
        ``judging_confidence`` field is also populated (possibly
        empty) so the CLI knows to print the explainer block.
    """
    started = time.time()
    target_week_iso = week_iso if week_iso is not None else current_iso_week()

    if week_iso is not None:
        # Past-week render: do not scan or score; rebuild the snapshot
        # from rows already persisted for that ISO week.
        week_start, week_end = parse_iso_week(week_iso)
        store = ProfileStore()
        since_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc)
        until_dt = datetime.combine(week_end, datetime.min.time(), tzinfo=timezone.utc)
        all_rows = store.load_session_scores(since=since_dt)
        rows = [
            row for row in all_rows
            if datetime.fromisoformat(row["started_at"]) < until_dt
        ]
        snapshot = _snapshot_from_rows(rows)
        coaching = generate_coaching(snapshot)
        summary = RunSummary(
            sessions_seen=len(rows),
            sessions_new=0,
            sessions_scored=0,
            elapsed_seconds=round(time.time() - started, 2),
            snapshot=snapshot,
            coaching=coaching,
            consolidated_for=None,
            trajectory=None,
            model_profiles=None,
            week_iso=target_week_iso,
            judging_confidence={} if explain_judging else None,
            forced_frontier=frontier_only,
        )
        return summary

    # Current-week render: respect dry_run by skipping the scan+score
    # write path entirely. The rebuild path mirrors the past-week branch.
    if dry_run:
        store = ProfileStore()
        since_dt = _utcnow() - timedelta(days=7)
        rows = store.load_session_scores(since=since_dt)
        snapshot = _snapshot_from_rows(rows)
        coaching = generate_coaching(snapshot)
        return RunSummary(
            sessions_seen=len(rows),
            sessions_new=0,
            sessions_scored=0,
            elapsed_seconds=round(time.time() - started, 2),
            snapshot=snapshot,
            coaching=coaching,
            consolidated_for=None,
            trajectory=None,
            model_profiles=None,
            week_iso=target_week_iso,
            judging_confidence={} if explain_judging else None,
            forced_frontier=frontier_only,
        )

    base = run(since_days=7)
    base.week_iso = target_week_iso
    base.forced_frontier = frontier_only
    if explain_judging:
        base.judging_confidence = {}
    return base


class ReScoreError(RuntimeError):
    """Raised by ``re_score_session`` when re-scoring cannot complete.

    Carries a structured ``code`` so the CLI maps it to an exit code
    without parsing the message:
      - ``"not_found"``  -- no session with that stable_id in session_scores
      - ``"no_judge"``   -- neither ANTHROPIC_API_KEY nor OPENAI_API_KEY set,
                            or every configured judge errored
      - ``"unreadable"`` -- the persisted source_path could not be parsed
                            (file moved, corrupted, or provider unknown)
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _scanner_for_provider(provider: str):
    """Return the scanner class whose ``provider_name`` matches.

    The session_scores row stores the provider as a string (the value of
    ``Provider.<...>.value``); we map it back to the scanner class that
    originally parsed the file. Returns None for an unknown provider so
    the caller can surface a clear "unreadable" error.
    """
    for cls in ALL_SCANNERS:
        if cls.provider_name == provider:
            return cls
    return None


def re_score_session(session_stable_id: str) -> SessionScore:
    """Re-run the frontier judge for one persisted session.

    Looks up the session by stable_id, re-parses its source file via the
    matching scanner, calls the LLM judge, and overwrites the persisted
    row + moments. The judge picked is the frontier judge (the default
    ``score_session`` order is Claude-first, OpenAI-second; both are
    frontier-tier models per the built-in cards).

    Returns the new SessionScore. Raises ReScoreError when the session
    cannot be re-scored; the CLI maps the ``.code`` field to an exit
    code so future stories (US-076) can add the "no API key" exit-2
    behavior without touching this function.
    """
    store = ProfileStore()
    row = store.load_one_session_score(session_stable_id)
    if row is None:
        raise ReScoreError(
            "not_found",
            f"no session with stable_id={session_stable_id!r} in session_scores.",
        )

    scanner_cls = _scanner_for_provider(row["provider"])
    if scanner_cls is None:
        raise ReScoreError(
            "unreadable",
            f"unknown provider {row['provider']!r} for session {session_stable_id}.",
        )
    scanner = scanner_cls()
    source_path = Path(row["source_path"])
    session = scanner.parse(source_path)
    if session is None:
        raise ReScoreError(
            "unreadable",
            f"could not re-parse {source_path}: file missing or malformed.",
        )

    score = score_one_session(session)
    if score is None:
        raise ReScoreError(
            "no_judge",
            "no frontier judge available "
            "(set ANTHROPIC_API_KEY or OPENAI_API_KEY and retry).",
        )
    store.save_session_score(score)
    if score.judge_result is not None:
        store.save_moments(session.stable_id, score.judge_result.moments)
    return score


def list_persisted_weeks() -> list[dict[str, Any]]:
    """List every ISO week that has at least one persisted session score.

    Used by ``praxis history`` to enumerate which past weeks
    ``praxis show <week_iso>`` can render. Each entry has:
      - ``week_iso``: the ISO-week tag, e.g. ``"2026-W21"``
      - ``session_count``: number of judged sessions inside that week
      - ``overall_mean``: mean of ``session_scores.overall`` for that week,
        rounded to two decimals (matches the digest's display precision).

    Returned newest-week first so the CLI lists most-recent first.
    """
    store = ProfileStore()
    rows = store.load_session_scores()
    buckets: dict[str, list[float]] = {}
    for row in rows:
        started = datetime.fromisoformat(row["started_at"])
        year, week, _ = started.date().isocalendar()
        week_iso = f"{year:04d}-W{week:02d}"
        buckets.setdefault(week_iso, []).append(float(row["overall"]))
    result: list[dict[str, Any]] = []
    for week_iso in sorted(buckets.keys(), reverse=True):
        scores = buckets[week_iso]
        result.append(
            {
                "week_iso": week_iso,
                "session_count": len(scores),
                "overall_mean": round(sum(scores) / len(scores), 2),
            }
        )
    return result
