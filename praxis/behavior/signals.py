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
    # behavioral-signals US-006 patterns: vague qualifier phrasing
    r"somehow|"
    r"or\s+whatever|"
    r"whatever\s+(makes\s+sense|works|you\s+think|you\s+want)|"
    r"the\s+right\s+(way|thing|approach)|"
    r"as\s+needed|"
    r"as\s+appropriate|"
    r"make\s+(it|this|that|the\s+[a-z]+)\s+(better|nicer|cleaner|prettier|work|good|right)|"
    r"clean\s+(it|this|that|the\s+[a-z]+)\s+up|"
    r"nicer|"
    r"i\s+(dunno|don'?t\s+know)|"
    r"figure\s+(it|this|that|the\s+[a-z]+)\s+out|"
    r"you\s+know\s+what\s+i\s+mean|"
    r"do\s+(your|the)\s+thing|"
    r"some\s+kind\s+of|"
    r"something\s+like|"
    # report-panels US-040 patterns: vague verb-object phrasing
    r"do\s+something(\s+(with|to|about))?|"
    r"handle\s+(this|it|that)(\s+(somehow|properly|right|correctly))?|"
    r"deal\s+with\s+(this|it|that)|"
    r"sort\s+(this|it|that)\s+out|"
    r"can\s+you\s+(just|maybe)\s+(help|look)"
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


# --- Expansion signals (US-007) ------------------------------------------------
# Five flat counters covering practices that primary sources call out as
# raising the quality of AI-assisted development:
#   - Anthropic Claude Code best practices: "plan before code" (plan mode,
#     shift-tab in the CLI), "scaffold the project skeleton up front", "follow
#     the patterns in this repo", and "give the model context via CLAUDE.md".
#   - OpenAI Codex / Responses docs: AGENTS.md and the "give the model an
#     instructions file" pattern; structured handoffs between user and tool.
#   - arXiv 2506.01604 (AI-assisted software development practices): the
#     test-driven and recipe / pattern-reuse practices separating skilled
#     from unskilled use; specifically the test-first habit and the
#     follow-the-prior-pattern habit. (Cite kept general -- the paper id
#     itself is the canonical reference.)

# Plan mode -- user explicitly asks for / invokes plan mode, or asks for a
# written plan before any code. Anthropic Claude Code surfaces "plan mode"
# via shift-tab and the EnterPlanMode tool; the practice is to plan before
# implementing on non-trivial work.
_PLAN_MODE = re.compile(
    r"\b("
    r"plan\s+mode|"
    r"/plan\b|"
    r"enterplanmode|exitplanmode|"
    r"draft\s+(a|the)\s+plan|"
    r"outline\s+(a|the|your)\s+plan|"
    r"sketch\s+(a|the)\s+plan|"
    r"make\s+(a|the)\s+plan|"
    r"propose\s+(a|the|your)\s+plan|"
    r"let'?s\s+plan\b|"
    r"plan\s+(this|it|that|first|before)\b|"
    r"plan\s+it\s+out|"
    r"walk\s+me\s+through\s+(the|your|a)\s+plan|"
    r"don'?t\s+(write|implement|code).{0,40}until.{0,20}plan|"
    r"before\s+(you\s+)?(writing|implementing|coding|building).{0,40}\bplan\b"
    r")\b",
    re.IGNORECASE,
)

# Scaffolding artifact -- user asks for or supplies a project skeleton,
# boilerplate, or starter layout. Anthropic Claude Code best practice is to
# scaffold the directory layout and boilerplate before iterating on logic.
_SCAFFOLDING_ARTIFACT = re.compile(
    r"\b("
    r"scaffold(ing|s|ed)?|"
    r"skeleton|"
    r"boilerplate|"
    r"starter\s+(kit|code|project|template|files?|repo)|"
    r"bootstrap\s+(a|the|this|that|us|me|new|fresh|empty|the\s+project|a\s+new)|"
    r"project\s+(structure|layout|skeleton|template|scaffold)|"
    r"(directory|folder|file)\s+(structure|layout|tree)|"
    r"set\s+up\s+the\s+(project|directory|folder|repo)\s+(structure|layout|tree|skeleton)"
    r")\b",
    re.IGNORECASE,
)

