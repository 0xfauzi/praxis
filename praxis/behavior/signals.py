"""Behavioral signal extraction.

Looks for text patterns in user turns that the Shen & Tamkin (2026)
"How AI Impacts Skill Formation" paper identified as separating
skill-developing users from atrophying users.

Their RCT (n=52 developers learning Trio) found three "high skill
development" patterns where users stayed cognitively engaged:
  1. Generation-then-comprehension — ask AI to generate, then ask 'why'
  2. Asking for explanations alongside code/answers
  3. Verifying understanding by testing or rephrasing

Atrophy patterns:
  1. Pure delegation — accept output without follow-up
  2. Outsourcing debugging — "fix this" without engaging with the error
  3. Telegraphic single-turn requests — "write me X" with no engagement

These are detectable from text alone. The Shen & Tamkin paper found
~80% of participants used patterns OTHER than pure delegation; the
20% who delegated were faster but had the worst learning outcomes.
We capture this split.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from praxis.models import Session, Turn


# Stable signal-kind keys used by the behavioral-patterns panel
# (`praxis/reports/panel_inputs.py`). Order doubles as the panel's row
# order: engagement signals first, then atrophy, then independence.
SIGNAL_KINDS_IN_PANEL_ORDER: tuple[str, ...] = (
    "why_question",
    "comprehension_check",
    "explanation_request",
    "pure_delegation",
    "outsourced_debug",
    "telegraphic",
    "own_attempt",
)


# Engagement signals — user is staying cognitively in the loop
_WHY_QUESTIONS = re.compile(
    r"\b(why (does|is|did|are|do)|how does (this|it|that) work|"
    r"what does (this|it|that) (do|mean)|explain (this|that|how|why)|"
    r"walk me through|help me understand|i don'?t understand|"
    r"what'?s the difference|what'?s happening|where does .* come from)\b",
    re.IGNORECASE,
)

_COMPREHENSION_CHECKS = re.compile(
    r"\b(so (if|when|that means)|let me (make sure|check|verify)|"
    r"if i understand|am i right that|so basically|"
    r"would (it|that|this) (also|still) work if|what about (when|if))\b",
    re.IGNORECASE,
)

_EXPLANATION_REQUESTS = re.compile(
    r"\b(explain|teach me|tutorial|how (to|do|does)|what is|"
    r"can you (walk|talk|show) me through|"
    r"break (this|it|that) down|step by step)\b",
    re.IGNORECASE,
)

# Atrophy signals — user is delegating without engagement
_PURE_DELEGATION = re.compile(
    r"^(write|make|create|build|generate|give me|do|fix|implement)\s",
    re.IGNORECASE,
)

_OUTSOURCED_DEBUG = re.compile(
    r"\b(fix (this|it|the)|just (make|get) it work|"
    r"why (is this|is it) (broken|not working|wrong)|"
    r"what'?s wrong with this)\b",
    re.IGNORECASE,
)

# Independence signals — did the user try first?
_OWN_ATTEMPT_MARKERS = re.compile(
    r"\b(i tried|i was thinking|my approach|i wrote|"
    r"my (idea|attempt|guess)|here'?s what i have|"
    r"i think (this|it) should)\b",
    re.IGNORECASE,
)

# Telegraphic prompts — very short, no context, no engagement
def _is_telegraphic(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) < 80 and "\n" not in stripped and "?" not in stripped[:-1]


@dataclass
class BehavioralSignals:
    """Per-session behavioral features. All counts are over USER turns."""

    user_turn_count: int

    # Engagement
    why_question_count: int
    comprehension_check_count: int
    explanation_request_count: int

    # Atrophy
    pure_delegation_count: int
    outsourced_debug_count: int
    telegraphic_count: int

    # Independence
    own_attempt_count: int

    # Derived ratios (0-1)
    engagement_rate: float       # share of user turns with engagement signals
    delegation_rate: float       # share with atrophy signals
    independence_rate: float     # share showing own attempt before asking

    # Single most diagnostic signal — Shen & Tamkin's key finding
    is_pure_delegator: bool      # True if delegation_rate > 0.6 and engagement_rate < 0.1


def extract(session: Session) -> BehavioralSignals:
    user_turns = session.user_turns
    if not user_turns:
        return BehavioralSignals(
            user_turn_count=0,
            why_question_count=0,
            comprehension_check_count=0,
            explanation_request_count=0,
            pure_delegation_count=0,
            outsourced_debug_count=0,
            telegraphic_count=0,
            own_attempt_count=0,
            engagement_rate=0.0,
            delegation_rate=0.0,
            independence_rate=0.0,
            is_pure_delegator=False,
        )

    n = len(user_turns)
    why_hits = sum(1 for t in user_turns if _WHY_QUESTIONS.search(t.content))
    comp_hits = sum(1 for t in user_turns if _COMPREHENSION_CHECKS.search(t.content))
    expl_hits = sum(1 for t in user_turns if _EXPLANATION_REQUESTS.search(t.content))
    del_hits = sum(1 for t in user_turns if _PURE_DELEGATION.match(t.content.strip()))
    out_hits = sum(1 for t in user_turns if _OUTSOURCED_DEBUG.search(t.content))
    tel_hits = sum(1 for t in user_turns if _is_telegraphic(t.content))
    own_hits = sum(1 for t in user_turns if _OWN_ATTEMPT_MARKERS.search(t.content))

    engagement_signals = why_hits + comp_hits + expl_hits
    atrophy_signals = del_hits + out_hits + tel_hits

    engagement_rate = min(1.0, engagement_signals / n)
    delegation_rate = min(1.0, atrophy_signals / n)
    independence_rate = own_hits / n

    return BehavioralSignals(
        user_turn_count=n,
        why_question_count=why_hits,
        comprehension_check_count=comp_hits,
        explanation_request_count=expl_hits,
        pure_delegation_count=del_hits,
        outsourced_debug_count=out_hits,
        telegraphic_count=tel_hits,
        own_attempt_count=own_hits,
        engagement_rate=engagement_rate,
        delegation_rate=delegation_rate,
        independence_rate=independence_rate,
        is_pure_delegator=(delegation_rate > 0.6 and engagement_rate < 0.1),
    )


def detect_signal_kinds(turn: Turn) -> set[str]:
    """Return the set of signal kinds that fire for this user turn.

    The keys returned are members of ``SIGNAL_KINDS_IN_PANEL_ORDER``.
    The behavioral-patterns panel (US-038) uses this to attach example
    excerpts to each signal kind for the user-facing footnote: counts
    come from ``extract``, but the panel also wants to surface up to two
    raw user-turn excerpts per signal so the reader can see what
    triggered the count.

    A single turn can match multiple kinds (e.g. an explanation request
    that is also a pure delegation), so the return type is a set.
    """
    kinds: set[str] = set()
    content = turn.content
    if _WHY_QUESTIONS.search(content):
        kinds.add("why_question")
    if _COMPREHENSION_CHECKS.search(content):
        kinds.add("comprehension_check")
    if _EXPLANATION_REQUESTS.search(content):
        kinds.add("explanation_request")
    if _PURE_DELEGATION.match(content.strip()):
        kinds.add("pure_delegation")
    if _OUTSOURCED_DEBUG.search(content):
        kinds.add("outsourced_debug")
    if _is_telegraphic(content):
        kinds.add("telegraphic")
    if _OWN_ATTEMPT_MARKERS.search(content):
        kinds.add("own_attempt")
    return kinds


# --------------------------------------------------------------------
# US-040 verification-calibration signals.
#
# Each pattern fires when a user turn shows the corresponding verification
# activity. The four kinds form a rigor hierarchy that the
# verification-calibration panel uses to categorize sessions:
#   1. source_check  - user verifies the origin/citation/reference of
#      a claim (highest rigor: checks upstream truth, not just behavior).
#   2. test_run      - user executes or asks for tests / pytest / npm
#      test (medium-high rigor: empirical verification of behavior).
#   3. spot_check    - user inspects output lightly ("looks right",
#      "double-check") without sourcing or testing (low rigor).
#   4. blanket_accept- no verification observed in the session (none).
#
# Patterns are intentionally conservative (precision > recall): we'd
# rather under-count rigorous verification than misclassify a delegating
# session as rigorous. The adapter walks user turns and promotes a
# session to the highest-rigor bucket any of its turns reached.

_SOURCE_CHECK_MARKERS = re.compile(
    r"\b("
    r"source|sources|cite|citation|citations|reference|references|"
    r"where (does|did) (this|it|that|they) (come|originate)|"
    r"where'?s (this|that|it) from|"
    r"what'?s the source|provide a source"
    r")\b",
    re.IGNORECASE,
)


_TEST_RUN_MARKERS = re.compile(
    r"\b("
    r"run (the )?tests?|"
    r"pytest|jest|npm test|cargo test|go test|"
    r"unit tests?|"
    r"did the tests? pass|do the tests? pass|"
    r"the tests? (passed|fail|failed)|"
    r"run it|let me run|i'?ll run"
    r")\b",
    re.IGNORECASE,
)


_SPOT_CHECK_MARKERS = re.compile(
    r"\b("
    r"double[- ]?check|spot[- ]?check|"
    r"let me check|let me look|"
    r"looks (right|good|fine|correct|ok|okay)|"
    r"that looks (right|good|fine|correct|ok|okay)|"
    r"glance|skim|eyeball|sanity check"
    r")\b",
    re.IGNORECASE,
)


# Kinds in highest-to-lowest rigor order; the adapter picks the FIRST
# kind that fires for any user turn in a session, so promotion to a
# higher-rigor bucket beats a lower-rigor match later in the same
# session. ``blanket_accept`` is the implicit default when no other
# pattern fires.
_VERIFICATION_CALIBRATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("source_check", _SOURCE_CHECK_MARKERS),
    ("test_run", _TEST_RUN_MARKERS),
    ("spot_check", _SPOT_CHECK_MARKERS),
)


def detect_verification_calibration_kinds(turn: Turn) -> set[str]:
    """Return the set of verification-calibration kinds for one turn.

    Used by the verification-calibration panel (US-040). A single turn
    can match multiple kinds (e.g. "let me run the tests and check the
    source") so the return value is a set; the panel adapter promotes
    the session to the highest-rigor kind any of its turns reached.
    Kinds returned are drawn from
    ``("source_check", "test_run", "spot_check")``; ``blanket_accept``
    is never returned here because it is the default when no other
    kind fires (a per-turn detector cannot observe absence of activity
    across a session).
    """
    kinds: set[str] = set()
    for kind, pattern in _VERIFICATION_CALIBRATION_PATTERNS:
        if pattern.search(turn.content):
            kinds.add(kind)
    return kinds


def categorize_session_verification(session: Session) -> str:
    """Return the highest-rigor verification kind for one session.

    Walks the session's user turns once and returns the highest-rigor
    kind any turn reached: source_check > test_run > spot_check >
    blanket_accept. Sessions that produced no verification signal at
    all (and sessions with zero user turns) land in ``blanket_accept``.

    This is the rigor "ceiling" of the session, not a turn-level count:
    a session that briefly source-checked once is classified as
    source-check regardless of how many turns afterward simply accepted
    output. The panel reads as a histogram of how the user verified at
    their MOST rigorous moment of each session, which matches the
    automation-bias literature's framing of verification ceilings as
    the trust-calibration anchor.
    """
    seen: set[str] = set()
    for turn in session.user_turns:
        seen.update(detect_verification_calibration_kinds(turn))
        if "source_check" in seen:
            # Highest rigor reached; early exit is safe.
            return "source_check"
    if "source_check" in seen:
        return "source_check"
    if "test_run" in seen:
        return "test_run"
    if "spot_check" in seen:
        return "spot_check"
    return "blanket_accept"
