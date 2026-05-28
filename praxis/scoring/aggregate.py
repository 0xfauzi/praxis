"""Score aggregation.

Wraps the LLM judge's per-session dimension scores into a SessionScore
and rolls session scores up into daily, weekly, and overall profiles.
The judge is the sole source of dimension scores; sessions that can't
be judged (no API keys, judge errors) are not scored at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean

from praxis.models import Session
from praxis.scoring.features import SessionFeatures, extract
from praxis.scoring.judge import (
    JudgeResult,
    score_session,
    score_session_pass1,
    score_session_pass2,
)
from praxis.scoring.rubric import RUBRIC


@dataclass
class SessionScore:
    session_stable_id: str
    provider: str
    started_at: datetime
    dimension_scores: dict[str, float]  # 0-10 per rubric dimension, from the judge
    overall: float                      # weighted /10
    judge_result: JudgeResult
    features: SessionFeatures
    source_path: str
    # Spec §9.6 (US-029): which pass of the two-pass judge produced this
    # score. 1 = cheap-tier pass 1 (always runs), 2 = frontier pass 2
    # (only for sessions pass 1 self-flagged as low confidence). Both rows
    # are persisted when escalation happens so disagreement is auditable.
    judge_pass: int = 1


def _weighted_overall(dimension_scores: dict[str, float]) -> float:
    total = 0.0
    for d in RUBRIC:
        total += dimension_scores.get(d.key, 5.0) * d.weight
    return round(total, 2)


def _session_score_from_judge(
    session: Session, judge: JudgeResult, *, judge_pass: int = 1
) -> SessionScore:
    """Build a SessionScore from a session and its judge output.

    Shared by the frontier-judge path (``score_one_session``) and the
    pass-1 cheap-tier path (``score_one_session_pass1``); both packages
    the same SessionScore shape, only the judge model used to produce
    ``judge`` differs.
    """
    features = extract(session)
    dimension_scores = {d.key: judge.dimension_scores.get(d.key, 5.0) for d in RUBRIC}
    overall = _weighted_overall(dimension_scores)
    return SessionScore(
        session_stable_id=session.stable_id,
        provider=session.provider.value,
        started_at=session.started_at,
        dimension_scores=dimension_scores,
        overall=overall,
        judge_result=judge,
        features=features,
        source_path=session.source_path,
        judge_pass=judge_pass,
    )


def score_one_session(session: Session) -> SessionScore | None:
    """Score one session via the LLM judge.

    Returns None if no judge is available (no API keys configured, or all
    configured judges errored). Callers should treat None as "skip this
    session" rather than substituting a default.
    """
    judge = score_session(session)
    if judge is None:
        return None
    return _session_score_from_judge(session, judge)


def score_one_session_pass1(
    session: Session,
    *,
    sharpen_calibration: bool = False,
    stricter_low: bool = False,
) -> SessionScore | None:
    """Pass 1 of the two-pass judge (spec §9.1): one cheap-tier call per session.

    Pass 1 MUST run on every session in the weekly window. There is no skip
    path; the orchestrator iterates the whole window without consulting any
    heuristic features. Returns None only when no API key is configured (the
    judge layer cannot run at all), never to "skip" a session for cost or
    feature-volume reasons.

    Spec §9.6 (US-031): ``sharpen_calibration`` and ``stricter_low`` are
    forwarded from the orchestrator's 4-week confidence-distribution
    telemetry. The orchestrator computes the rolling shares before the
    pass-1 loop and threads the flags through so each pass-1 call uses
    the same adjusted prompt.
    """
    judge = score_session_pass1(
        session,
        sharpen_calibration=sharpen_calibration,
        stricter_low=stricter_low,
    )
    if judge is None:
        return None
    return _session_score_from_judge(session, judge, judge_pass=1)


def score_one_session_pass2(session: Session) -> SessionScore | None:
    """Pass 2 of the two-pass judge (spec §9.1): one frontier-tier call per session.

    Used only for sessions that pass 1 self-flagged as low confidence. The
    frontier judge gets ONLY the compressed transcript - no pass-1 scores,
    rationale, or moments are passed in, so pass 2 judges fresh. Returns
    None when no API key is configured; the caller should keep the pass-1
    score in that case.
    """
    judge = score_session_pass2(session)
    if judge is None:
        return None
    return _session_score_from_judge(session, judge, judge_pass=2)


@dataclass
class ProfileSnapshot:
    """An aggregate view across many sessions."""

    overall: float
    dimension_means: dict[str, float]
    session_count: int
    provider_breakdown: dict[str, int]
    strongest_dimension: str
    weakest_dimension: str
    standout_moments: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)

    @classmethod
    def from_scores(cls, scores: list[SessionScore]) -> "ProfileSnapshot":
        if not scores:
            return cls(
                overall=0.0,
                dimension_means={d.key: 0.0 for d in RUBRIC},
                session_count=0,
                provider_breakdown={},
                strongest_dimension="",
                weakest_dimension="",
            )

        dim_means: dict[str, float] = {}
        for d in RUBRIC:
            vals = [s.dimension_scores.get(d.key, 5.0) for s in scores]
            dim_means[d.key] = round(mean(vals), 2)

        overall = round(_weighted_overall(dim_means), 2)

        providers: dict[str, int] = {}
        for s in scores:
            providers[s.provider] = providers.get(s.provider, 0) + 1

        strongest = max(dim_means, key=lambda k: dim_means[k])
        weakest = min(dim_means, key=lambda k: dim_means[k])

        # Pull a few standout moments + failure modes from the most recent judged
        # sessions. Per spec 9.5: take their first standout/failure each, dedupe
        # (preserving order), cap at 5.
        recent_judged = [s for s in scores if s.judge_result is not None][-5:]

        def _dedupe(items: list[str], cap: int = 5) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for it in items:
                if it not in seen:
                    seen.add(it)
                    out.append(it)
                if len(out) >= cap:
                    break
            return out

        raw_standouts: list[str] = []
        raw_failures: list[str] = []
        for s in recent_judged:
            if s.judge_result:
                raw_standouts.extend(s.judge_result.standout_moments[:1])
                raw_failures.extend(s.judge_result.failure_modes[:1])

        return cls(
            overall=overall,
            dimension_means=dim_means,
            session_count=len(scores),
            provider_breakdown=providers,
            strongest_dimension=strongest,
            weakest_dimension=weakest,
            standout_moments=_dedupe(raw_standouts),
            failure_modes=_dedupe(raw_failures),
        )
