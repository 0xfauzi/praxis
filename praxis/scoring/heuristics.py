"""Heuristic feature extraction.

Cheap signals computed without any API calls. They give us a baseline
score that the LLM judge can refine. They also let us run the scorecard
on machines where API keys aren't configured.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from praxis.models import Role, Session


# Patterns chosen for precision over recall — we'd rather miss a planning
# turn than false-positive on one. Bare "will" and "first.*then" were
# dropped because they fire on common phrases like "this will fail" and
# "first click X then click Y" that have no planning intent.
_PLAN_MARKERS = re.compile(
    r"\b(goals?|objectives?|plans?|approach|steps?|acceptance criteria|"
    r"constraints?|success criteria|done when|"
    r"step 1|my (goal|plan|approach)|i (plan|aim) to|i want to (build|design|create|implement|refactor|plan|ship|migrate))\b",
    re.IGNORECASE,
)

_VERIFY_MARKERS = re.compile(
    r"\b(source|citation|cite|verify|check|reference|where did|how do you know|"
    r"is that correct|are you sure|double-check|fact-check)\b",
    re.IGNORECASE,
)

_ITERATION_MARKERS = re.compile(
    r"\b(no,|actually|instead|rather|try again|different|simpler|"
    r"more specific|too (vague|generic|long|short|complicated)|"
    r"that's not|that's wrong|not quite|closer but|better but)\b",
    re.IGNORECASE,
)

_PUSHBACK_MARKERS = re.compile(
    r"\b(i don'?t (agree|think)|that'?s (wrong|incorrect)|"
    r"are you sure|but what about|disagree)\b",
    re.IGNORECASE,
)


@dataclass
class HeuristicFeatures:
    """Numeric signals extracted from one session."""

    turn_count: int
    user_turn_count: int
    avg_user_prompt_chars: float
    longest_user_prompt_chars: int
    planning_density: float        # share of user turns showing planning markers
    verification_rate: float       # share of user turns showing verification
    iteration_rate: float          # share of user turns showing iteration/correction
    pushback_count: int
    tool_call_count: int
    distinct_tools: int
    has_multi_turn: bool
    avg_context_richness: float    # heuristic 0-1 based on length + code blocks


def extract(session: Session) -> HeuristicFeatures:
    user_turns = session.user_turns

    if not user_turns:
        return HeuristicFeatures(
            turn_count=session.turn_count,
            user_turn_count=0,
            avg_user_prompt_chars=0.0,
            longest_user_prompt_chars=0,
            planning_density=0.0,
            verification_rate=0.0,
            iteration_rate=0.0,
            pushback_count=0,
            tool_call_count=0,
            distinct_tools=0,
            has_multi_turn=False,
            avg_context_richness=0.0,
        )

    lengths = [len(t.content) for t in user_turns]
    avg_len = sum(lengths) / len(lengths)
    longest = max(lengths)

    plan_hits = sum(1 for t in user_turns if _PLAN_MARKERS.search(t.content))
    verify_hits = sum(1 for t in user_turns if _VERIFY_MARKERS.search(t.content))
    iter_hits = sum(1 for t in user_turns if _ITERATION_MARKERS.search(t.content))
    pushback_hits = sum(1 for t in user_turns if _PUSHBACK_MARKERS.search(t.content))

    all_tool_calls = [tc for t in session.turns for tc in t.tool_calls]
    tool_names = {tc.get("name") for tc in all_tool_calls if tc.get("name")}

    # Context richness: combine prompt length with presence of code/data blocks
    code_block_share = sum(
        1 for t in user_turns if "```" in t.content or t.content.count("\n") > 10
    ) / len(user_turns)
    length_score = min(1.0, avg_len / 1500.0)  # 1500 chars ~ "rich enough"
    richness = 0.6 * length_score + 0.4 * code_block_share

    return HeuristicFeatures(
        turn_count=session.turn_count,
        user_turn_count=len(user_turns),
        avg_user_prompt_chars=avg_len,
        longest_user_prompt_chars=longest,
        planning_density=plan_hits / len(user_turns),
        verification_rate=verify_hits / len(user_turns),
        iteration_rate=iter_hits / len(user_turns),
        pushback_count=pushback_hits,
        tool_call_count=len(all_tool_calls),
        distinct_tools=len(tool_names),
        has_multi_turn=len(user_turns) >= 2,
        avg_context_richness=richness,
    )


def heuristic_dimension_scores(features: HeuristicFeatures) -> dict[str, float]:
    """Convert features into 0-10 scores per rubric dimension.

    These are deliberately conservative — they cap at 7/10 so the LLM
    judge has room to award the top tier based on quality, not just
    presence of markers.
    """

    def clip(value: float, ceiling: float = 7.0) -> float:
        return max(0.0, min(ceiling, value))

    planning = clip(features.planning_density * 14.0)  # density of 0.5 -> 7.0
    context = clip(features.avg_context_richness * 10.0)
    # Multi-turn alone isn't iteration; require at least one iteration marker
    # before crediting the multi-turn bonus. Otherwise a 4-turn chat where the
    # user never pushed back gets a free +2.0 — exactly the kind of inflation
    # the spec called out for the heuristic to avoid.
    multi_turn_bonus = 2.0 if (features.has_multi_turn and features.iteration_rate > 0) else 0.0
    iteration = clip(features.iteration_rate * 10.0 + multi_turn_bonus)
    tools = clip(
        min(7.0, features.tool_call_count / 2.0) + min(2.0, features.distinct_tools)
    )
    # Model fit can't be inferred from one session alone; neutral baseline.
    fit = 5.0
    verification = clip(features.verification_rate * 14.0)

    return {
        "planning": planning,
        "context": context,
        "iteration": iteration,
        "tools": tools,
        "fit": fit,
        "verification": verification,
    }
