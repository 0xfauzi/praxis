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
from dataclasses import dataclass, field

from praxis.models import Session


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


# --- Expansion signals (US-005) -------------------------------------------------
# All three patterns below come from primary sources we already build on:
#   - Anthropic's Claude Code best-practices guidance: "be specific", "give the
#     model context", "iterate" (cited in README and Section 14 of the
#     behavioral-signals reference doc; same source as the engagement patterns
#     in this module).
#   - Shen & Tamkin (2026), "How AI Impacts Skill Formation" (arXiv 2601.20245).
#     Their RCT showed that specification before delegation, naming the error
#     instead of "fix this", and iterative refinement separate skill-developing
#     users from atrophying users (cited throughout praxis/behavior/).
#   - OpenAI Codex/Responses docs on structured instructions and tool-loop
#     refinement (the "give the model a goal, then refine" pattern).

# Specification artifact — the user supplies a written spec: goal, constraints,
# acceptance criteria, inputs/outputs, or a Given/When/Then scenario. This is
# the cheapest, highest-leverage move per Anthropic's guidance and is the
# spec-driven-development practice called out in our own CLAUDE.md.
_SPECIFICATION_ARTIFACT = re.compile(
    r"(?im)"
    r"(^\s*(##+\s*)?(goal|constraints?|acceptance(\s+criteria)?|"
    r"requirements?|inputs?|outputs?|out\s+of\s+scope|non[\-\s]?goals?|"
    r"context|background)\s*[:\-])"
    r"|"
    r"\b(spec|prd|user\s+stor(y|ies))\s*[:\-]"
    r"|"
    r"\b(given|when|then)\b.*\b(when|then|and)\b"
    r"|"
    r"\b(must|should|shall)\s+(have|be|not|return|exit|raise|emit|support|"
    r"handle|accept|reject|fail)\b",
)

# Error naming — the user identifies a *specific* error class, traceback, or
# error message instead of telegraphic "fix this". Shen & Tamkin's debugging
# finding (17pp comprehension gap, biggest in debugging) maps directly here:
# naming the error is the diagnostic step the atrophying group skips.
_ERROR_NAMING = re.compile(
    r"\b("
    # Python builtins
    r"TypeError|ValueError|AttributeError|KeyError|NameError|IndexError|"
    r"RuntimeError|ImportError|ModuleNotFoundError|ZeroDivisionError|"
    r"FileNotFoundError|IOError|OSError|AssertionError|NotImplementedError|"
    r"StopIteration|RecursionError|UnicodeDecodeError|UnicodeEncodeError|"
    # JS / TS
    r"ReferenceError|SyntaxError|RangeError|"
    # Generic phrases that quote / name the error
    r"traceback\s+\(most\s+recent\s+call|"
    r"stack\s+trace\s+(shows|says|reads)|"
    r"the\s+error\s+(says|reads|is)|"
    r"error\s+message\s+(says|reads|is)|"
    r"got\s*:|"
    r"cannot\s+read\s+propert(y|ies)|"
    r"is\s+not\s+a\s+function|"
    r"undefined\s+is\s+not"
    r")\b",
    re.IGNORECASE,
)

# Iterative refinement — the user revises the previous output instead of
# accepting it ("now also...", "instead of X try Y", "tweak the..."). Anthropic
# Claude Code best practices explicitly recommend iteration over one-shot
# requests; this is the positive counterpart of pure delegation.
_ITERATIVE_REFINEMENT = re.compile(
    r"\b("
    r"now\s+(also|make|change|add|remove|use|try|do)|"
    r"instead\s+of|"
    r"actually,?\s+(let'?s|can|could|use|try|make|change)|"
    r"can\s+you\s+(also|instead|now|change|tweak|refine|adjust|revise)|"
    r"let'?s\s+(also|instead|change|tweak|refine|adjust|revise|try)|"
    r"(tweak|refine|adjust|revise|rework|rewrite)\s+(the|this|that|it)|"
    r"change\s+\w+\s+to\s+\w+|"
    r"but\s+(with|use|make|change)"
    r")\b",
    re.IGNORECASE,
)