# TDD marker -- user explicitly invokes test-driven development practice.
# Cite: arXiv 2506.01604 on practice patterns and the long-standing TDD
# canon (Beck, 2002); the "write a failing test first" cue is the most
# robust textual marker.
_TDD_MARKER = re.compile(
    r"\b("
    r"tdd\b|"
    r"test[\s\-]driven|"
    r"test[\s\-]first|"
    r"write\s+(a\s+|the\s+)?failing\s+tests?|"
    r"start\s+with\s+(a\s+|the\s+)?failing\s+tests?|"
    r"red[\s\-]green[\s\-]refactor|"
    r"(red|green|refactor)\s+phase|"
    r"tests?\s+before\s+(implementation|the\s+code|you\s+(write|implement)|writing|implementing)|"
    r"write\s+(the\s+)?tests?\s+first|"
    r"fail(ing)?\s+tests?\s+first"
    r")\b",
    re.IGNORECASE,
)

# Recipe pattern -- user invokes an existing pattern in the codebase rather
# than asking for ad-hoc code. Anthropic Claude Code best practice is "follow
# the patterns in this repo"; arXiv 2506.01604 frames pattern-reuse as a
# practice that separates skilled from unskilled use of AI assistants.
_RECIPE_PATTERN = re.compile(
    r"\b("
    r"follow\s+the\s+(same\s+)?(pattern|approach|recipe|convention|style|format)|"
    r"mirror(s|ing)?\s+(the|that|this)\s+(pattern|approach|structure|format|layout)|"
    r"use\s+the\s+(same\s+)?(pattern|approach|recipe|convention|format)|"
    r"same\s+(pattern|approach|recipe|convention|format)\s+as|"
    r"(just\s+)?(like|as)\s+(we\s+)?(did|do|have|already\s+did)\s+(in|for|with|when|here)|"
    r"similar\s+to\s+(how|the\s+way|what)|"
    r"match(ing|es)?\s+the\s+(pattern|style|convention|format|shape)|"
    r"copy\s+(the\s+)?(pattern|approach|style|convention|format|recipe)\s+(from|of)|"
    r"follow\s+the\s+recipe"
    r")\b",
    re.IGNORECASE,
)

# Context-and-instructions -- user references a project-level instructions
# file (CLAUDE.md, AGENTS.md, .cursorrules, copilot-instructions) or supplies
# an explicit context block. Anthropic and OpenAI both document this as the
# top-leverage move for steering coding agents.
_CONTEXT_INSTRUCTIONS = re.compile(
    r"("
    r"\bclaude\.md\b|"
    r"\bagents?\.md\b|"
    r"\b\.?cursor[\-_\.]?rules?\b|"
    r"\bcopilot[\-_]instructions(\.md)?\b|"
    r"\b(per|see|read|check|consult|reference|following|according\s+to|in)\s+"
    r"(the\s+)?(claude\.md|agents?\.md|readme|context\s+(file|doc|block|section)|instructions\s+(file|doc|block|section))\b|"
    r"\b(for|here'?s|here\s+is)\s+(the\s+|some\s+)?context\b|"
    r"\bcontext\s+(block|file|doc(ument)?|section)\b|"
    r"\binstructions?\s+(file|doc(ument)?|block|section)\b|"
    r"\bsystem\s+prompt\b"
    r")",
    re.IGNORECASE,
)


# --- Expansion signals (US-008) ------------------------------------------------
# Verification depth, code comprehension, and the agent-capability ladder.
# Primary sources:
#   - Anthropic Claude Code best practices: "read the code before changing it",
#     "run the tests yourself", "report what you actually verified". The four
#     verification_depth subtypes mirror Anthropic's documented practice
#     ladder: source_check > test_run > spot_check > blanket_accept.
#   - OpenAI Codex / Responses docs on tools, skills, hooks, and subagents.
#     The tool_ladder_level rungs (0..4) mirror the shared "agent capability
#     ladder" -- prompt-only, then tools, then skills, then hooks, then
#     subagents -- in order of increasing automation leverage.
#   - Shen & Tamkin (2026), "How AI Impacts Skill Formation" (arXiv 2601.20245):
#     source_check maps onto their generation-then-comprehension finding;
#     spot_check vs blanket_accept maps onto the verification-vs-pure-
#     delegation split that drove their 17pp comprehension gap in debugging.

