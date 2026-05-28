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


# --------------------------------------------------------------------
# US-041 signals: specification adoption, context engineering depth,
# knowledge-gap distribution.
#
# These are all session-opening (spec block) or session-wide (scaffolding
# artifacts, knowledge gaps) detectors. The detectors are conservative
# regexes: precision-first, so a session that does not match is reported
# as "no signal" rather than risking a false positive that would mask a
# coachable moment.


# Spec-block markers per spec section 11 ("Specification artifact"). The
# first user turn is the open of the session; we look for structured
# spec headings (Markdown ## or labelled inline forms) so a freeform
# prompt that happens to contain the word "goal" does not falsely fire.
# Anchoring on `## ` (or a label-colon at line start) keeps the bar at
# "user wrote a structured spec block" rather than "user mentioned a
# spec word in passing".
_SPEC_BLOCK_HEADING_MARKERS = re.compile(
    r"(?im)^\s*#{2,}\s*"
    r"(goal|constraints|approach|acceptance criteria|done when|"
    r"success criteria|requirements|specification|context|out of scope)"
    r"\b",
)


_SPEC_BLOCK_INLINE_MARKERS = re.compile(
    r"(?im)^\s*"
    r"(goal|constraints|approach|acceptance criteria|done when|"
    r"success criteria|requirements|out of scope)"
    r"\s*:",
)


def detect_spec_block(session: Session) -> bool:
    """Return True when this session opens with a structured spec block.

    Per spec section 11 ("Specification artifact"), the signal fires when
    the FIRST user turn carries a Markdown spec heading (e.g. ``## Goal``
    / ``## Constraints``) or a label-colon form (e.g. ``Goal:`` /
    ``Done when:``) at line start. Sessions with no user turns return
    False - there is no session-opening turn to evaluate.

    The detector looks only at the first user turn rather than the
    entire session: a spec block written mid-session is interesting in
    a different way (course correction, not adoption), and the
    specification-adoption panel measures session OPENINGS specifically.
    """
    user_turns = session.user_turns
    if not user_turns:
        return False
    content = user_turns[0].content
    if not content:
        return False
    if _SPEC_BLOCK_HEADING_MARKERS.search(content):
        return True
    if _SPEC_BLOCK_INLINE_MARKERS.search(content):
        return True
    return False


# Context-engineering scaffolding artifacts per spec section 11. Each
# kind matches a single AI-coding-tool convention; the detector emits
# the set of kinds whose markers appear ANYWHERE in the session (any
# user turn). The five kinds are tracked separately so the panel can
# show which scaffolding surfaces the user actually engages with.
SCAFFOLDING_KINDS_IN_PANEL_ORDER: tuple[str, ...] = (
    "claude_md",
    "agents_md",
    "copilot_instructions",
    "projects",
    "skills",
)


_SCAFFOLDING_PATTERNS: dict[str, re.Pattern[str]] = {
    "claude_md": re.compile(r"\bCLAUDE\.md\b", re.IGNORECASE),
    "agents_md": re.compile(r"\bAGENTS\.md\b", re.IGNORECASE),
    "copilot_instructions": re.compile(
        r"\bcopilot[-_]?instructions(?:\.md)?\b", re.IGNORECASE
    ),
    # ChatGPT "Projects" feature (OpenAI 2024) - the leading uppercase
    # marker keeps the pattern from firing on generic uses of "project"
    # (e.g. "this project's auth module"). The user explicitly names
    # the surface or describes it as a Custom GPT.
    "projects": re.compile(
        r"\b(ChatGPT Projects?|Custom GPT|custom instructions for ChatGPT)\b"
    ),
    # Anthropic Skills + the broader "skill file" idiom. The .skill
    # extension is the canonical artifact; the "subagent" / "agent
    # skill" / "Skills/<name>" forms cover the spelled-out variants.
    "skills": re.compile(
        r"(?:\.skill\b|"
        r"\b(?:agent skill|skills?/[A-Za-z0-9_\-]+|"
        r"sub[- ]?agents?|hooks?/[A-Za-z0-9_\-]+))",
        re.IGNORECASE,
    ),
}


