"""Learning trajectory analysis.

Looks at behavioral signals across ALL sessions to determine whether
the user's relationship with AI is one of skill-building or atrophy.

The Shen & Tamkin (2026) paper found that users who fully delegate
gain speed at the cost of comprehension. We can't measure comprehension
directly without testing the user, but we CAN measure whether their
engagement patterns are increasing, stable, or declining.

We also use the LLM judge for a qualitative trajectory read, since
behavior change is exactly the kind of thing humans (and LLMs) judge
better than regex.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from statistics import mean

from praxis.behavior.signals import BehavioralSignals
from praxis.models import Session
from praxis.scoring.features import extract as _extract_features


class TrajectoryLabel(str, Enum):
    LEARNING = "learning"             # engagement rising, delegation falling
    STABLE_ENGAGED = "stable_engaged" # consistently high engagement
    STABLE_PASSIVE = "stable_passive" # consistently low engagement
    ATROPHYING = "atrophying"         # delegation rising, engagement falling
    INSUFFICIENT_DATA = "insufficient_data"
    # v0.2 spec section 7.2 labels produced by the weekly-bucketed model
    # with hysteresis. The legacy labels above stay for the per-session
    # heuristic path; the v0.2 weekly model emits these.
    GROWING_AUTONOMY = "growing_autonomy"
    STEADY = "steady"
    DRIFTING = "drifting"
    READING = "reading"


@dataclass
class TrajectoryAssessment:
    label: TrajectoryLabel
    engagement_slope: float       # change in engagement_rate per session
    delegation_slope: float       # change in delegation_rate per session
    headline: str                 # one sentence summary
    evidence: list[str] = field(default_factory=list)   # specific observations
    risks: list[str] = field(default_factory=list)      # what to watch
    interventions: list[str] = field(default_factory=list)  # what to do
    # v0.3 — the small-multiples chart in the report shows 4 signals,
    # so the trajectory assessment now exposes 4 slopes (defaulted to
    # 0.0 so older fixtures and persisted rows keep loading). The headline
    # itself names whichever of the four are actually moving.
    independence_slope: float = 0.0
    verification_slope: float = 0.0


def _linear_slope(values: list[float]) -> float:
    """Simple least-squares slope. Returns 0.0 for short series."""
    n = len(values)
    if n < 3:
        return 0.0
    xs = list(range(n))
    x_mean = mean(xs)
    y_mean = mean(values)
    num = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    return num / den if den else 0.0


def assess_trajectory_heuristic(
    sessions_with_signals: list[tuple[Session, BehavioralSignals]],
) -> TrajectoryAssessment:
    """Heuristic trajectory read — works with no API."""
    n = len(sessions_with_signals)
    if n < 5:
        return TrajectoryAssessment(
            label=TrajectoryLabel.INSUFFICIENT_DATA,
            engagement_slope=0.0,
            delegation_slope=0.0,
            headline="Need at least 5 sessions to read a trajectory. Keep using AI; we'll have a read for you soon.",
        )

    # Sort by session time, oldest first
    sorted_pairs = sorted(sessions_with_signals, key=lambda p: p[0].started_at)
    engagement_series = [p[1].engagement_rate for p in sorted_pairs]
    delegation_series = [p[1].delegation_rate for p in sorted_pairs]
    independence_series = [p[1].independence_rate for p in sorted_pairs]
    # Verification marker rate is derived from session features (not
    # carried on BehavioralSignals) so we extract per session here.
    verification_series: list[float] = []
    for sess, _sig in sorted_pairs:
        feats = _extract_features(sess)
        turns = max(feats.turn_count, 1)
        verification_series.append(
            feats.marker_hit_counts.get("verification", 0) / turns
        )

    eng_slope = _linear_slope(engagement_series)
    del_slope = _linear_slope(delegation_series)
    ind_slope = _linear_slope(independence_series)
    ver_slope = _linear_slope(verification_series)

    avg_eng = mean(engagement_series)
    mean(delegation_series)
    pure_delegator_count = sum(1 for _s, sig in sorted_pairs if sig.is_pure_delegator)
    pure_delegator_rate = pure_delegator_count / n

    # Decision logic. The label still keys off engagement/delegation;
    # the headline gets a secondary clause that names independence /
    # verification when they're actually moving so the prose covers all
    # 4 signals shown in the small-multiples chart.
    if eng_slope > 0.02 and del_slope < 0.0:
        label = TrajectoryLabel.LEARNING
        headline = (
            f"Engagement is rising and delegation is falling across "
            f"{n} sessions. You're using AI to learn, not to outsource."
        )
    elif del_slope > 0.02 and eng_slope < 0.0:
        label = TrajectoryLabel.ATROPHYING
        headline = (
            f"Delegation is rising and engagement is falling across "
            f"{n} sessions. This is the pattern Shen & Tamkin (2026) "
            f"associated with skill loss."
        )
    elif avg_eng >= 0.3:
        label = TrajectoryLabel.STABLE_ENGAGED
        headline = (
            f"Consistent engagement across {n} sessions. You ask 'why' "
            f"and check your understanding. Skill is forming."
        )
    elif pure_delegator_rate > 0.4:
        label = TrajectoryLabel.STABLE_PASSIVE
        headline = (
            f"{int(pure_delegator_rate*100)}% of your recent sessions "
            f"are pure delegation. Speed today, capability debt tomorrow."
        )
    else:
        label = TrajectoryLabel.STABLE_PASSIVE
        headline = (
            f"Low but steady engagement across {n} sessions. Plenty of "
            f"room to shift from outputs to learning."
        )

    secondary = _secondary_signal_clause(ind_slope, ver_slope)
    if secondary:
        headline = f"{headline} {secondary}"

    evidence: list[str] = []
    if pure_delegator_count > 0:
        evidence.append(
            f"{pure_delegator_count} of {n} sessions show pure delegation "
            f"(>60% atrophy signals, <10% engagement signals)."
        )
    why_total = sum(sig.why_question_count for _s, sig in sorted_pairs)
    if why_total == 0:
        evidence.append(
            "No 'why does this work' or 'explain that' questions in any session. "
            "This is the single strongest atrophy indicator in the literature."
        )
    elif why_total / n > 1.0:
        evidence.append(
            f"You ask {why_total} 'why' questions across {n} sessions "
            f"(avg {why_total/n:.1f}/session). This is the engagement pattern."
        )
    own_total = sum(sig.own_attempt_count for _s, sig in sorted_pairs)
    if own_total / n < 0.1:
        evidence.append(
            "You rarely show your own attempt before asking the AI. "
            "Try the problem for two minutes first; you'll learn more from "
            "the AI's response when you have a hypothesis to compare against."
        )

    risks: list[str] = []
    interventions: list[str] = []
    if label in {TrajectoryLabel.ATROPHYING, TrajectoryLabel.STABLE_PASSIVE}:
        risks.append(
            "Comprehension debt: speed gains compound, but so does the "
            "knowledge gap that emerges when you have to debug AI output "
            "or supervise AI without ground-truth understanding."
        )
        interventions.extend([
            "After accepting any non-trivial AI output, ask: 'explain the "
            "tricky parts of this so I'd be able to write it myself next time.'",
            "When debugging, narrate your hypothesis before asking the AI: "
            "'I think this is failing because X — am I right?'",
            "Pick one library or concept per month and learn it WITHOUT AI for "
            "the first hour. Use AI to verify, not to discover.",
        ])
    elif label == TrajectoryLabel.LEARNING:
        interventions.append(
            "Keep the engagement-then-verify rhythm. Consider teaching "
            "what you've learned — explaining to someone else (or to "
            "the AI, asking it to critique your explanation) compounds "
            "the gains."
        )

    return TrajectoryAssessment(
        label=label,
        engagement_slope=round(eng_slope, 4),
        delegation_slope=round(del_slope, 4),
        headline=headline,
        evidence=evidence,
        risks=risks,
        interventions=interventions,
        independence_slope=round(ind_slope, 4),
        verification_slope=round(ver_slope, 4),
    )


# Threshold for naming a 3rd/4th signal in the headline. Below this slope
# magnitude the secondary signal is treated as flat and skipped to avoid
# narrating noise.
_SECONDARY_SLOPE_THRESHOLD: float = 0.01


def _secondary_signal_clause(
    independence_slope: float,
    verification_slope: float,
) -> str:
    """Name independence/verification when they're materially moving.

    The primary headline already covers engagement + delegation. This
    appends a short clause so the prose accounts for the other two
    signals the small-multiples chart shows. Returns empty string when
    neither signal is moving enough to mention; the primary headline
    stands on its own in that case.
    """
    movers: list[str] = []
    if abs(independence_slope) >= _SECONDARY_SLOPE_THRESHOLD:
        direction = "rising" if independence_slope > 0 else "falling"
        movers.append(f"independence {direction}")
    if abs(verification_slope) >= _SECONDARY_SLOPE_THRESHOLD:
        direction = "up" if verification_slope > 0 else "down"
        movers.append(f"verification {direction}")
    if not movers:
        return ""
    if len(movers) == 1:
        return f"({movers[0]} too)."
    return f"({movers[0]}, {movers[1]})."


def assess_trajectory_with_llm(
    sessions_with_signals: list[tuple[Session, BehavioralSignals]],
) -> TrajectoryAssessment | None:
    """LLM-judged trajectory read. Returns None if no API key available."""
    have_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if not (have_anthropic or have_openai):
        return None

    n = len(sessions_with_signals)
    if n < 5:
        return None

    sorted_pairs = sorted(sessions_with_signals, key=lambda p: p[0].started_at)

    # Per-session verification rate is derived from features (markers/turn).
    # Kept aligned with sorted_pairs so the slope and the per-row timeline
    # share an index.
    verification_per_session: list[float] = []
    for sess, _sig in sorted_pairs:
        feats = _extract_features(sess)
        turns = max(feats.turn_count, 1)
        verification_per_session.append(
            feats.marker_hit_counts.get("verification", 0) / turns
        )

    # Build a compact behavior timeline. independence_rate + verification
    # are included so the LLM can name all 4 signals in its headline.
    timeline = []
    for i, (sess, sig) in enumerate(sorted_pairs[-20:]):
        first_prompt = sess.user_turns[0].content if sess.user_turns else ""
        if len(first_prompt) > 300:
            first_prompt = first_prompt[:300] + "..."
        # Index into the full verification list: the last 20-slice starts
        # at len(sorted_pairs)-20 (clamped).
        full_idx = max(0, len(sorted_pairs) - 20) + i
        timeline.append({
            "date": sess.started_at.strftime("%Y-%m-%d"),
            "provider": sess.provider.value,
            "model": sess.model_hint or "unknown",
            "first_user_prompt": first_prompt,
            "turns": sess.turn_count,
            "engagement_rate": round(sig.engagement_rate, 2),
            "delegation_rate": round(sig.delegation_rate, 2),
            "independence_rate": round(sig.independence_rate, 2),
            "verification_rate": round(verification_per_session[full_idx], 2),
            "why_questions": sig.why_question_count,
            "comprehension_checks": sig.comprehension_check_count,
            "own_attempts": sig.own_attempt_count,
            "is_pure_delegation": sig.is_pure_delegator,
        })

    eng_slope = _linear_slope([p[1].engagement_rate for p in sorted_pairs])
    del_slope = _linear_slope([p[1].delegation_rate for p in sorted_pairs])
    ind_slope = _linear_slope([p[1].independence_rate for p in sorted_pairs])
    ver_slope = _linear_slope(verification_per_session)

    system_prompt = """You are a behavioral analyst studying how people learn (or fail to learn) from AI assistants. You read a timeline of one person's chat sessions over time and judge their learning trajectory.