VERIFICATION_DEPTH_KEYS: tuple[str, ...] = (
    "source_check",
    "test_run",
    "spot_check",
    "blanket_accept",
)


def _zero_verification_depth() -> dict[str, int]:
    """Return a fresh dict with all verification_depth subtype keys set to 0."""
    return {key: 0 for key in VERIFICATION_DEPTH_KEYS}


# source_check -- the user reports actually reading the code, docs, or
# implementation rather than trusting an assistant summary.
_SOURCE_CHECK = re.compile(
    r"\b("
    r"i\s+(read|skimmed|browsed|inspected|reviewed)\s+(through\s+)?"
    r"(the\s+|some\s+)?(source|code|implementation|impl|module|file|lines?|"
    r"repo|library|docs?|documentation)|"
    r"looked\s+(at|into|through)\s+(the\s+)?(source|code|implementation|impl|"
    r"module|file|docs?|documentation|repo|library|definition|signature)|"
    r"checked\s+(the\s+)?(source|code|implementation|impl|docs?|documentation|"
    r"signature|definition)|"
    r"read\s+(through\s+)?(the\s+)?(source|code|impl(ementation)?|"
    r"docs?|documentation|definition|signature|file|module|library)|"
    r"i\s+grepped|grepped\s+(for|the|through)|"
    r"git\s+(log|blame|show)|"
    r"reading\s+(the\s+)?(source|code|docs?|implementation|impl|file|module)|"
    r"after\s+(reading|checking|inspecting)\s+(the\s+)?(code|source|docs?|"
    r"implementation|impl|file|module)"
    r")\b",
    re.IGNORECASE,
)

# test_run -- the user reports running tests (or seeing tests pass) as
# verification, rather than just asking for tests to be written.
_TEST_RUN = re.compile(
    r"\b("
    r"i\s+ran\s+(the\s+|some\s+|all\s+)?(unit\s+|integration\s+|e2e\s+|smoke\s+)?tests?\b|"
    r"i\s+ran\s+(uv\s+run\s+)?(pytest|mypy|pyright|jest|vitest|cargo\s+test|"
    r"go\s+test|npm\s+(run\s+)?test)|"
    r"running\s+(the\s+|all\s+)?tests?\s+(now|first|locally|to\s+(check|verify))|"
    r"all\s+tests?\s+(pass(ed|ing)?|are\s+green)|"
    r"tests?\s+pass(ed|ing)?\s+(locally|now|cleanly)|"
    r"test\s+suite\s+(pass(ed|es)?|is\s+green|ran\s+green)|"
    r"after\s+running\s+(the\s+|all\s+)?tests?|"
    r"the\s+tests?\s+(pass(ed|es)?|are\s+green|came\s+back\s+green)|"
    r"uv\s+run\s+pytest|"
    r"all\s+green\b|"
    r"green\s+locally|"
    r"smoke[\s\-]?test\s+(pass(ed|es)?|ran|works)"
    r")",
    re.IGNORECASE,
)

# spot_check -- the user reports verifying a small sample rather than a
# full test run; weaker than test_run but stronger than blanket_accept.
_SPOT_CHECK = re.compile(
    r"\b("
    r"spot[\s\-]?check(ed|ing|s)?|"
    r"smoke[\s\-]?test(ed|ing)?|"
    r"sanity[\s\-]?check(ed|ing|s)?|"
    r"eyeball(ed|ing|s)?|"
    r"skim(med)?\s+(through|over|the)|"
    r"(quick(ly)?|briefly)\s+(check(ed)?|scan(ned)?|look(ed)?|glance|browse(d)?|"
    r"review(ed)?|verif(ied|y))|"
    r"i\s+(just\s+)?checked\s+(a\s+(few|couple|sample|handful)|one|two|three)|"
    r"checked\s+a\s+(few|couple|sample|handful)|"
    r"quick\s+glance|"
    r"at\s+a\s+glance|"
    r"hand[\s\-]checked\s+(one|two|three|a\s+(few|couple))"
    r")\b",
    re.IGNORECASE,
)

