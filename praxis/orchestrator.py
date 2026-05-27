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
    score_one_session,
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
    use_judge: bool = True,
    since_days: int | None = 30,
    max_new_scored: int = 50,
    force_consolidate: bool = False,
) -> RunSummary:
    """Full pipeline. Idempotent: re-running won't re-score known sessions.

    Args:
        use_judge: If True, call the LLM judge. If False, heuristics only.
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
    # These would otherwise score 0.6/10 via the fit=5.0 fixed baseline, which
    # drags the snapshot dimension means without representing real usage.
    sessions = [s for s in sessions if s.user_turns]
    new_sessions = [s for s in sessions if not store.has_session(s.stable_id)]

    # Sort newest first so if we hit the cap, the most recent get scored.
    new_sessions.sort(key=lambda s: s.started_at, reverse=True)
    to_score = new_sessions[:max_new_scored]
    scored_count = 0
    for session in to_score:
        score = score_one_session(session, use_judge=use_judge)
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

    trajectory = assess_trajectory(sessions_with_signals, prefer_llm=use_judge)

    # Join behavior signals with this run's score rows so the model advisor
    # has overall scores per session where available.
    score_by_stable_id = {row["stable_id"]: row["overall"] for row in rows}
    enriched_for_models: list[tuple[Session, BehavioralSignals, float | None]] = [
        (s, sig, score_by_stable_id.get(s.stable_id)) for s, sig in sessions_with_signals
    ]
    model_profiles = build_profiles(enriched_for_models, use_llm=use_judge)

    store.log_run(
        kind="full" if use_judge else "heuristic",
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


def _snapshot_from_rows(rows: list[dict]) -> ProfileSnapshot:
    """Rebuild a ProfileSnapshot from persisted session rows."""
    if not rows:
        return ProfileSnapshot.from_scores([])

    scores: list[SessionScore] = []
    from praxis.scoring.heuristics import HeuristicFeatures
    from praxis.scoring.judge import JudgeResult

    for row in rows:
        features = HeuristicFeatures(**row["features"])
        judge: JudgeResult | None = None
        if row["judge_result"]:
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
                heuristic_scores=row["heuristic_scores"],
                judge_result=judge,
                features=features,
                source_path=row["source_path"],
            )
        )
    return ProfileSnapshot.from_scores(scores)