The person will see your assessment. They need honest, useful feedback — not flattery, and not catastrophizing.

# Research grounding

Your judgments should be informed by what the literature actually finds. The most relevant study:

Shen & Tamkin (2026), "How AI Impacts Skill Formation" (arXiv 2601.20245). Randomized controlled trial with 52 developers learning a new Python library (Trio). Findings:

  - AI-assisted developers scored 17 percentage points lower on a comprehension quiz than unaided controls.
  - The largest gap was in debugging — the exact skill needed to supervise AI output.
  - About 20% of users were "pure delegators" — fastest, worst learning outcomes.
  - The ~80% who showed "high skill development" patterns stayed cognitively engaged: they asked WHY, requested explanations, and combined generation with comprehension.
  - Agentic coding products (Claude Code, Cursor, etc.) likely amplify these effects compared to plain Q&A assistants.

Anthropic's 2026 qualitative study of 81,000 users found 16.3% of respondents — and 24% of teachers — explicitly worried about cognitive atrophy from AI use. 46% of those worried had observed it firsthand in themselves or others.

# What you're looking for

Pick exactly ONE label:

  - "learning"           — engagement signals rising over time, delegation signals falling. Evidence of skill formation.
  - "stable_engaged"     — consistently high engagement throughout. Asks why, checks understanding, shows own attempts. Skill is forming.
  - "stable_passive"     — productive but disengaged. Uses AI to get things done without learning much. Speed today, capability gap tomorrow.
  - "atrophying"         — engagement falling, delegation rising. The at-risk pattern in the literature.
  - "insufficient_data"  — fewer than 5 sessions, or sessions too uniform to read a trajectory.