# blanket_accept -- the user accepts output without any verification.
# This is the atrophy end of the ladder (mirrors Shen & Tamkin's "pure
# delegation" pattern).
_BLANKET_ACCEPT = re.compile(
    r"\b("
    r"lgtm|"
    r"looks\s+(good|great|fine|right|correct|ok|okay)|"
    r"looks?\s+good\s+to\s+me|"
    r"ship\s+it\b|"
    r"let'?s\s+ship\b|"
    r"merge\s+it\b|"
    r"good\s+to\s+(merge|go|ship)|"
    r"go\s+ahead\s+(and\s+(merge|ship)|with\s+(it|that))|"
    r"i'?ll\s+take\s+(it|that)|"
    r"i'?m\s+(good|happy)\s+with\s+(it|this|that)|"
    r"that\s+works\s+for\s+me|"
    r"sounds\s+good\b|"
    r"approved\b"
    r")\b",
    re.IGNORECASE,
)


# code_comprehension -- the user demonstrates understanding by paraphrasing
# what the code does, rather than asking what it does. Distinct from
# `comprehension_check_count` (which captures verification *questions* like
# "am I right that..."): this captures *statements* of understanding ("the
# function takes X and returns Y", "this loop iterates over...").
_CODE_COMPREHENSION = re.compile(
    r"\b("
    r"(the|this)\s+(function|method|class|module|loop|code|block|line|file|"
    r"snippet|helper|callback|generator|coroutine|handler|decorator|"
    r"dataclass|fixture)\s+"
    r"(takes|returns|does|handles|iterates|implements|computes|reads|writes|"
    r"loops|runs|executes|calls|invokes|maps|filters|sorts|reduces|"
    r"checks|verifies|validates|parses|builds|constructs|emits|yields|"
    r"raises|wraps)|"
    r"(this|it)\s+is\s+(iterating|looping|reading|writing|mapping|filtering|"
    r"sorting|reducing|computing|implementing|handling|parsing|building|"
    r"calling|invoking|emitting|yielding|raising|wrapping)|"
    r"i\s+see\s+(that|how|why|what)\s+(it|this|the\s+[a-z_]+)\s+"
    r"(does|takes|returns|handles|iterates|implements|reads|writes|calls|"
    r"yields|raises|wraps|maps|filters|parses)|"
    r"ah,?\s+so\s+(it|this|that)\s+(does|takes|returns|handles|loops|iterates|"
    r"yields|raises|wraps|reads|writes)|"
    r"now\s+i\s+(see|understand|get\s+it)\b|"
    r"so\s+(the|this)\s+(function|method|code|loop|module|class|decorator|"
    r"generator|handler)\s+(does|takes|returns|handles|iterates|reads|"
    r"writes|yields|raises|wraps)|"
    r"i\s+(see|understand)\s+now\s+(that|how|why)"
    r")\b",
    re.IGNORECASE,
)


# --- tool_ladder_level rungs (US-008) -----------------------------------------
# Per the agent-capability ladder shared between Anthropic Claude Code and
# OpenAI Codex docs, ordered by increasing automation leverage:
#   0 = prompt-only      -- the user types text, the agent answers in text.
#   1 = tools-on         -- the session has generic tools enabled but no
#                            specific tool/skill invocation visible.
#   2 = skills           -- explicit tool/skill calls (Turn.tool_calls
#                            truthy, "tool call", "Skill tool", "/skill X").
#                            Per the US-008 AC, "tool call" maps to this
#                            rung (not rung 1) -- skills are the surface
#                            through which tool calls happen in Claude Code.
#   3 = hooks            -- the session references hook scripts (Claude Code
#                            hooks, PreToolUse, settings.json hooks).
#   4 = subagents        -- the session references subagent dispatch (Task
#                            tool, "spawn a subagent", "delegate to a
#                            subagent").
#
# tool_ladder_level for a session is `max(rung-per-turn over all turns,
# default 0)`. It is NOT a sum: a session that mixes a tool call AND a hook
# reference resolves to max(2, 3) = 3, not 5. The ladder is scanned across
# the whole transcript (every Turn role), since tool_calls appear on
# assistant turns while hook/subagent references can appear in either user
# or assistant text.
TOOL_LADDER_PROMPT_ONLY: int = 0
TOOL_LADDER_TOOLS_ON: int = 1
TOOL_LADDER_SKILLS: int = 2
TOOL_LADDER_HOOKS: int = 3
TOOL_LADDER_SUBAGENTS: int = 4