def detect_scaffolding_kinds(session: Session) -> set[str]:
    """Return the scaffolding artifact kinds present in this session.

    Walks every user turn and accumulates the set of kinds whose pattern
    fires. The return is a set rather than a list because a kind that
    fires twice in a session is no more meaningful than a kind that
    fires once - the panel measures presence, not frequency.
    Kinds returned are members of ``SCAFFOLDING_KINDS_IN_PANEL_ORDER``.
    """
    seen: set[str] = set()
    for turn in session.user_turns:
        content = turn.content
        for kind, pattern in _SCAFFOLDING_PATTERNS.items():
            if kind in seen:
                continue
            if pattern.search(content):
                seen.add(kind)
        if len(seen) == len(_SCAFFOLDING_PATTERNS):
            break
    return seen


# Knowledge-gap kinds per arXiv 2501.11709 ("Towards Detecting Prompt
# Knowledge Gaps for Improved LLM-guided Issue Resolution"). The four
# categories below match the spec section 11 cut: each is a per-USER-TURN
# count, and the panel shows the four-category distribution across the
# week. Patterns are precision-first so a clear prompt is never
# misclassified as a knowledge gap.
KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER: tuple[str, ...] = (
    "missing_context",
    "missing_specs",
    "multiple_context",
    "unclear_instructions",
)


KNOWLEDGE_GAP_LABELS: dict[str, str] = {
    "missing_context": "Missing context",
    "missing_specs": "Missing specifications",
    "multiple_context": "Multiple contexts",
    "unclear_instructions": "Unclear instructions",
}


# "Missing context" cue: prompt asks about a specific identifier
# (function / class / file / variable) WITHOUT providing the code
# block. We approximate by looking for "this function" / "this class" /
# "the function I wrote" patterns in turns that carry no code fence.
_MISSING_CONTEXT_REFERENCES = re.compile(
    r"\b(this (function|class|method|module|file|code|script|test|loop)|"
    r"my (function|class|method|module|file|code|script)|"
    r"the (function|class|method|module|file|code) (i|we) (wrote|made|"
    r"have|am working on))\b",
    re.IGNORECASE,
)


# Acceptance-criteria / done-when markers. When PRESENT, the turn does
# NOT fire missing_specs (the user spelled out the criteria). When
# ABSENT, the imperative-only prompt fires missing_specs.
_SPECS_PRESENT_MARKERS = re.compile(
    r"(?im)\b(acceptance criteria|done when|success criteria|"
    r"should (return|produce|handle|raise|emit|accept)|"
    r"must (return|produce|handle|raise|emit|accept)|"
    r"expected (output|result|behavior)|"
    r"the goal is|when (it|this) is done)\b",
)


# Imperative opener that indicates a build-something prompt. Used as a
# precondition for "missing_specs": only build-something prompts that
# lack spec markers are gaps. A question-form prompt without spec
# markers (e.g. "why does X happen?") is not a missing-specs gap.
_BUILD_IMPERATIVE = re.compile(
    r"^\s*(write|make|create|build|generate|implement|add|"
    r"refactor|fix|update|change|extend|design)\b",
    re.IGNORECASE,
)


# "Multiple context" cue: the turn lists multiple unrelated TODO items.
# Conservative heuristic: 2+ "and also" / numbered-list / "plus" /
# semicolon-separated request structures push the turn into the
# multi-context bucket.
_MULTIPLE_CONTEXT_MARKERS = re.compile(
    r"(\band also\b|\balso (write|make|create|build|add|fix)|"
    r"^\s*\d+[.)]\s+.+\n\s*\d+[.)]\s+|"
    r";\s*(then|also|and)\s+(write|make|create|build|add|fix)|"
    r"\bplus\b.*(\bwrite|\bmake|\bcreate|\bbuild|\badd|\bfix))",
    re.IGNORECASE | re.MULTILINE,
)


