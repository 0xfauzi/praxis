"""Session feature extraction.

These features are pure, inexpensive metadata about a session - turn
counts, average user prompt length, and how often each dialogue marker
(planning, verification, iteration, pushback) appears. They are inputs
and metadata only: this module does NOT return scores, labels, or
coaching decisions. The LLM judge owns scoring; this module owns the
raw numbers that downstream consumers (the judge, advisors, reports)
can read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from praxis.models import Session

# Patterns chosen for precision over recall - we'd rather miss a planning
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

_MARKER_PATTERNS: dict[str, re.Pattern[str]] = {
    "planning": _PLAN_MARKERS,
    "verification": _VERIFY_MARKERS,
    "iteration": _ITERATION_MARKERS,
    "pushback": _PUSHBACK_MARKERS,
}


def _zero_marker_counts() -> dict[str, int]:
    return {name: 0 for name in _MARKER_PATTERNS}


@dataclass
class SessionFeatures:
    """Pure-data metadata extracted from one session.

    All fields are deterministic measurements. Nothing here is a score,
    label, or coaching decision.
    """

    turn_count: int
    avg_prompt_chars: float
    marker_hit_counts: dict[str, int] = field(default_factory=_zero_marker_counts)


def extract(session: Session) -> SessionFeatures:
    user_turns = session.user_authored_turns

    if not user_turns:
        return SessionFeatures(
            turn_count=session.turn_count,
            avg_prompt_chars=0.0,
            marker_hit_counts=_zero_marker_counts(),
        )

    lengths = [len(t.content) for t in user_turns]
    avg_len = sum(lengths) / len(lengths)

    marker_hit_counts = {
        name: sum(1 for t in user_turns if pattern.search(t.content))
        for name, pattern in _MARKER_PATTERNS.items()
    }

    return SessionFeatures(
        turn_count=session.turn_count,
        avg_prompt_chars=avg_len,
        marker_hit_counts=marker_hit_counts,
    )