_TOOL_LADDER_SUBAGENT = re.compile(
    r"\b("
    r"sub[\s\-]?agents?|"
    r"task\s+tool|"
    r"spawn(ing|s|ed)?\s+(a|an|the|sub|multiple|parallel)\s+(sub|agent)|"
    r"delegate\s+(this|that|it|the\s+\w+)?\s*to\s+(a|an|the)?\s*sub[\s\-]?agent|"
    r"orchestrate\s+(agents?|sub|multiple\s+agents?)|"
    r"parallel\s+sub[\s\-]?agents?|"
    r"agent\s+sdk"
    r")\b",
    re.IGNORECASE,
)

_TOOL_LADDER_HOOK = re.compile(
    r"\b("
    r"claude\s+code\s+hooks?|"
    r"settings\.json\s+hooks?|"
    r"(pre|post)[\s\-]?tool[\s\-]?use[\s\-]?hooks?|"
    r"pretooluse|posttooluse|"
    r"user[\s\-]?prompt[\s\-]?submit[\s\-]?hooks?|"
    r"userpromptsubmit|"
    r"stop\s+hooks?|"
    r"session[\s\-]?(start|end)[\s\-]?hooks?|"
    r"git\s+hooks?|"
    r"pre[\s\-]?commit\s+hooks?|"
    r"lifecycle\s+hooks?|"
    r"hook\s+(configuration|script|command|fires|runs|executes|that)|"
    r"configure\s+(a\s+|the\s+)?hooks?|"
    r"hooks?\s+(fire|run|execute|trigger|configured|set\s+up)"
    r")\b",
    re.IGNORECASE,
)

_TOOL_LADDER_SKILL = re.compile(
    r"\b("
    r"tool\s+calls?|"
    r"function\s+calls?|"
    r"call(ed|ing)?\s+the\s+\w+\s+tool|"
    r"the\s+\w+\s+tool\s+(call|invocation|return|result)|"
    r"skill\s+tool|"
    r"/skill\b|"
    r"skill\s+invocation|"
    r"invok(e|ed|ing|es)\s+(a|the)?\s*(tool|skill)|"
    r"using\s+the\s+\w+\s+tool|"
    r"via\s+the\s+\w+\s+tool"
    r")\b",
    re.IGNORECASE,
)

_TOOL_LADDER_TOOLS_ON = re.compile(
    r"\b("
    r"tools?\s+(are\s+)?(on|enabled|available)|"
    r"enable\s+(the\s+)?tools?|"
    r"tools[\s\-]on|"
    r"with\s+tools?\s+enabled|"
    r"tool[\s\-]use\s+(turned\s+)?on|"
    r"agent\s+(with|has)\s+tools?|"
    r"turn\s+on\s+tools?"
    r")\b",
    re.IGNORECASE,
)