# Decision principles

- Slopes matter more than averages. A user moving from delegation to engagement is on a better trajectory than one stuck at moderate engagement.
- Look at the FIRST PROMPT of each session. Telegraphic first prompts ("write me X") that don't develop into deeper engagement later in the session are a strong delegation signal.
- "Why does this work?" / "explain X to me" questions are the single strongest learning indicator in the literature. Their absence is diagnostic.
- "I tried X" / "my approach was Y" before asking is a strong independence signal.
- Don't conflate productivity with learning. Someone can be very productive with AI and still atrophy.

# Edge cases

- If the person's use case doesn't involve learning new things (e.g. mostly using AI for repetitive translation or formatting), "stable_passive" may be the appropriate label without being alarming. Note this in the headline.
- If you see a sudden trajectory shift (e.g. clear shift from engaged to passive in the last week), call that out specifically.
- Be honest about "atrophying" when you see it. Don't soften the call. The user benefits from a clear read.

# Voice

Direct, warm, practical. You're a colleague giving honest observations, not a tool barking metrics. One sentence of specificity beats a paragraph of hedging. No corporate jargon. Active voice.

# Output

Return ONLY valid JSON, no preamble:

{
  "label": "<one of the five above>",
  "headline": "<2 sentences. Direct. Names the trajectory and cites the strongest evidence for it.>",
  "evidence": [
    "<specific observation from the timeline data>",
    "..."
  ],
  "risks": [
    "<what's at stake if this pattern continues>",
    "..."
  ],
  "interventions": [
    "<concrete, do-it-tomorrow change>",
    "..."
  ]
}

