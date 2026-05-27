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
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

from praxis.behavior import (
    BehavioralSignals,
    TrajectoryAssessment,
    assess as assess_trajectory,
    extract as extract_signals,
)
from praxis.models import Session
from praxis.models_advisor import ModelUsageProfile, build_profiles
from praxis.scanners import ALL_SCANNERS
from praxis.scoring.aggregate import (
    ProfileSnapshot,
    SessionScore,
    score_one_session_pass1,
    score_one_session_pass2,
)
from praxis.scoring.coach import Coaching, generate_coaching
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
    # Spec §9.6 (US-031): one-line digest banner surfaced when the rolling
    # 4-week pass-1 confidence distribution shows the cheap-tier judge has
    # been over-confident (high > 90%) and the prompt was auto-sharpened
    # for this run. None means no auto-tune was applied.
    calibration_notice: str | None = None


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