# --- Knowledge-gap classifier (US-006) -----------------------------------------
# Per-user-turn classifier that assigns at most one knowledge-gap subtype.
# Primary sources for the four subtypes:
#   - Anthropic Claude Code best practices: "be specific", "give the model
#     context", and "iterate" -- the explicit listing of *what fails* in a
#     prompt is the source for missing_context, missing_specs, and
#     unclear_instructions.
#   - OpenAI Codex / Responses docs on structured tool-use prompts: too many
#     bundled tasks in one turn confuses tool selection (multiple_context).
#   - Shen & Tamkin (2026), "How AI Impacts Skill Formation" (arXiv 2601.20245):
#     the atrophy pattern of vague, single-shot prompts maps onto these four
#     subtypes; their 17pp comprehension gap was largest when the user did not
#     supply context, specs, or specifics.
#
# Precedence (highest -> lowest), documented and enforced in
# `_classify_knowledge_gap`. A turn matching multiple categories is counted
# only against the highest-precedence one (no double counting):
#   1. missing_context     -- bare 'this/it/that' referent in a short turn
#                             with NO anchor (no code block, file extension,
#                             error class, traceback, or path).
#   2. missing_specs       -- creation request ("write me...", "build...",
#                             "create...") with no spec markers (Goal:,
#                             Acceptance:, requirements, must/should/shall,
#                             Given/When/Then, inputs/outputs).
#   3. multiple_context    -- 2+ transition markers ("also", "additionally",
#                             "on top of that") signalling disparate tasks
#                             bundled into one turn.
#   4. unclear_instructions-- vague qualifier phrases ("somehow", "the right
#                             way/thing/approach", "as needed/appropriate",
#                             "or whatever", "make X better/nicer", etc.).
KNOWLEDGE_GAP_KEYS: tuple[str, ...] = (
    "missing_context",
    "missing_specs",
    "multiple_context",
    "unclear_instructions",
)


def _zero_knowledge_gaps() -> dict[str, int]:
    """Return a fresh dict with all knowledge-gap subtype keys set to 0."""
    return {key: 0 for key in KNOWLEDGE_GAP_KEYS}


_BARE_REFERENT = re.compile(r"\b(this|it|that)\b", re.IGNORECASE)
_FILE_EXTENSION = re.compile(
    r"\b\w+\.(py|js|ts|tsx|jsx|go|rs|java|cpp|c|h|md|yaml|yml|json|toml|"
    r"sql|html|css|sh|rb|swift|kt|scala|php)\b",
    re.IGNORECASE,
)
_ERROR_CLASS = re.compile(r"\b[A-Z][a-zA-Z]*Error\b")
_TRACEBACK_MARKER = re.compile(r"\btraceback\b|\bstack\s+trace\b", re.IGNORECASE)

# Length cap for missing_context. Longer turns supply enough text on their own
# that "this/it/that" usually has a nearby anchor; bare referents in <=100-char
# turns are the lazy reference pattern Anthropic's "give context" guidance
# targets.
_MISSING_CONTEXT_MAX_LEN = 100


