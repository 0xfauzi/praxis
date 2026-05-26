"""The Praxis Rubric.

Six dimensions, weighted by evidence strength. Each dimension is scored
0-10. The weighted sum is the overall /10 score.

Citations are tracked so the user can see WHY a behavior matters, not
just THAT it matters. This is the Praxis differentiator: evidence over
claims.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    key: str
    title: str
    description: str
    weight: float
    evidence: str
    # What "great" looks like — used in coaching prompts and report
    exemplar: str


RUBRIC: list[Dimension] = [
    Dimension(
        key="planning",
        title="Planning before prompting",
        description=(
            "Do you outline what you want before asking? State the goal, "
            "constraints, acceptance criteria, and success conditions up front."
        ),
        weight=0.20,
        evidence=(
            "Sarkar (2025) found experienced agent users are more likely to "
            "develop plans in their initial messages, and that one standard "
            "deviation higher work experience corresponds to 6% higher "
            "agent code accept rates. Planning is the strongest predictor "
            "of agent productivity gains."
        ),
        exemplar=(
            "Goal: refactor auth module to use JWT. Constraints: keep "
            "existing test suite green, no breaking API changes. Done when: "
            "all tests pass and /login returns a valid token."
        ),
    ),
    Dimension(
        key="context",
        title="Context richness",
        description=(
            "Do you bring relevant material into the prompt — files, errors, "
            "specs, prior decisions — rather than expecting the model to guess?"
        ),
        weight=0.20,
        evidence=(
            "OpenRouter's State of AI 2025 study of 100T tokens shows average "
            "prompt length grew nearly fourfold to over 6K tokens since "
            "early 2024. The most effective workloads are context-rich; "
            "models are increasingly analytical engines, not creative "
            "generators."
        ),
        exemplar=(
            "Pastes the failing test output, the function under test, and "
            "the relevant config — then asks 'why is this failing?'"
        ),
    ),
    Dimension(
        key="iteration",
        title="Iteration & evaluation",
        description=(
            "Do you refine the prompt when the answer isn't right, or just "
            "accept the first draft? Do you push back, ask 'what would make "
            "this better?', and tighten the spec?"
        ),
        weight=0.18,
        evidence=(
            "Sarkar (2025) identifies abstraction, clarity, and evaluation "
            "as the core skills of effective AI users. The semantic activity "
            "of instructing and evaluating agents matters more than the "
            "syntactic activity of typing."
        ),
        exemplar=(
            "First response is too vague; user replies 'too generic — show "
            "me three concrete options with trade-offs', then critiques "
            "each option to converge on the right answer."
        ),
    ),
    Dimension(
        key="tools",
        title="Tool & multi-step use",
        description=(
            "Are you using the AI as an agent — letting it run code, search, "
            "and chain steps — or only as a single-turn Q&A box?"
        ),
        weight=0.14,
        evidence=(
            "OpenRouter's State of AI 2025 report shows tool-capable patterns "
            "rising sharply. Sequence lengths have more than tripled to over "
            "5,400 tokens, driven by embedded, sophisticated agentic "
            "workflows rather than user verbosity."
        ),
        exemplar=(
            "Asks the agent to read the codebase, run the tests, identify "
            "the failure, propose a fix, and verify the fix in one flow."
        ),
    ),
    Dimension(
        key="fit",
        title="Model–task fit",
        description=(
            "Are you picking the right model for the job — reasoning models "
            "for hard problems, fast models for simple ones, vision models "
            "for images?"
        ),
        weight=0.14,
        evidence=(
            "OpenRouter data shows users converging on specific model–task "
            "fits ('Glass Slipper' retention). Praxis's cross-model "
            "methodology treats vendor-agnostic fluency as a core "
            "practitioner skill."
        ),
        exemplar=(
            "Uses a reasoning model (e.g. Claude Opus / o-series) for "
            "architecture decisions; a fast model for boilerplate; a "
            "long-context model for document review."
        ),
    ),
    Dimension(
        key="verification",
        title="Verification habits",
        description=(
            "Do you check the output before you act on it? Ask for sources, "
            "spot-check claims, run the generated code, push back on "
            "confident-but-wrong answers?"
        ),
        weight=0.14,
        evidence=(
            "Anthropic's safety and grounding guidance, plus the broader "
            "literature on LLM hallucination, identifies verification as "
            "the dividing line between practitioners and casual users. "
            "Confident-sounding output is not the same as correct output."
        ),
        exemplar=(
            "When the AI cites a statistic, user asks 'what's the source?' "
            "and verifies it. When generated code looks plausible, user "
            "runs it before merging."
        ),
    ),
]


def total_weight() -> float:
    return sum(d.weight for d in RUBRIC)


def by_key(key: str) -> Dimension:
    for d in RUBRIC:
        if d.key == key:
            return d
    raise KeyError(key)


# Sanity check: weights should sum to 1.0
assert abs(total_weight() - 1.0) < 1e-9, f"Rubric weights sum to {total_weight()}, not 1.0"