# "Unclear instructions" cue: vague verb-object phrasing where the
# object is a pronoun without antecedent or "something". Conservative:
# requires a "do something with"/"handle this somehow"/etc. shape so a
# normal short prompt does not falsely fire.
_UNCLEAR_INSTRUCTION_MARKERS = re.compile(
    r"\b("
    r"do something (with|to|about)|"
    r"handle (this|it|that) (somehow|properly|right|correctly)|"
    r"make (this|it|that) (work|better|good|right)|"
    r"figure (this|it|that) out|"
    r"clean (this|it|that) up|"
    r"deal with (this|it|that)|"
    r"sort (this|it|that) out|"
    r"can you (just|maybe) (help|look)"
    r")\b",
    re.IGNORECASE,
)


def detect_knowledge_gap_kinds(turn: Turn) -> set[str]:
    """Return the knowledge-gap kinds that fire for this user turn.

    Implements the arXiv 2501.11709 four-category cut: missing_context,
    missing_specs, multiple_context, unclear_instructions. A single turn
    can match multiple kinds (e.g. a vague imperative that also lists
    several unrelated tasks). Kinds returned are members of
    ``KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER``.

    The detector is precision-first: a clearly-scoped prompt with a code
    block and acceptance criteria returns an empty set even if it
    contains pronouns or imperatives in passing.
    """
    kinds: set[str] = set()
    content = turn.content
    if not content:
        return kinds

    # Missing context: references a specific identifier without including
    # the code. We use a fenced-code-block heuristic: a turn that
    # carries a fenced block (```) or an indented code block (4+ spaces)
    # is treated as having the code present.
    references_identifier = bool(_MISSING_CONTEXT_REFERENCES.search(content))
    has_code_block = "```" in content or "    " in content
    if references_identifier and not has_code_block:
        kinds.add("missing_context")

    # Missing specs: build-something imperative with no acceptance
    # criteria / done-when / expected-output markers.
    if _BUILD_IMPERATIVE.search(content) and not _SPECS_PRESENT_MARKERS.search(
        content
    ):
        kinds.add("missing_specs")

    if _MULTIPLE_CONTEXT_MARKERS.search(content):
        kinds.add("multiple_context")

    if _UNCLEAR_INSTRUCTION_MARKERS.search(content):
        kinds.add("unclear_instructions")

    return kinds


def count_session_knowledge_gaps(session: Session) -> dict[str, int]:
    """Return the per-kind knowledge-gap counts for this session.

    Walks every user turn in the session and accumulates one count per
    kind. The four keys in the returned dict are exactly
    ``KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER``, with each entry initialised
    to zero so a session with no detected gaps still contributes an
    explicit zero to each category (US-041 acceptance: no silent drops).
    """
    counts: dict[str, int] = {kind: 0 for kind in KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER}
    for turn in session.user_turns:
        for kind in detect_knowledge_gap_kinds(turn):
            counts[kind] += 1
    return counts


# --------------------------------------------------------------------
# US-042 tool/agent ladder signal.
#
# The ladder anchors against Anthropic's Skills/hooks/subagents harness
# stack and OpenAI's harness-engineering writeups: as users move up the
# ladder from prompt-only -> tools-on -> skills -> hooks -> subagents
# they exercise progressively richer scaffolding. The panel surfaces the
# MAX rung observed across the week's sessions so the reader sees the
# ceiling of their current habit. Each session is bucketed by the
# highest-rung kind any of its turns reached.
#
# Rung-order is intentional and lowest-to-highest. ``categorize_session_
# ladder_rung`` walks user-turn content for the scaffolding markers
# (skills / hooks / subagents) and assistant-turn ``tool_calls`` for the
# tools-on signal, then returns the highest rung any turn reached. A
# session whose only signal is the user typing prompts (no tool calls,
# no scaffolding mention) lands in ``prompt_only``.

LADDER_KINDS_IN_PANEL_ORDER: tuple[str, ...] = (
    "prompt_only",
    "tools_on",
    "skills",
    "hooks",
    "subagents",
)