def _is_missing_context(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > _MISSING_CONTEXT_MAX_LEN:
        return False
    if not _BARE_REFERENT.search(stripped):
        return False
    if "```" in stripped:
        return False
    if _ERROR_CLASS.search(stripped):
        return False
    if _TRACEBACK_MARKER.search(stripped):
        return False
    if _FILE_EXTENSION.search(stripped):
        return False
    if "/" in stripped:
        return False
    return True


_CREATION_REQUEST = re.compile(
    r"^\s*(please\s+)?(write|make|create|build|generate|implement|design|"
    r"set\s+up|setup|add|do)\s",
    re.IGNORECASE,
)
_SPEC_MARKERS = re.compile(
    r"\b(goals?|constraints?|acceptance|criteria|criterion|requirements?|"
    r"inputs?|outputs?|must|should|shall|out\s+of\s+scope|non[\-\s]?goals?|"
    r"given|when|then|expected|behaviour|behavior)\b",
    re.IGNORECASE,
)


def _is_missing_specs(text: str) -> bool:
    if not _CREATION_REQUEST.match(text):
        return False
    if _SPEC_MARKERS.search(text):
        return False
    return True


_TRANSITION_MARKERS = re.compile(
    r"\b(also|and\s+also|additionally|on\s+top\s+of\s+that)\b",
    re.IGNORECASE,
)


def _is_multiple_context(text: str) -> bool:
    return len(_TRANSITION_MARKERS.findall(text)) >= 2


_UNCLEAR_INSTRUCTION_MARKERS = re.compile(
    r"\b("
    r"somehow|"
    r"or\s+whatever|"
    r"whatever\s+(makes\s+sense|works|you\s+think|you\s+want)|"
    r"the\s+right\s+(way|thing|approach)|"
    r"as\s+needed|"
    r"as\s+appropriate|"
    r"make\s+(it|this|that|the\s+[a-z]+)\s+(better|nicer|cleaner|prettier)|"
    r"clean\s+(it|this|that|the\s+[a-z]+)\s+up|"
    r"nicer|"
    r"i\s+(dunno|don'?t\s+know)|"
    r"figure\s+it\s+out|"
    r"you\s+know\s+what\s+i\s+mean|"
    r"do\s+(your|the)\s+thing|"
    r"some\s+kind\s+of|"
    r"something\s+like"
    r")\b",
    re.IGNORECASE,
)


def _classify_knowledge_gap(text: str) -> str | None:
    """Return the dominant knowledge-gap subtype for one user turn, or None.

    See the precedence comment block above. Each turn contributes at most 1
    to a single subtype; a turn matching no subtype returns None and leaves
    every knowledge_gaps key at its previous value.
    """
    if _is_missing_context(text):
        return "missing_context"
    if _is_missing_specs(text):
        return "missing_specs"
    if _is_multiple_context(text):
        return "multiple_context"
    if _UNCLEAR_INSTRUCTION_MARKERS.search(text):
        return "unclear_instructions"
    return None


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

    # --- Expansion (US-005). All counts are over USER turns. Defaults preserve
    # backward compatibility with callers that construct BehavioralSignals
    # positionally without the new fields.
    specification_artifact_count: int = 0
    error_naming_count: int = 0
    iterative_refinement_count: int = 0

    # --- Expansion (US-006). Knowledge-gap subtype counts. Dict (not nested
    # dataclass) so dataclasses.asdict + json.dumps round-trip cleanly for
    # session_scores.signals_json persistence. All four keys are always
    # present; an empty session yields all-zero values, not an empty dict.
    knowledge_gaps: dict[str, int] = field(default_factory=_zero_knowledge_gaps)


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
            specification_artifact_count=0,
            error_naming_count=0,
            iterative_refinement_count=0,
            knowledge_gaps=_zero_knowledge_gaps(),
        )

    n = len(user_turns)
    why_hits = sum(1 for t in user_turns if _WHY_QUESTIONS.search(t.content))
    comp_hits = sum(1 for t in user_turns if _COMPREHENSION_CHECKS.search(t.content))
    expl_hits = sum(1 for t in user_turns if _EXPLANATION_REQUESTS.search(t.content))
    del_hits = sum(1 for t in user_turns if _PURE_DELEGATION.match(t.content.strip()))
    out_hits = sum(1 for t in user_turns if _OUTSOURCED_DEBUG.search(t.content))
    tel_hits = sum(1 for t in user_turns if _is_telegraphic(t.content))
    own_hits = sum(1 for t in user_turns if _OWN_ATTEMPT_MARKERS.search(t.content))
    spec_hits = sum(1 for t in user_turns if _SPECIFICATION_ARTIFACT.search(t.content))
    err_hits = sum(1 for t in user_turns if _ERROR_NAMING.search(t.content))
    iter_hits = sum(1 for t in user_turns if _ITERATIVE_REFINEMENT.search(t.content))

    knowledge_gaps = _zero_knowledge_gaps()
    for t in user_turns:
        gap = _classify_knowledge_gap(t.content)
        if gap is not None:
            knowledge_gaps[gap] += 1

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
        specification_artifact_count=spec_hits,
        error_naming_count=err_hits,
        iterative_refinement_count=iter_hits,
        knowledge_gaps=knowledge_gaps,
    )