evidence: 1-4 items. risks: 0-3 items (empty for learning/engaged labels). interventions: 1-4 items."""

    user_msg = (
        f"Engagement slope: {eng_slope:+.4f} per session\n"
        f"Delegation slope: {del_slope:+.4f} per session\n"
        f"Independence slope: {ind_slope:+.4f} per session\n"
        f"Verification-marker slope: {ver_slope:+.4f} per session\n"
        f"Total sessions: {n}\n\n"
        f"Headline guidance: the digest renders a small-multiples chart "
        f"with all four signals (engagement, delegation, independence, "
        f"verification). Your 2-sentence headline should account for the "
        f"signals that are actually moving; do not narrate one or two and "
        f"silently drop the others when they are moving as well.\n\n"
        f"Behavior timeline (oldest first, last 20 sessions):\n"
        f"{json.dumps(timeline, indent=2)}"
    )

    text: str = ""
    try:
        if have_anthropic:
            from anthropic import Anthropic  # type: ignore
            client = Anthropic()
            resp = client.messages.create(
                model="claude-opus-4-7",
                max_tokens=1500,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        else:
            from openai import OpenAI  # type: ignore
            client = OpenAI()  # type: ignore[assignment]
            resp = client.chat.completions.create(  # type: ignore[attr-defined]
                model="gpt-5",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        print(f"[behavior] LLM trajectory call failed: {exc!r}", file=sys.stderr)
        return None

    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[-1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
            cleaned = cleaned.rsplit("```", 1)[0]
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        payload = json.loads(cleaned[start : end + 1])
        try:
            label = TrajectoryLabel(payload["label"])
        except (KeyError, ValueError):
            label = TrajectoryLabel.STABLE_PASSIVE
        return TrajectoryAssessment(
            label=label,
            engagement_slope=round(eng_slope, 4),
            delegation_slope=round(del_slope, 4),
            headline=payload.get("headline", ""),
            evidence=list(payload.get("evidence", [])),
            risks=list(payload.get("risks", [])),
            interventions=list(payload.get("interventions", [])),
            independence_slope=round(ind_slope, 4),
            verification_slope=round(ver_slope, 4),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[behavior] failed to parse LLM trajectory: {exc!r}", file=sys.stderr)
        return None


def assess(
    sessions_with_signals: list[tuple[Session, BehavioralSignals]],
    prefer_llm: bool = True,
) -> TrajectoryAssessment:
    """Top-level assessor. LLM if available, heuristic otherwise."""
    if prefer_llm:
        llm_result = assess_trajectory_with_llm(sessions_with_signals)
        if llm_result is not None:
            return llm_result
    return assess_trajectory_heuristic(sessions_with_signals)
