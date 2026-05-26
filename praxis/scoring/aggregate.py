"""Score aggregation.

Combines heuristic and LLM-judge scores for one session, then rolls
session scores up into daily, weekly, and overall profiles.

Weighting choice: when the judge is available, judge weights are 0.7
and heuristics 0.3 — judges catch nuance heuristics can't, but
heuristics catch volume signals judges may miss with a truncated
transcript. When no judge is available, heuristics carry the full
weight.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean

from praxis.models import Session
from praxis.scoring.heuristics import (
    HeuristicFeatures,
    extract,
    heuristic_dimension_scores,
)
from praxis.scoring.judge import JudgeResult, score_session
from praxis.scoring.rubric import RUBRIC


JUDGE_WEIGHT = 0.7
HEURISTIC_WEIGHT = 0.3


@dataclass
class SessionScore:
    session_stable_id: str
    provider: str
    started_at: datetime
    dimension_scores: dict[str, float]  # final blended 0-10
    overall: float                      # weighted /10
    heuristic_scores: dict[str, float]
    judge_result: JudgeResult | None
    features: HeuristicFeatures
    source_path: str


def _blend(
    heuristic: dict[str, float], judge: dict[str, float] | None
) -> dict[str, float]:
    if judge is None:
        return dict(heuristic)
    blended: dict[str, float] = {}
    for d in RUBRIC:
        h = heuristic.get(d.key, 5.0)
        j = judge.get(d.key, h)
        blended[d.key] = HEURISTIC_WEIGHT * h + JUDGE_WEIGHT * j
    return blended


def _weighted_overall(dimension_scores: dict[str, float]) -> float:
    total = 0.0
    for d in RUBRIC:
        total += dimension_scores.get(d.key, 5.0) * d.weight
    return round(total, 2)


def score_one_session(session: Session, use_judge: bool = True) -> SessionScore:
    features = extract(session)
    heuristic = heuristic_dimension_scores(features)
    judge: JudgeResult | None = None
    if use_judge:
        judge = score_session(session)
    blended = _blend(heuristic, judge.dimension_scores if judge else None)
    overall = _weighted_overall(blended)
    return SessionScore(
        session_stable_id=session.stable_id,
        provider=session.provider.value,
        started_at=session.started_at,
        dimension_scores=blended,
        overall=overall,
        heuristic_scores=heuristic,
        judge_result=judge,
        features=features,
        source_path=session.source_path,
    )


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

        strongest = max(dim_means, key=dim_means.get)
        weakest = min(dim_means, key=dim_means.get)

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
