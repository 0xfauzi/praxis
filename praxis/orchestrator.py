"""Orchestrator.

Coordinates scanning, scoring, persistence, and daily consolidation.
This is the brain of the daemon.

Honest framing on "continuous learning": this runs once per invocation.
The continuous part comes from being scheduled (cron / launchd / systemd
timer / Task Scheduler), with deduplication via session stable IDs so
re-runs are idempotent and cheap.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from praxis.follow_up import FollowUp
    from praxis.reports.commitment_rollup import CommitmentRollup


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_started_at(value: str) -> datetime:
    """Parse a stored ``started_at`` string, coercing naive values to UTC.

    Rows written before the scanners normalized timezones (or hand-edited
    DBs) can hold a naive timestamp; comparing one against the aware UTC
    week bounds below raises "can't compare offset-naive and offset-aware
    datetimes" and would otherwise crash the entire weekly run on one row.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


from pathlib import Path

from praxis.behavior import (
    BehavioralSignals,
    TrajectoryAssessment,
    TrajectoryLabel,
    iso_week_tag,
)
from praxis.behavior import (
    assess as assess_trajectory,
)
from praxis.behavior import (
    extract as extract_signals,
)
from praxis.behavior.aug_auto import (
    AugAutoError,
    AugAutoParseError,
    AugAutoResult,
    AugAutoUnavailableError,
    classify_session,
)
from praxis.models import Moment as JudgeMoment
from praxis.models import Session
from praxis.models_advisor import ModelUsageProfile, build_profiles
from praxis.reports.commitment_rollup import (
    build_commitment_rollup,
    fetch_self_report_tally,
)
from praxis.reports.gap_judge import apply_gap_prose
from praxis.scanners import ALL_SCANNERS
from praxis.scoring.aggregate import (
    ProfileSnapshot,
    SessionScore,
    score_one_session_pass1,
    score_one_session_pass2,
)
from praxis.scoring.clustering import Task, cluster_sessions
from praxis.scoring.coach import Coaching, generate_coaching
from praxis.scoring.cost_ledger import estimate_session_cost_usd
from praxis.scoring.judge import JudgeResult, verify_moment_substrings
from praxis.scoring.moment_selector import (
    Moment as SelectorMoment,
)
from praxis.scoring.moment_selector import (
    MomentCandidate,
    MomentSelection,
    select_moments_with_fallback,
)
from praxis.storage.profile_store import ProfileStore

