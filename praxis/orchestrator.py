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
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

from praxis.behavior import (
    BehavioralSignals,
    TrajectoryAssessment,
    assess as assess_trajectory,
    extract as extract_signals,
)
from pathlib import Path

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