def _tool_ladder_rung_for_turn(turn: Turn) -> int:
    """Return the highest ladder rung observed in a single turn.

    Order of precedence (highest match wins): subagent > hook > skill (incl.
    Turn.tool_calls truthy) > tools-on > prompt-only. Returns an int in 0..4.
    """
    text = turn.content
    if _TOOL_LADDER_SUBAGENT.search(text):
        return TOOL_LADDER_SUBAGENTS
    if _TOOL_LADDER_HOOK.search(text):
        return TOOL_LADDER_HOOKS
    if turn.tool_calls or _TOOL_LADDER_SKILL.search(text):
        return TOOL_LADDER_SKILLS
    if _TOOL_LADDER_TOOLS_ON.search(text):
        return TOOL_LADDER_TOOLS_ON
    return TOOL_LADDER_PROMPT_ONLY


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

    # --- Expansion (US-007). Five flat per-user-turn counters covering
    # practices from Anthropic Claude Code best practices, OpenAI Codex docs,
    # and arXiv 2506.01604. Defaults preserve backward compatibility with
    # positional constructors in existing tests.
    plan_mode_count: int = 0
    scaffolding_artifact_count: int = 0
    tdd_marker_count: int = 0
    recipe_pattern_count: int = 0
    context_instructions_count: int = 0

    # --- Expansion (US-008). verification_depth is dict-valued (same shape
    # as knowledge_gaps so asdict + json.dumps round-trip cleanly). All four
    # keys are always present; an empty session yields all-zero values.
    # A single user turn can increment multiple subtypes (no precedence rule
    # -- the lenses are orthogonal, e.g., "I ran the tests, LGTM" counts as
    # both test_run and blanket_accept).
    verification_depth: dict[str, int] = field(default_factory=_zero_verification_depth)
    # code_comprehension_count is a flat per-user-turn count of *statements*
    # of understanding (distinct from comprehension_check_count, which counts
    # verification *questions*).
    code_comprehension_count: int = 0
    # tool_ladder_level is a single ordinal 0..4 -- the max rung observed
    # across the WHOLE transcript (all turn roles, not just user turns).
    # See the rung definitions and the AC's max(2, 3) = 3 example.
    tool_ladder_level: int = 0


def extract(session: Session) -> BehavioralSignals:
    user_turns = session.user_turns
    if not user_turns:
        # tool_ladder_level is still scanned over the whole transcript even
        # if there are no user turns, since assistant turns can carry
        # tool_calls or hook/subagent references.
        tool_ladder_level = max(
            (_tool_ladder_rung_for_turn(t) for t in session.turns),
            default=TOOL_LADDER_PROMPT_ONLY,
        )
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
            plan_mode_count=0,
            scaffolding_artifact_count=0,
            tdd_marker_count=0,
            recipe_pattern_count=0,
            context_instructions_count=0,
            verification_depth=_zero_verification_depth(),
            code_comprehension_count=0,
            tool_ladder_level=tool_ladder_level,
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

    plan_hits = sum(1 for t in user_turns if _PLAN_MODE.search(t.content))
    scaf_hits = sum(1 for t in user_turns if _SCAFFOLDING_ARTIFACT.search(t.content))
    tdd_hits = sum(1 for t in user_turns if _TDD_MARKER.search(t.content))
    recipe_hits = sum(1 for t in user_turns if _RECIPE_PATTERN.search(t.content))
    ctx_hits = sum(1 for t in user_turns if _CONTEXT_INSTRUCTIONS.search(t.content))

    verification_depth = _zero_verification_depth()
    for t in user_turns:
        if _SOURCE_CHECK.search(t.content):
            verification_depth["source_check"] += 1
        if _TEST_RUN.search(t.content):
            verification_depth["test_run"] += 1
        if _SPOT_CHECK.search(t.content):
            verification_depth["spot_check"] += 1
        if _BLANKET_ACCEPT.search(t.content):
            verification_depth["blanket_accept"] += 1
    comprehension_hits = sum(
        1 for t in user_turns if _CODE_COMPREHENSION.search(t.content)
    )
    # tool_ladder_level scans the WHOLE transcript (not just user turns), per
    # the AC: max-rung-observed; tool_calls live on assistant turns.
    tool_ladder_level = max(
        (_tool_ladder_rung_for_turn(t) for t in session.turns),
        default=TOOL_LADDER_PROMPT_ONLY,
    )

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
        plan_mode_count=plan_hits,
        scaffolding_artifact_count=scaf_hits,
        tdd_marker_count=tdd_hits,
        recipe_pattern_count=recipe_hits,
        context_instructions_count=ctx_hits,
        verification_depth=verification_depth,
        code_comprehension_count=comprehension_hits,
        tool_ladder_level=tool_ladder_level,
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


# `_UNCLEAR_INSTRUCTION_MARKERS` is defined once near the top of this
# module (its broader pattern matches both the behavioral-signals fixtures
# and the report-panels detector's needs). The duplicate definition that
# shipped in two parallel branches was removed at merge time so the
# top-of-file pattern stays canonical.


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