LADDER_LABELS: dict[str, str] = {
    "prompt_only": "Prompt-only",
    "tools_on": "Tools-on",
    "skills": "Skills",
    "hooks": "Hooks",
    "subagents": "Subagents",
}


# Per-rung markers. Each pattern is anchored on text that names the
# concept by its canonical artifact form: ``.skill`` files / ``skills/<name>``
# paths for Skills, ``hooks/<name>`` paths or ``.hook`` for hooks,
# ``subagent`` / ``sub-agent`` spellings for subagents. The patterns are
# distinct from ``_SCAFFOLDING_PATTERNS["skills"]`` (which conflates the
# three rungs into one bucket) because the ladder panel needs each rung
# separate to compute the user's ceiling.
_LADDER_SKILLS_MARKERS = re.compile(
    r"(?:\.skill\b|"
    r"\bagent skill\b|"
    r"\bskills?/[A-Za-z0-9_\-]+|"
    r"\banthropic skills?\b)",
    re.IGNORECASE,
)


_LADDER_HOOKS_MARKERS = re.compile(
    r"(?:\bhooks?/[A-Za-z0-9_\-]+|"
    r"\.hook\b|"
    r"\bpre[- ]?(?:tool[- ]?use|commit)\s+hook|"
    r"\bpost[- ]?(?:tool[- ]?use|commit)\s+hook)",
    re.IGNORECASE,
)


_LADDER_SUBAGENTS_MARKERS = re.compile(
    r"\b(sub[- ]?agents?|subagent[- ]?type|spawn(?:ed)?\s+a?\s*subagent)\b",
    re.IGNORECASE,
)


def _turn_has_tool_calls(turn: Turn) -> bool:
    """True when an assistant turn carries one or more tool_calls.

    Defensive: a turn whose ``tool_calls`` list is None or missing is
    treated as no calls. Tool calls only ever attach to assistant turns
    in the normalized model, so a user turn with tool_calls would be a
    bug upstream; the function does not filter on role to keep the
    detector cheap.
    """
    calls = getattr(turn, "tool_calls", None) or []
    return bool(calls)


def detect_session_ladder_rungs(session: Session) -> set[str]:
    """Return the set of ladder-rung kinds observed in this session.

    Walks every turn:
      - Any assistant turn with ``tool_calls`` contributes ``tools_on``.
      - Any user-turn content matching skills / hooks / subagents
        markers contributes that respective kind.

    ``prompt_only`` is the implicit default (returned by
    ``categorize_session_ladder_rung`` when no other kind fires) and is
    never returned here, mirroring the verification-calibration
    detector's shape: an absence-marker cannot be observed per-turn.
    """
    kinds: set[str] = set()
    for turn in session.turns:
        if _turn_has_tool_calls(turn):
            kinds.add("tools_on")
        content = getattr(turn, "content", "") or ""
        if not content:
            continue
        if _LADDER_SKILLS_MARKERS.search(content):
            kinds.add("skills")
        if _LADDER_HOOKS_MARKERS.search(content):
            kinds.add("hooks")
        if _LADDER_SUBAGENTS_MARKERS.search(content):
            kinds.add("subagents")
    return kinds


def categorize_session_ladder_rung(session: Session) -> str:
    """Return the highest-rung kind reached by this session.

    Rung order (lowest to highest): prompt_only < tools_on < skills <
    hooks < subagents. ``categorize_session_ladder_rung`` walks the
    session once via ``detect_session_ladder_rungs`` and promotes the
    session to the highest kind any turn reached. A session with no
    detected signal lands in ``prompt_only`` (the user typed prompts;
    the assistant produced text; no tools, no scaffolding).

    Returns a member of ``LADDER_KINDS_IN_PANEL_ORDER``.
    """
    rungs = detect_session_ladder_rungs(session)
    # Walk panel order from highest to lowest so the first match wins.
    for kind in reversed(LADDER_KINDS_IN_PANEL_ORDER):
        if kind == "prompt_only":
            continue
        if kind in rungs:
            return kind
    return "prompt_only"
