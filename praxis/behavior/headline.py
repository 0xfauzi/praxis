"""LLM-generated headline sentence for the v0.2 trajectory model (US-046).

Spec section 7.3:

  For each label, generate a single sentence that names the specific
  behavior shift, not just the slope direction. The sentence is
  LLM-generated, with the slope numbers and bucket count as inputs.
  Truncate to 180 chars.

This module owns step 5 of the trajectory pipeline (after weekly
bucketing, slope+stderr, label assignment, and hysteresis). The LLM
does NOT pick the label - the label decision lives in
`praxis.behavior.labels` and is supplied to this function as an
input. The LLM's only job is to paraphrase the supplied label into
prose, given the slope numbers and bucket count for context.

Spec section 2.3 ("statistical math on time series"):

  The labels they produce (Learning, Drifting, etc.) are statistical
  facts, not LLM judgments. The LLM paraphrases the slope into prose;
  it does not invent the label.

To enforce this, the LLM call is constrained two ways:

  1. The system prompt explicitly forbids naming any other label.
  2. The response is rejected (fallback fires) if it names any other
     label as a whole word, case-insensitive.

When the LLM is unavailable (no API key) or violates the contract,
a deterministic per-label template is used as a graceful fallback.
The fallback templates intentionally avoid all other label names so
they always satisfy the same constraint.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Callable

from praxis.behavior.labels import WeeklyTrajectoryLabel


HEADLINE_MAX_CHARS: int = 180


@dataclass(frozen=True)
class _HeadlineContext:
    """Bundle of the three template inputs.

    Wrapping these in a single object lets every fallback template
    accept the same context argument while only referencing the
    fields it actually needs - so templates for STEADY / READING
    don't trip "unused parameter" diagnostics when they ignore the
    slope numbers.
    """

    engagement_slope: float
    delegation_slope: float
    bucket_count: int


def _fmt_magnitude(value: float) -> str:
    """Format a slope magnitude with 2 decimals. Sign-direction lives in the surrounding prose."""
    return f"{abs(value):.2f}"


_FALLBACK_TEMPLATES: dict[
    WeeklyTrajectoryLabel, Callable[[_HeadlineContext], str]
] = {
    WeeklyTrajectoryLabel.LEARNING: lambda ctx: (
        f"Engagement up {_fmt_magnitude(ctx.engagement_slope)}/wk, "
        f"delegation down {_fmt_magnitude(ctx.delegation_slope)}/wk over "
        f"{ctx.bucket_count} weeks. You're investing more cognition per "
        f"session, not less."
    ),
    WeeklyTrajectoryLabel.GROWING_AUTONOMY: lambda ctx: (
        f"Delegation down {_fmt_magnitude(ctx.delegation_slope)}/wk over "
        f"{ctx.bucket_count} weeks with engagement holding flat. You're "
        f"leaning on the AI less."
    ),
    WeeklyTrajectoryLabel.STEADY: lambda ctx: (
        f"No significant movement on either axis over {ctx.bucket_count} "
        f"weeks. Habit is locked in - good or bad."
    ),
    WeeklyTrajectoryLabel.DRIFTING: lambda ctx: (
        f"Delegation up {_fmt_magnitude(ctx.delegation_slope)}/wk over "
        f"{ctx.bucket_count} weeks while engagement held flat. You're "
        f"shipping more, but checking less."
    ),
    WeeklyTrajectoryLabel.ATROPHYING: lambda ctx: (
        f"Delegation up {_fmt_magnitude(ctx.delegation_slope)}/wk, "
        f"engagement down {_fmt_magnitude(ctx.engagement_slope)}/wk over "
        f"{ctx.bucket_count} weeks. This is the at-risk pattern in the "
        f"literature."
    ),
    WeeklyTrajectoryLabel.READING: lambda ctx: (
        f"Only {ctx.bucket_count} eligible weekly buckets so far - need "
        f"at least 4 weeks of data before a trajectory can be called."
    ),
}


def _other_labels(label: WeeklyTrajectoryLabel) -> list[str]:
    """Display strings of every label except the supplied one."""
    return [l.value for l in WeeklyTrajectoryLabel if l is not label]


def _violates_label_constraint(sentence: str, label: WeeklyTrajectoryLabel) -> bool:
    """True if `sentence` names any label other than `label` as a whole word.

    Whole-word, case-insensitive. Multi-word labels (e.g. "Growing
    autonomy") are matched as a single phrase. Word boundaries make
    natural prose like "steadily improving" safe when the supplied
    label is not STEADY - we only catch the literal label name.
    """
    for other in _other_labels(label):
        pattern = r"\b" + re.escape(other) + r"\b"
        if re.search(pattern, sentence, flags=re.IGNORECASE):
            return True
    return False


def _truncate(sentence: str, limit: int = HEADLINE_MAX_CHARS) -> str:
    s = sentence.strip()
    return s if len(s) <= limit else s[:limit]


def fallback_headline(
    label: WeeklyTrajectoryLabel,
    engagement_slope: float,
    delegation_slope: float,
    bucket_count: int,
) -> str:
    """Deterministic per-label headline template.

    Used when no API key is configured, when the LLM call fails, or
    when the LLM violates the paraphrase constraint. Always returns
    a sentence of length <= HEADLINE_MAX_CHARS.
    """
    template = _FALLBACK_TEMPLATES[label]
    ctx = _HeadlineContext(
        engagement_slope=engagement_slope,
        delegation_slope=delegation_slope,
        bucket_count=bucket_count,
    )
    return _truncate(template(ctx))


def _build_prompt(
    label: WeeklyTrajectoryLabel,
    engagement_slope: float,
    delegation_slope: float,
    bucket_count: int,
) -> tuple[str, str]:
    """Return (system, user) prompts for the headline LLM call."""
    forbidden = ", ".join(_other_labels(label))
    system = (
        "You write the headline sentence for a user's weekly AI-usage "
        "digest. The trajectory label has already been chosen for you by "
        "the system; your job is only to paraphrase it into one short "
        "sentence using the supplied slope numbers and bucket count.\n\n"
        "Constraints:\n"
        "- Exactly one sentence.\n"
        f"- At most {HEADLINE_MAX_CHARS} characters total.\n"
        f"- Paraphrase the supplied label '{label.value}'. Do NOT name a "
        "different trajectory label.\n"
        f"- Specifically, do NOT use any of these words/phrases: {forbidden}.\n"
        "- Name the specific behavior shift, not just slope direction.\n"
        "- Plain prose. No JSON, no preamble, no markdown, no quotes."
    )
    user = (
        f"Trajectory label: {label.value}\n"
        f"Engagement slope: {engagement_slope:+.4f} per week\n"
        f"Delegation slope: {delegation_slope:+.4f} per week\n"
        f"Weekly buckets observed: {bucket_count}\n\n"
        "Write the headline sentence now."
    )
    return system, user


def _call_anthropic(system: str, user: str) -> str | None:
    try:
        from anthropic import Anthropic  # type: ignore

        client = Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            getattr(b, "text", "")
            for b in resp.content
            if getattr(b, "type", None) == "text"
        )
        return text or None
    except Exception as exc:  # noqa: BLE001
        print(f"[behavior] headline anthropic call failed: {exc!r}", file=sys.stderr)
        return None


def _call_openai(system: str, user: str) -> str | None:
    try:
        from openai import OpenAI  # type: ignore

        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or None
    except Exception as exc:  # noqa: BLE001
        print(f"[behavior] headline openai call failed: {exc!r}", file=sys.stderr)
        return None


def _call_llm(system: str, user: str) -> str | None:
    """One LLM call. Returns None when no provider is configured or the call fails.

    Anthropic is preferred when both keys are present, matching the
    rest of the v0.2 pipeline.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _call_anthropic(system, user)
    if os.environ.get("OPENAI_API_KEY"):
        return _call_openai(system, user)
    return None


def generate_headline(
    label: WeeklyTrajectoryLabel,
    engagement_slope: float,
    delegation_slope: float,
    bucket_count: int,
) -> str:
    """Produce a single headline sentence for the supplied trajectory label.

    Per spec section 7.3, the sentence is LLM-generated with the slope
    numbers and bucket count as inputs and truncated to 180 chars.

    The label itself is NOT chosen by the LLM (spec section 2.3): this
    function only emits prose paraphrasing the label that the caller
    has already determined via `label_trajectory_with_hysteresis`.

    Falls back to `fallback_headline` when:
      - No API key is configured.
      - The LLM call fails or returns empty/whitespace-only output.
      - The LLM names a different label (violates the paraphrase
        constraint).

    Always returns a string of length <= HEADLINE_MAX_CHARS.
    """
    system, user = _build_prompt(
        label, engagement_slope, delegation_slope, bucket_count
    )
    raw = _call_llm(system, user)
    if raw is None:
        return fallback_headline(
            label, engagement_slope, delegation_slope, bucket_count
        )
    raw = raw.strip()
    if not raw or _violates_label_constraint(raw, label):
        return fallback_headline(
            label, engagement_slope, delegation_slope, bucket_count
        )
    return _truncate(raw)