NO_API_KEY_MESSAGE = (
    "No API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your environment and retry."
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
    # Sessions that errored mid-scoring and were skipped so the batch could
    # finish. Default 0 for back-compat with existing constructors/tests.
    sessions_skipped: int = 0
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


def _task_id_for(task) -> str:
    """sha256(sorted session ids)[:16] - matches the spec section 14 contract."""
    import hashlib

    sids = sorted(task.session_ids)
    return hashlib.sha256("|".join(sids).encode("utf-8")).hexdigest()[:16]


def _task_project_hint(task, sessions) -> str | None:
    """Pick the most-common project_hint across the task's sessions."""
    by_id = {s.stable_id: s for s in sessions}
    hints = [
        by_id[sid].project_hint
        for sid in task.session_ids
        if sid in by_id and by_id[sid].project_hint
    ]
    if not hints:
        return None
    # Most common (small lists, no need for Counter)
    counts: dict[str, int] = {}
    for h in hints:
        counts[h] = counts.get(h, 0) + 1
    return max(counts, key=lambda k: counts[k])


@dataclass
class _SummaryView:
    """Read-only view of an in-flight WeeklyRunSummary for the renderer adapter.

    The adapter reads a fixed set of attributes from the summary; we
    can pass it this view before WeeklyRunSummary is constructed (so
    the digest renderer sees the actual data being assembled, not a
    placeholder). Keeps the adapter contract narrow.
    """

    week_iso: str
    sessions: list
    tasks: list
    judge_results: dict
    moments: list
    selection: object | None
    snapshot: object
    trajectory: object | None
    cost_total_usd: float | None
    cost_baseline_usd: float | None
    last_week_means: dict[str, float] | None
    commitment_rollup: CommitmentRollup | None = None


def _gather_sessions(since_days: int | None = None) -> list[Session]:
    """Run every scanner and collect sessions."""
    since_ts: float | None = None
    if since_days is not None:
        since_ts = time.time() - since_days * 86400

    sessions: list[Session] = []
    for scanner_cls in ALL_SCANNERS:
        scanner = scanner_cls()
        try:
            sessions.extend(scanner.scan(since=since_ts))
        except Exception as exc:  # noqa: BLE001
            print(
                f"[orchestrator] {scanner.provider_name} scanner failed: {exc!r}", file=sys.stderr
            )
    return sessions


def run(
    since_days: int | None = 30,
    max_new_scored: int | None = None,
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
    sessions = [s for s in sessions if s.user_authored_turns]
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
    sharpen_calibration = rolling_total > 0 and rolling["high"] / rolling_total > 0.9
    stricter_low = rolling_total > 0 and rolling["low"] / rolling_total > 0.7
    calibration_notice: str | None = (
        "calibration was off; re-tuned" if sharpen_calibration else None
    )

    pass1_confidence_counts = {"low": 0, "medium": 0, "high": 0}
    scored_count = 0
    skipped_count = 0
    for session in to_score:
        # Resilience: one malformed/unscorable session (a parse error in
        # _session_score_from_judge, a DB hiccup, an unexpected judge shape)
        # must never abort the whole batch. Already-scored sessions are saved
        # per-session above, so we log the failure, skip the session, and keep
        # going. The judge call itself already isolates network/API errors.
        try:
            # Spec §9.1 (US-027): pass 1 runs on every session in the window
            # using the cheap-tier judge. No heuristic features gate this call.
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
            # Spec §9.1 (US-028): when pass 1 self-flags as low confidence,
            # re-judge on the frontier model in a fresh call (no pass-1
            # context). The pass-2 result overrides pass-1's scores, rationale,
            # and moments. If pass 2 fails (no key, transient error), keep the
            # pass-1 score rather than leaving the session unjudged.
            if pass1_score.judge_result.confidence == "low":
                pass2_score = score_one_session_pass2(session)
                if pass2_score is not None:
                    # Persist pass-2 alongside the existing pass-1 row
                    # (composite primary key (stable_id, judge_pass) keeps both).
                    store.save_session_score(pass2_score)
                    winning_score = pass2_score
            if winning_score.judge_result is not None:
                # Moments come from the winning judgment (pass 2 when escalation
                # happened, pass 1 otherwise). Only one set of moments per
                # session is persisted to avoid duplicate coaching items.
                store.save_moments(session.stable_id, winning_score.judge_result.moments)
            scored_count += 1
        except Exception as exc:  # noqa: BLE001 -- one bad session must not abort the batch
            skipped_count += 1
            print(
                f"[orchestrator] skipped session {session.stable_id}: {exc!r}",
                file=sys.stderr,
            )

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

    # Daily consolidation was a v0.1 concept; v0.2 replaces it with the
    # weekly digest (see run_weekly). The legacy `run()` keeps the
    # snapshot + coaching computation so `praxis scan` still produces
    # a one-line summary, but no longer writes a daily row.
    rows = store.load_session_scores(since=_utcnow() - timedelta(days=since_days or 30))
    snapshot = _snapshot_from_rows(rows)
    coaching = generate_coaching(snapshot)
    consolidated_for = None

    # Behavioral analysis + per-model advice both need the actual Session
    # objects (not just persisted score rows), so we extract signals from
    # the freshly-scanned sessions in this run's window.
    sessions_in_window = [
        s
        for s in sessions
        if (_utcnow() - timedelta(days=since_days or 30)).timestamp() <= s.started_at.timestamp()
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
        notes=f"scored={scored_count}, skipped={skipped_count}, "
        f"trajectory={trajectory.label.value}, "
        f"models={len(model_profiles)}",
    )

    return RunSummary(
        sessions_seen=len(sessions),
        sessions_new=len(new_sessions),
        sessions_scored=scored_count,
        sessions_skipped=skipped_count,
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
    # Set by run_weekly(explain_judging=True) so the CLI can print
    # the pass-1 confidence distribution after the run. The two-pass
    # judge calibration story has not landed yet, so today an empty
    # dict signals "explain requested, nothing measured."
    judging_confidence: dict[str, int] | None = None
    # True when run_weekly(frontier_only=True) was used. Surfaced so
    # renderers and the explain-judging block can note that pass-1
    # was bypassed.
    forced_frontier: bool = False
    # Spec section 8.1: per-dim means from the immediately prior ISO
    # week. None when the precondition (2+ weeks of data) is not met
    # so the renderer omits the faded last-week annotation.
    last_week_means: dict[str, float] | None = None
    # Spec section 2 (coaching-reposition): rollup of the active
    # commitment's status for the masthead. None when no follow-up
    # exists for the rendered week so the masthead's commitment block
    # is omitted rather than rendered with empty data.
    commitment_rollup: CommitmentRollup | None = None


def _step_scan(since_days: int) -> list[Session]:
    """Step 1: gather sessions from every provider scanner.

    Output feeds: cluster (step 2), pass1 (step 3).
    """
    sessions = _gather_sessions(since_days=since_days)
    return [s for s in sessions if s.user_authored_turns]


def _step_cluster(sessions: list[Session]) -> list[Task]:
    """Step 2: cluster sessions into tasks via one cheap-tier LLM call.

    Output feeds: pass1 (batching constraint, spec 9.4), render (tasks panel).
    """
    if not sessions:
        return []
    tasks = cluster_sessions(sessions)
    return tasks or []


def _session_transcript_for_classifier(session: Session) -> str:
    """Render a session's turns as plain text for the aug_auto classifier.

    The classifier wants raw transcript content - it has its own length
    cap inside classify_session, so this helper only stitches role-tagged
    blocks together without further trimming.
    """
    blocks: list[str] = []
    for turn in session.turns:
        role = turn.role.value
        blocks.append(f"<{role}> {turn.content} </{role}>")
    return "\n".join(blocks)


def _classify_and_persist(
    session: Session,
    store: ProfileStore,
    unavailable_logged: list[bool],
) -> None:
    """Run the aug_auto classifier for one session and write the result.

    Per US-010 acceptance criteria:
      - When no API key is set, log once per run and leave the aug_auto
        columns NULL.
      - On AugAutoParseError, log the rationale (the exception message)
        and leave the columns NULL.
      - On success, persist classification + confidence.

    Failures here must never abort the rest of the pipeline.
    """
    transcript = _session_transcript_for_classifier(session)
    try:
        result: AugAutoResult = classify_session(transcript)
    except AugAutoUnavailableError:
        if not unavailable_logged[0]:
            print(
                "[orchestrator] aug_auto classifier unavailable "
                "(no API key); aug_auto columns will be NULL for this run.",
                file=sys.stderr,
            )
            unavailable_logged[0] = True
        return
    except AugAutoParseError as exc:
        print(
            f"[orchestrator] aug_auto classifier parse error for {session.stable_id}: {exc}",
            file=sys.stderr,
        )
        return
    except AugAutoError as exc:
        print(
            f"[orchestrator] aug_auto classifier error for {session.stable_id}: {exc!r}",
            file=sys.stderr,
        )
        return
    store.save_session_aug_auto(session.stable_id, result.classification, result.confidence)


def _step_pass1(
    sessions: list[Session],
    tasks: list[Task],
    *,
    frontier_only: bool = False,
    max_new: int | None = None,
) -> Pass1Output:
    """Step 3: batched cheap-tier judge with same-task exclusion (spec 9.4).

    Per spec 9.1 the cheap judge runs on every session; this story (US-070)
    wires the ordering but reuses the per-session judge path (US-073 will
    add the batching mechanic). `tasks` is accepted here so the batching
    constraint can be enforced when the batched path lands.

    Issue #5: sessions whose stable_id is already in ``session_scores``
    are skipped before the judge call. ``save_session_score`` was already
    idempotent at the DB layer, but the judge call was still being made
    (and billed). With the early skip, re-running ``praxis review`` over
    the same week is effectively free.

    Known limitation of the early skip: ``store.has_session`` returns
    True if EITHER a pass-1 OR a pass-2 row exists. A session whose
    pass-1 succeeded but whose pass-2 call errored transiently (no row
    saved) will NOT auto-retry pass-2 on the next weekly run -- the
    pass-1 row alone keeps it filtered out here, so pass-2 never gets
    re-flagged. Users hit by this can force a fresh frontier judge via
    ``praxis re-score <stable_id>``. A future fix could restrict the
    skip to sessions whose pass-1 confidence was not ``low``, or
    re-flag sessions that are missing their expected pass-2 row.

    ``max_new`` caps how many NEW (not-yet-scored) sessions get the
    judge treatment. ``None`` or ``0`` means unbounded (the weekly run's
    historical default). When set, the cap is applied AFTER the
    already-scored filter and AFTER sorting newest-first, so the most
    recent unjudged sessions win.

    US-010: the augmentation-vs-automation classifier runs alongside the
    rubric judge here, once per session. Its failure modes (no API key /
    parse error) are absorbed by `_classify_and_persist` so the rest of
    the pipeline continues; the aug_auto columns stay NULL on failure.

    Output feeds: pass2 (low-confidence subset only), validate.
    """
    _ = tasks  # spec-true 5-session batched prompt lands later; for now
    # we fan out single-session judge calls in parallel to get the same
    # wall-time win (5x), at the same per-call cost. Real batching gives
    # both wall-time AND cost reductions; this version only gets the
    # wall-time half. The same-task exclusion from build_pass1_batches
    # is moot here because each session goes to its own LLM call.
    results: dict[str, JudgeResult] = {}
    low_confidence: list[str] = []
    store = ProfileStore()
    if not sessions:
        return Pass1Output(results=results, low_confidence_session_ids=low_confidence)

    if frontier_only:
        # --frontier-only (spec §9.6): skip pass-1 entirely and force the
        # frontier judge on every session. Flag every session id as
        # 'low confidence' so _step_pass2 picks them up. The already-
        # scored filter applies here too: the frontier judge in
        # _step_pass2 also doesn't need to re-judge stable_ids that
        # already have a score row.
        return Pass1Output(
            results={},
            low_confidence_session_ids=[
                s.stable_id for s in sessions if not store.has_session(s.stable_id)
            ],
        )

    # Issue #5: filter sessions already in the store BEFORE any LLM call.
    # ``score_one_session_pass1`` is the expensive step (cheap-tier judge +
    # aug/auto classifier); the DB-side idempotency in save_session_score
    # only kicks in AFTER both have run. Doing the filter here makes a
    # re-run effectively free in tokens.
    to_score = [s for s in sessions if not store.has_session(s.stable_id)]
    if max_new is not None and max_new > 0 and len(to_score) > max_new:
        # Newest-first ordering matches the `praxis scan` cap (spec 9.1)
        # so a budget-bounded run favours the user's most recent work.
        to_score = sorted(to_score, key=lambda s: s.started_at, reverse=True)[:max_new]
    if not to_score:
        return Pass1Output(results=results, low_confidence_session_ids=low_confidence)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 5 workers matches PASS1_BATCH_SIZE = 5 from the spec. Each thread
    # holds one in-flight OpenAI/Anthropic HTTP request; the SDK's own
    # connection pool handles concurrency safely.
    max_workers = min(5, len(to_score))
    futures = {}
    # Pre-compute behavioral signals so persistence carries them for the
    # weekly-bucketed trajectory model (spec §7).
    signals_by_id = {s.stable_id: extract_signals(s) for s in to_score}
    from dataclasses import asdict as _dc_asdict

    # Mutable one-element list so the per-session classifier helper can
    # flip the "already logged?" gate without needing a nonlocal.
    aug_auto_unavailable_logged: list[bool] = [False]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for s in to_score:
            futures[pool.submit(score_one_session_pass1, s)] = s
        for fut in as_completed(futures):
            session = futures[fut]
            try:
                score = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[orchestrator] pass-1 failed for {session.stable_id}: {exc!r}",
                    file=sys.stderr,
                )
                continue
            if score is None or score.judge_result is None:
                continue
            signals_dict = _dc_asdict(signals_by_id[session.stable_id])
            store.save_session_score(score, signals=signals_dict)
            results[session.stable_id] = score.judge_result
            if score.judge_result.confidence == "low":
                low_confidence.append(session.stable_id)
            # Spec US-010: classify each scored session for the aug_auto
            # side-channel. The classifier is a side-channel data collector;
            # its failure must not stop the rest of the run, hence the
            # broad-but-typed handling in _classify_and_persist.
            _classify_and_persist(session, store, aug_auto_unavailable_logged)
    return Pass1Output(results=results, low_confidence_session_ids=low_confidence)


def _step_pass2(sessions: list[Session], pass1: Pass1Output) -> dict[str, JudgeResult]:
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
    store = ProfileStore()
    for sid, session in by_id.items():
        score = score_one_session_pass2(session)
        if score is None or score.judge_result is None:
            continue
        store.save_session_score(score)  # judge_pass=2 row coexists with pass-1
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
    from dataclasses import replace as _replace

    from praxis.models import compute_moment_id

    for sid, judge in final_results.items():
        session = by_id.get(sid)
        if session is None:
            continue
        verified = verify_moment_substrings(session, judge.moments)
        # The judge does not know session_stable_id or the deterministic
        # moment_id (sha256(stable_id|dim_key|turn_index)[:16]); populate
        # them here so downstream consumers (selector, save_moments,
        # follow-up engine) have stable identifiers to work with.
        for m in verified:
            survivors.append(
                _replace(
                    m,
                    session_stable_id=sid,
                    moment_id=compute_moment_id(sid, m.dim_key, m.turn_index),
                )
            )
    return survivors


def _recent_headline_alternatives(
    store: ProfileStore, current_week_iso: str, lookback: int = 3
) -> list[str]:
    """Suggested_alternative strings from the previous N weeks' headline moments.

    Spec section 4.3 input: 'whether this same suggested_alternative was
    flagged in any of the previous 3 weeks (recurrence_count)'. Returns
    the list (oldest first) so the selector can count per-candidate matches.
    Empty when no prior digests exist.
    """
    alternatives: list[str] = []
    iso = current_week_iso
    for _ in range(lookback):
        iso = _prior_iso_week(iso)
        digest = store.load_weekly_digest(iso)
        if digest is None:
            continue
        moment_id = digest.get("headline_moment_id")
        if not moment_id:
            continue
        moment = store.load_moment_by_id(moment_id)
        if moment is None:
            continue
        alt = moment.get("suggested_alternative") or ""
        if alt:
            alternatives.append(alt)
    return alternatives


def _step_select_moments(
    sessions: list[Session],
    moments: list[JudgeMoment],
    recurrence_alternatives: list[str] | None = None,
) -> MomentSelection | None:
    """Step 6: one LLM call picks headline + up to two supporting moments.

    `recurrence_alternatives` carries the headline suggested_alternative
    strings from the previous up-to-3 weeks. Spec section 4.3 uses this
    to escalate framing when the same lapse recurs.

    Output feeds: follow_up (commitment derives from headline), render.
    """
    if not moments:
        return None
    started_by_id = {s.stable_id: s.started_at for s in sessions}
    past_alts = recurrence_alternatives or []
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
        # Count how many prior weeks had a headline with the same alt text.
        recurrence_count = sum(1 for alt in past_alts if alt == jm.suggested_alternative)
        candidates.append(
            MomentCandidate(
                moment=selector_moment,
                session_started_at=started,
                recurrence_count=recurrence_count,
            )
        )
    if not candidates:
        return None
    # Pick the primary provider based on configured API keys. The selector
    # default is Anthropic; if ANTHROPIC_API_KEY is unset, the Anthropic
    # SDK raises TypeError before any LLM call (which the
    # InvalidMomentSelectionError fallback can't catch). Forwarding the
    # provider explicitly avoids that whole class of failure.
    primary = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "openai"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        return None
    return select_moments_with_fallback(candidates, primary_provider=primary)


def _step_follow_up(
    selection: MomentSelection | None,
    moments: list[JudgeMoment],
    snapshot: ProfileSnapshot,
    week_iso: str,
    verification_rate: float = 0.0,
    delegation_rate: float = 0.0,
) -> FollowUp | None:
    """Step 7: build this week's commitment from the headline moment.

    `verification_rate` and `delegation_rate` capture this week's actual
    rates so the FollowUp's baseline_value reflects real behavior, not
    a hardcoded zero. Next week's run reads back the row, computes the
    new rates the same way, and decides outcome via close_follow_up.
    Output feeds: render (follow-up panel) + persistence in run_weekly.
    """
    if selection is None:
        return None
    from praxis.follow_up import FollowUp, HeadlineMoment, build_follow_up  # noqa: F401

    headline = next(
        (m for m in moments if m.moment_id == selection.headline_moment_id),
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
        verification_rate=verification_rate,
        delegation_rate=delegation_rate,
    )


_WEEKLY_LABEL_MAP = {
    "Learning": TrajectoryLabel.LEARNING,
    "Growing autonomy": TrajectoryLabel.GROWING_AUTONOMY,
    "Steady": TrajectoryLabel.STEADY,
    "Drifting": TrajectoryLabel.DRIFTING,
    "Atrophying": TrajectoryLabel.ATROPHYING,
    "Reading": TrajectoryLabel.READING,
}


def assess_trajectory_weekly(
    store: ProfileStore,
    current_week_iso: str,
    sessions_with_signals: list[tuple[Session, BehavioralSignals]],
) -> TrajectoryAssessment:
    """Spec section 7 trajectory: weekly buckets + hysteresis-gated label.

    Loads up to 90 days of persisted session_scores + signals_json,
    converts to WeeklySessionInputs, buckets them by ISO week, and runs
    the spec-compliant label_trajectory_with_hysteresis. The prior
    week's persisted trajectory_label feeds hysteresis.

    Falls back to the legacy per-session `assess_trajectory_heuristic`
    when no signals are on file (fresh DB, or all rows pre-date the
    signals_json migration). That preserves a useful headline for
    week one until the weekly model has 4+ buckets.
    """
    from praxis.behavior.labels import label_trajectory_with_hysteresis
    from praxis.behavior.trajectory import assess_trajectory_heuristic
    from praxis.behavior.weekly import (
        WeeklySessionInput,
        bucket_sessions_by_iso_week,
    )

    # Read 90-day window
    since_dt = _utcnow() - timedelta(days=90)
    rows = store.load_session_scores(since=since_dt)
    inputs: list[WeeklySessionInput] = []
    for row in rows:
        raw_signals = row.get("signals_json")
        if not raw_signals:
            continue
        try:
            sig = json.loads(raw_signals)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        inputs.append(
            WeeklySessionInput(
                started_at=_parse_started_at(row["started_at"]),
                engagement_rate=float(sig.get("engagement_rate", 0.0)),
                delegation_rate=float(sig.get("delegation_rate", 0.0)),
                independence_rate=float(sig.get("independence_rate", 0.0)),
                dim_scores=row.get("dimension_scores") or {},
            )
        )

    # Always run legacy heuristic for engagement/delegation slope + headline.
    legacy = assess_trajectory_heuristic(sessions_with_signals)

    if not inputs:
        return legacy

    buckets = bucket_sessions_by_iso_week(inputs)
    # Hysteresis input: last week's persisted label (if any).
    prior_label_str: str | None = None
    prior_digest = store.load_weekly_digest(_prior_iso_week(current_week_iso))
    if prior_digest:
        prior_label_str = prior_digest.get("trajectory_label")
    from praxis.behavior.labels import WeeklyTrajectoryLabel as _WTL

    prior_wlabel = None
    if prior_label_str:
        try:
            prior_wlabel = _WTL(prior_label_str)
        except ValueError:
            prior_wlabel = None
    weekly_label = label_trajectory_with_hysteresis(buckets, prior_wlabel)

    new_label = _WEEKLY_LABEL_MAP.get(weekly_label.value, legacy.label)
    return TrajectoryAssessment(
        label=new_label,
        engagement_slope=legacy.engagement_slope,
        delegation_slope=legacy.delegation_slope,
        headline=legacy.headline,
        evidence=legacy.evidence,
        risks=legacy.risks,
        interventions=legacy.interventions,
    )


def _compute_week_rates(sessions: list[Session]) -> tuple[float, float]:
    """Aggregate verification_rate + delegation_rate across the week's sessions.

    verification_rate is share of user turns with a verification marker
    (sourced from SessionFeatures.marker_hit_counts). delegation_rate is
    the mean of per-session BehavioralSignals.delegation_rate. Both
    feed the FollowUp baseline so we can measure improvement next week.
    """
    from praxis.scoring.features import extract as extract_features

    if not sessions:
        return 0.0, 0.0
    total_user_turns = 0
    total_verify_hits = 0
    delegation_rates: list[float] = []
    for s in sessions:
        f = extract_features(s)
        ut = len(s.user_authored_turns)
        if ut:
            total_user_turns += ut
            total_verify_hits += f.marker_hit_counts.get("verification", 0)
        sig = extract_signals(s)
        delegation_rates.append(sig.delegation_rate)
    verification_rate = total_verify_hits / total_user_turns if total_user_turns else 0.0
    delegation_rate = sum(delegation_rates) / len(delegation_rates) if delegation_rates else 0.0
    return verification_rate, delegation_rate


def _count_sessions_in_iso_week(store: ProfileStore, week_iso: str) -> int:
    """Count persisted session_scores rows whose started_at falls in the ISO week.

    Used by the commitment rollup so the masthead can show
    "sessions this week vs last week" without making the renderer
    issue its own SQL. Pass-2 rows override pass-1, so we dedupe via
    `load_session_scores`'s default behavior.
    """
    try:
        week_start, week_end = parse_iso_week(week_iso)
    except InvalidWeekError:
        return 0
    since_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
    until_dt = datetime.combine(week_end, datetime.min.time(), tzinfo=UTC)
    rows = store.load_session_scores(since=since_dt)
    return sum(1 for row in rows if _parse_started_at(row["started_at"]) < until_dt)


def _close_prior_follow_up(
    store: ProfileStore,
    current_week_iso: str,
    snapshot: ProfileSnapshot,
    verification_rate: float,
    delegation_rate: float,
) -> None:
    """Find the most recent pending follow-up before this week and close it.

    Per spec section 6.3: each weekly run reads the prior week's FollowUp,
    measures this week's value of its target_metric, computes outcome via
    pure threshold rules, and saves the closed row back. The LLM does not
    decide the outcome - that's a structural enforcement of the feedback-
    loop contract.
    """
    from praxis.follow_up import close_follow_up

    prior = store.latest_follow_up()
    if prior is None:
        return
    if prior.week_iso == current_week_iso:
        return
    if prior.outcome != "pending":
        return
    closed = close_follow_up(prior, snapshot, verification_rate, delegation_rate)
    store.save_follow_up(closed)


def _step_render(
    sessions: list[Session],
    tasks: list[Task],
    selection: MomentSelection | None,
    follow_up: object | None,
    snapshot: ProfileSnapshot,
    *,
    dry_run: bool = False,
    week_iso: str = "",
    trajectory: TrajectoryAssessment | None = None,
    cost_total_usd: float | None = None,
    cost_baseline_usd: float | None = None,
    judge_results: dict[str, JudgeResult] | None = None,
    moments: list[JudgeMoment] | None = None,
    last_week_means: dict[str, float] | None = None,
    commitment_rollup: CommitmentRollup | None = None,
) -> tuple[str, str]:
    """Step 8: produce HTML + terminal renderings of the digest.

    Assembles a `_SummaryView` from the steps above plus the orchestrator
    metadata (trajectory, cost), feeds the adapter, and calls both the
    HTML and terminal digest renderers. `dry_run` is forwarded so future
    file-writing concerns can branch here without changing the contract.
    The first five positional parameters are the documented step-data-flow
    contract from spec 9.4 (US-070); the rest are kwargs so the contract
    stays stable.
    """
    from praxis.reports import digest_html as _dh
    from praxis.reports import digest_terminal as _dt
    from praxis.reports.adapter import build_html_digest, build_terminal_digest

    _ = dry_run  # currently informational; renderer write-paths read it via summary
    view = _SummaryView(
        week_iso=week_iso,
        sessions=sessions,
        tasks=tasks,
        judge_results=judge_results or {},
        moments=moments or [],
        selection=selection,
        snapshot=snapshot,
        trajectory=trajectory,
        cost_total_usd=cost_total_usd,
        cost_baseline_usd=cost_baseline_usd,
        last_week_means=last_week_means,
        commitment_rollup=commitment_rollup,
    )
    rendered_html = _dh.render(build_html_digest(view, follow_up))
    rendered_terminal = _dt.render(build_terminal_digest(view, follow_up))
    return rendered_html, rendered_terminal


def run_weekly(
    since_days: int = 7,
    store: ProfileStore | None = None,
    dry_run: bool = False,
    week_iso: str | None = None,
    frontier_only: bool = False,
    explain_judging: bool = False,
    max_new: int | None = 50,
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

    When `week_iso` is set, the run renders that past week's persisted
    data only: scanning and scoring are skipped and the snapshot is
    rebuilt from rows whose ``started_at`` falls in the ISO-week range.

    `frontier_only` and `explain_judging` are CLI-level switches plumbed
    through to the returned summary so callers can surface them; they
    do not yet alter the judge pipeline behavior (US-031 lands a later
    iteration).

    Issue #5: ``max_new`` caps how many already-unjudged sessions are
    sent to the cheap-tier judge in this run. The default of ``50``
    matches the ``praxis scan --max-new`` default so a stray
    ``praxis review`` cannot silently kick off N x LLM calls on a
    busy week. ``None`` or ``0`` means unbounded (preserves the v0.2
    behaviour for callers that opt in). The cap only applies to NEW
    sessions; already-scored stable_ids are skipped before the LLM
    layer regardless of the cap.
    """
    started = time.time()
    steps: list[str] = []

    # Past-week render: do not scan or score. Rebuild the snapshot from
    # persisted session_scores in the ISO-week range; reconstruct the
    # rest (headline moment, follow-up, tasks, weekly_digest metadata)
    # from the same DB so the past-week digest shows what the user saw
    # the day that week was current.
    if week_iso is not None:
        week_start, week_end = parse_iso_week(week_iso)
        if store is None:
            store = ProfileStore()
        since_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        until_dt = datetime.combine(week_end, datetime.min.time(), tzinfo=UTC)
        all_rows = store.load_session_scores(since=since_dt)
        rows = [row for row in all_rows if _parse_started_at(row["started_at"]) < until_dt]
        snapshot = _snapshot_from_rows(rows)
        past_sessions = _reconstruct_sessions_from_score_rows(rows)
        past_follow_up = store.load_follow_up(week_iso)
        past_digest = store.load_weekly_digest(week_iso)

        # Reconstruct the headline moment for that week, if persisted.
        past_selection = None
        past_moments: list[JudgeMoment] = []
        if past_digest and past_digest.get("headline_moment_id"):
            hmid = past_digest["headline_moment_id"]
            m_row = store.load_moment_by_id(hmid)
            if m_row is not None:
                past_moments.append(
                    JudgeMoment(
                        dim_key=m_row["dim_key"],
                        turn_index=int(m_row["turn_index"]),
                        quoted_excerpt=m_row["quoted_excerpt"],
                        why_it_lost_score=m_row["why_it_lost_score"],
                        suggested_alternative=m_row["suggested_alternative"],
                        severity=m_row["severity"],
                        moment_id=m_row["moment_id"],
                        session_stable_id=m_row["session_stable_id"],
                        created_at=datetime.fromisoformat(m_row["created_at"])
                        if m_row.get("created_at")
                        else None,
                        dollar_impact_estimate=m_row.get("dollar_impact_estimate"),
                        minutes_impact_estimate=m_row.get("minutes_impact_estimate"),
                    )
                )
                past_selection = MomentSelection(
                    headline_moment_id=hmid,
                    headline_reason="",
                    supporting_moment_ids=[],
                )

        # Reconstruct the trajectory line if the digest row holds one.
        past_trajectory: TrajectoryAssessment | None = None
        if past_digest:
            label_str = past_digest.get("trajectory_label") or ""
            try:
                label = TrajectoryLabel(label_str)
            except ValueError:
                label = TrajectoryLabel.READING
            past_trajectory = TrajectoryAssessment(
                label=label,
                engagement_slope=0.0,
                delegation_slope=0.0,
                headline=past_digest.get("trajectory_headline") or "",
            )

        # Load tasks that started in the week.
        past_task_rows = store.load_tasks_for_week(since_dt, until_dt)
        past_tasks: list[Task] = [
            Task(
                label=t["label"],
                task_type=t["task_type"],
                session_ids=list(t["session_stable_ids"]),
                rationale="",
                label_source=t["label_source"],
            )
            for t in past_task_rows
        ]

        past_cost_total = past_digest.get("cost_total_usd") if past_digest else None
        past_cost_baseline = past_digest.get("cost_baseline_usd") if past_digest else None

        # Spec section 2 (coaching-reposition): masthead rollup. Pull the
        # prior week's dim means from its persisted digest snapshot,
        # count sessions in this and the immediately prior ISO week, and
        # build the rollup once. None when no follow-up is on file.
        past_prior_iso = _prior_iso_week(week_iso)
        past_prior_means: dict[str, float] | None = None
        past_prior_digest = store.load_weekly_digest(past_prior_iso)
        if past_prior_digest and past_prior_digest.get("snapshot"):
            prior_snap = past_prior_digest["snapshot"]
            prior_dim_means = prior_snap.get("dimension_means") or {}
            if prior_dim_means:
                past_prior_means = {k: float(v) for k, v in prior_dim_means.items()}
        past_prior_sessions = _count_sessions_in_iso_week(store, past_prior_iso)
        past_rollup = build_commitment_rollup(
            follow_up=past_follow_up,
            snapshot=snapshot,
            prior_week_means=past_prior_means,
            sessions_this_week=len(rows),
            sessions_prior_week=past_prior_sessions,
            self_report_tally=fetch_self_report_tally(store, week_iso),
        )
        # US-037: attach constrained-judge prose when the self-report
        # and dim data disagree. The helper is a no-op on agreement
        # and silently returns the input rollup when no API key is
        # configured or the judge call fails.
        past_rollup = apply_gap_prose(past_rollup)

        rendered_html, rendered_terminal = _step_render(
            past_sessions,
            past_tasks,
            past_selection,
            past_follow_up,
            snapshot,
            dry_run=True,
            week_iso=week_iso,
            trajectory=past_trajectory,
            cost_total_usd=past_cost_total,
            cost_baseline_usd=past_cost_baseline,
            judge_results={},
            moments=past_moments,
            commitment_rollup=past_rollup,
        )
        return WeeklyRunSummary(
            week_iso=week_iso,
            sessions=past_sessions,
            tasks=past_tasks,
            judge_results={},
            moments=past_moments,
            selection=past_selection,
            snapshot=snapshot,
            rendered_html=rendered_html,
            rendered_terminal=rendered_terminal,
            elapsed_seconds=round(time.time() - started, 2),
            trajectory=past_trajectory,
            cost_total_usd=past_cost_total,
            cost_baseline_usd=past_cost_baseline,
            digest_persisted=False,
            steps_executed=[],
            judging_confidence={} if explain_judging else None,
            forced_frontier=frontier_only,
            last_week_means=past_prior_means,
            commitment_rollup=past_rollup,
        )

    sessions = _step_scan(since_days)
    steps.append("scan")

    tasks = _step_cluster(sessions)
    steps.append("cluster")

    pass1 = _step_pass1(sessions, tasks, frontier_only=frontier_only, max_new=max_new)
    steps.append("pass1")

    pass2_results = _step_pass2(sessions, pass1)
    steps.append("pass2")

    # Spec §9.6 (US-031): explain-judging shows the pass-1 confidence
    # distribution. Build it from real results when --explain-judging is on.
    confidence_dist: dict[str, int] | None = None
    if explain_judging:
        confidence_dist = {"high": 0, "medium": 0, "low": 0}
        for r in pass1.results.values():
            conf_label: str = getattr(r, "confidence", None) or "medium"
            if conf_label in confidence_dist:
                confidence_dist[conf_label] += 1

    moments = _step_validate_moments(sessions, pass1, pass2_results)
    steps.append("validate_moments")

    # Spec section 4.3 selector input: which suggested_alternative strings
    # were headlines in the previous up-to-3 weeks. Gate on having a store
    # (or an existing DB file under dry_run); the dry-run test asserts we
    # do NOT create profile.db, so a missing file means we skip lookup.
    recurrence_alts: list[str] = []
    _prior_store: ProfileStore | None = store
    if _prior_store is None and not dry_run:
        _prior_store = ProfileStore()
    elif _prior_store is None and dry_run:
        from praxis.storage.profile_store import resolve_home as _rh

        if (_rh() / "profile.db").exists():
            _prior_store = ProfileStore()
    if _prior_store is not None:
        try:
            recurrence_alts = _recent_headline_alternatives(_prior_store, iso_week_tag(_utcnow()))
        except Exception:  # noqa: BLE001
            recurrence_alts = []
    selection = _step_select_moments(sessions, moments, recurrence_alternatives=recurrence_alts)
    steps.append("select")

    final_results: dict[str, JudgeResult] = dict(pass1.results)
    final_results.update(pass2_results)

    # Build the snapshot from persisted rows that fall in the current
    # ISO week, NOT from the freshly scored sessions alone. In tests
    # (and in real runs where users seeded data via `praxis scan`), the
    # current week may already have judged rows the scan won't re-yield.
    #
    # In dry-run mode we won't CREATE profile.db, but we will read it
    # if it already exists (so the user can preview a digest built from
    # already-scanned data). Constructing ProfileStore() also creates
    # the file, so dry-run must check existence first.
    current_week_iso = iso_week_tag(_utcnow())
    cw_start, cw_end = parse_iso_week(current_week_iso)
    cw_since_dt = datetime.combine(cw_start, datetime.min.time(), tzinfo=UTC)
    cw_until_dt = datetime.combine(cw_end, datetime.min.time(), tzinfo=UTC)
    snapshot_store: ProfileStore | None
    if dry_run:
        if store is not None:
            snapshot_store = store
        else:
            from praxis.storage.profile_store import resolve_home

            if (resolve_home() / "profile.db").exists():
                snapshot_store = ProfileStore()
            else:
                snapshot_store = None
    else:
        snapshot_store = store if store is not None else ProfileStore()
    if snapshot_store is not None:
        cw_rows_all = snapshot_store.load_session_scores(since=cw_since_dt)
        cw_rows = [row for row in cw_rows_all if _parse_started_at(row["started_at"]) < cw_until_dt]
        snapshot = _snapshot_from_rows(cw_rows) if cw_rows else ProfileSnapshot.from_scores([])
    else:
        snapshot = ProfileSnapshot.from_scores([])

    week_iso = current_week_iso
    verification_rate, delegation_rate = _compute_week_rates(sessions)
    follow_up = _step_follow_up(
        selection,
        moments,
        snapshot,
        week_iso,
        verification_rate=verification_rate,
        delegation_rate=delegation_rate,
    )
    steps.append("follow_up")

    # Trajectory is summary metadata, not a numbered pipeline step. Spec
    # section 7 uses the weekly-bucketed model with hysteresis when 4+
    # weeks of data are on file; below that, the legacy per-session
    # heuristic provides a useful headline and slope.
    sessions_with_signals = [(s, extract_signals(s)) for s in sessions]
    _traj_store: ProfileStore | None = None
    if not dry_run:
        _traj_store = store if store is not None else ProfileStore()
    elif snapshot_store is not None:
        _traj_store = snapshot_store
    if _traj_store is not None:
        trajectory = assess_trajectory_weekly(_traj_store, week_iso, sessions_with_signals)
    else:
        trajectory = assess_trajectory(sessions_with_signals)

    # Spec 10.1: cost_total_usd is the USER'S spend this week across the
    # models they actually used (estimated from prompt char volume against
    # each model card's pricing). It is NOT praxis's own pipeline cost.
    # Sessions whose model has no card or no per-token pricing
    # (subscription-only Copilot) contribute None and are excluded.
    user_week_total = 0.0
    have_priced_session = False
    for s in sessions:
        # Cost = billable bytes the provider charged for, so we sum
        # every user-role turn including tool-injected preambles
        # (Codex AGENTS.md, Claude Code system-reminders); switching
        # to user_authored_turns here would under-report actual spend.
        chars = sum(len(t.content) for t in s.user_turns)
        c = estimate_session_cost_usd(s.model_hint, chars)
        if c is not None:
            user_week_total += c
            have_priced_session = True
    cost_total_usd: float | None = user_week_total if have_priced_session else None
    cost_baseline_usd: float | None = None

    # Persist tasks/moments/follow-up and read the cost baseline before
    # we render. The renderer reads cost_baseline_usd via the summary;
    # if we render before reading it, the digest shows a zero baseline.
    digest_persisted = False
    if not dry_run:
        if store is None:
            store = ProfileStore()
        cost_baseline_usd = store.weekly_cost_baseline(before_week_iso=week_iso)

        # Persist tasks + task_members for the week's clusters (spec 14).
        for task in tasks:
            if not task.session_ids:
                continue
            task_id = _task_id_for(task)
            session_started = [s.started_at for s in sessions if s.stable_id in task.session_ids]
            t_start = min(session_started) if session_started else _utcnow()
            t_end = max(session_started) if session_started else _utcnow()
            project_hint = _task_project_hint(task, sessions)
            store.save_task(
                task_id=task_id,
                label=task.label,
                task_type=task.task_type,
                project_hint=project_hint,
                started_at=t_start,
                ended_at=t_end,
                session_stable_ids=list(task.session_ids),
                total_cost_estimate_usd=None,
                label_source=task.label_source,
            )

        # Persist surviving moments (already redacted by save_moments).
        # Group by session so save_moments can apply per-session replace.
        moments_by_session: dict[str, list[JudgeMoment]] = {}
        for m in moments:
            if m.session_stable_id:
                moments_by_session.setdefault(m.session_stable_id, []).append(m)
        for sid, ms in moments_by_session.items():
            store.save_moments(sid, ms)

        # Close last week's commitment (spec 6.3) BEFORE saving this
        # week's row, so latest_follow_up() reliably finds the prior one.
        _close_prior_follow_up(
            store,
            week_iso,
            snapshot,
            verification_rate,
            delegation_rate,
        )

        # Persist this week's follow-up commitment (spec 6.3).
        if follow_up is not None:
            store.save_follow_up(follow_up)

    # Spec section 8.1: load the prior ISO week's snapshot so the renderer
    # can show "Planning 6.7 (baseline 5.4)" annotations. None when no
    # prior week is on file - the renderer treats that as "baseline forming".
    last_week_means: dict[str, float] | None = None
    if not dry_run or snapshot_store is not None:
        ws = (
            snapshot_store
            if snapshot_store is not None
            else (store if store is not None else ProfileStore())
        )
        prior_week_iso = _prior_iso_week(week_iso)
        prior_digest = ws.load_weekly_digest(prior_week_iso)
        if prior_digest and prior_digest.get("snapshot"):
            prior_snap = prior_digest["snapshot"]
            prior_dim_means = prior_snap.get("dimension_means") or {}
            if prior_dim_means:
                last_week_means = {k: float(v) for k, v in prior_dim_means.items()}

    # Spec section 2 (coaching-reposition): commitment rollup for the
    # masthead. Built from already-computed state (follow_up, snapshot,
    # last_week_means, session counts) so no fresh I/O is needed beyond
    # the optional session_reflections tally. None when no commitment
    # exists for the week so the masthead's block is omitted.
    rollup_store: ProfileStore | None = store if store is not None else snapshot_store
    if rollup_store is None and not dry_run:
        rollup_store = ProfileStore()
    sessions_prior_week = 0
    if rollup_store is not None:
        sessions_prior_week = _count_sessions_in_iso_week(rollup_store, _prior_iso_week(week_iso))
    self_report_tally = fetch_self_report_tally(rollup_store, week_iso)
    commitment_rollup = build_commitment_rollup(
        follow_up=follow_up,
        snapshot=snapshot,
        prior_week_means=last_week_means,
        sessions_this_week=len(sessions),
        sessions_prior_week=sessions_prior_week,
        self_report_tally=self_report_tally,
    )
    # US-037: attach constrained-judge prose when self-report and
    # dim data disagree. No-op when there's nothing to compare or
    # when no API key / judge failure means the renderer should fall
    # back to the static phrasing.
    commitment_rollup = apply_gap_prose(commitment_rollup)

    # Render last - now that trajectory, cost, and persistence are settled.
    rendered_html, rendered_terminal = _step_render(
        sessions,
        tasks,
        selection,
        follow_up,
        snapshot,
        dry_run=dry_run,
        week_iso=week_iso,
        trajectory=trajectory,
        cost_total_usd=cost_total_usd,
        cost_baseline_usd=cost_baseline_usd,
        judge_results=final_results,
        moments=moments,
        last_week_means=last_week_means,
        commitment_rollup=commitment_rollup,
    )
    steps.append("render")

    if not dry_run:
        # Now that we have the rendered HTML, write the weekly_digests row.
        headline_moment_id = selection.headline_moment_id if selection is not None else None
        store.save_weekly_digest(
            week_iso=week_iso,
            trajectory_label=trajectory.label.value,
            trajectory_headline=trajectory.headline,
            snapshot=snapshot,
            headline_moment_id=headline_moment_id,
            cost_total_usd=cost_total_usd,
            cost_baseline_usd=cost_baseline_usd,
            html_path=None,
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
        judging_confidence=confidence_dist,
        forced_frontier=frontier_only,
        last_week_means=last_week_means,
        commitment_rollup=commitment_rollup,
    )


def _snapshot_from_rows(rows: list[dict]) -> ProfileSnapshot:
    """Rebuild a ProfileSnapshot from persisted session rows."""
    if not rows:
        return ProfileSnapshot.from_scores([])

    scores: list[SessionScore] = []
    # SessionFeatures was renamed/reshaped in the features-module component
    # (heuristics.py -> features.py); pre-rename rows carry extra/legacy
    # keys in features_json. Filter to current fields AND back-fill any
    # required fields that older snapshots did not record so a v0.1/v0.2
    # mixed DB still loads instead of crashing with TypeError.
    from dataclasses import MISSING
    from dataclasses import fields as _dc_fields

    from praxis.scoring.features import SessionFeatures
    from praxis.scoring.judge import JudgeResult

    _CURRENT_FEATURE_FIELDS = {f.name for f in _dc_fields(SessionFeatures)}
    _REQUIRED_FEATURE_DEFAULTS = {
        f.name: 0 if f.type is int else 0.0
        for f in _dc_fields(SessionFeatures)
        if f.default is MISSING and f.default_factory is MISSING  # type: ignore[misc]
    }
    for row in rows:
        if not row["judge_result"]:
            # Sessions can only be persisted via the judge path; rows missing
            # a judge result come from earlier builds and are not scoreable.
            continue
        raw_features = row["features"] or {}
        filtered = {k: v for k, v in raw_features.items() if k in _CURRENT_FEATURE_FIELDS}
        for k, default in _REQUIRED_FEATURE_DEFAULTS.items():
            filtered.setdefault(k, default)
        features = SessionFeatures(**filtered)
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
                started_at=_parse_started_at(row["started_at"]),
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
            f"invalid --week value {week_iso!r}: expected 'YYYY-Www' (for example, 2026-W21)."
        )
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        week_start = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise InvalidWeekError(f"invalid --week value {week_iso!r}: {exc}.") from exc
    return week_start, week_start + timedelta(days=7)


def current_iso_week(now: datetime | None = None) -> str:
    """Return the current ISO-week tag ('YYYY-Www')."""
    if now is None:
        now = _utcnow()
    year, week, _ = now.date().isocalendar()
    return f"{year:04d}-W{week:02d}"


def _prior_iso_week(week_iso: str) -> str:
    """Return the ISO-week tag for the week immediately before ``week_iso``."""
    week_start, _week_end = parse_iso_week(week_iso)
    prior = week_start - timedelta(days=7)
    year, week, _ = prior.isocalendar()
    return f"{year:04d}-W{week:02d}"


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


def _reconstruct_sessions_from_score_rows(rows: list[dict[str, Any]]) -> list[Session]:
    """Re-parse persisted score rows into Sessions for historical reports.

    Past-week renders are read-only: they must not rescan discovery roots
    or re-run scoring, but the expansion panels still need the same
    turn-level Session objects that fresh weekly renders use. The score
    rows carry exact provider/source_path pairs, so we parse only those
    files and keep only sessions whose stable_id still matches the row.

    If a source file has moved or no longer parses, we omit that session
    instead of fabricating a partial Session from aggregate score data.
    """
    sessions: list[Session] = []
    seen: set[str] = set()
    for row in rows:
        stable_id = str(row.get("stable_id") or "")
        provider = str(row.get("provider") or "")
        source_path = row.get("source_path")
        if not stable_id or not provider or not source_path or stable_id in seen:
            continue
        scanner_cls = _scanner_for_provider(provider)
        if scanner_cls is None:
            continue
        scanner = scanner_cls()
        try:
            session = scanner.parse(Path(str(source_path)))
        except Exception as exc:  # noqa: BLE001 -- a moved/corrupt source file must
            # not crash a past-week render; omit the session, as the docstring
            # promises, instead of propagating FileNotFoundError/parse errors.
            print(
                f"[orchestrator] could not re-parse {source_path}: {exc!r}",
                file=sys.stderr,
            )
            continue
        if session is None or session.stable_id != stable_id:
            continue
        aug_auto_label = row.get("aug_auto_classification")
        if isinstance(aug_auto_label, str):
            session.aug_auto_classification = aug_auto_label
        sessions.append(session)
        seen.add(stable_id)
    return sessions


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

    # re-score is authoritative: go straight to the frontier judge.
    score = score_one_session_pass2(session)
    if score is None:
        raise ReScoreError(
            "no_judge",
            "no frontier judge available (set ANTHROPIC_API_KEY or OPENAI_API_KEY and retry).",
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
        started = _parse_started_at(row["started_at"])
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
