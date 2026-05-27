"""Coaching generator.

Given a profile snapshot, generate concrete, targeted instructions for
the user to improve their AI usage. Coaching is anchored to the user's
two weakest dimensions, not generic best-practice slop.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.rubric import by_key


@dataclass
class Coaching:
    headline: str
    focus_areas: list[dict]  # [{dimension, current_score, target, drills: [...]}]
    daily_practice: str      # one concrete habit to try tomorrow
    generated_by: str


_FALLBACK_DRILLS: dict[str, list[str]] = {
    "planning": [
        "Before your next session, write the goal in one sentence, the constraints in three bullets, and the 'done when' criterion. Paste that in as turn one.",
        "Ask the assistant to restate your plan in its own words before doing anything. Correct it before approving.",
        "For complex work, ask for a step-by-step plan first, then approve or amend it before execution starts.",
    ],
    "context": [
        "When you ask about code, paste the relevant function AND its callers. Don't make the model guess the surrounding logic.",
        "Before asking 'why is this broken?', paste the error, the input, and your expected output.",
        "Build a 'context block' template you reuse: project description, current task, files in scope, constraints, prior decisions.",
    ],
    "iteration": [
        "When the first answer is generic, don't accept it. Reply: 'too generic — give me three concrete options with trade-offs.'",
        "Ask 'what would make this better?' or 'what assumption did you make that I should challenge?' on every substantive response.",
        "Use a critique loop: ask the model to critique its own answer, then revise.",
    ],
    "tools": [
        "Move from single-turn questions to multi-step requests: 'read the file, run the tests, identify the failure, propose a fix.'",
        "Let the agent search/browse/execute when it would speed things up. Authorize the tools it actually needs.",
        "For repeated workflows, build a skill or sub-agent rather than retyping the setup each time.",
    ],
    "fit": [
        "For architecture decisions or hard reasoning, use a top-tier reasoning model. Don't waste tokens on speed there.",
        "For boilerplate, conversions, and simple lookups, use a fast cheap model. Reserve the expensive ones for the hard problems.",
        "For long documents, use a long-context model. Don't fragment what could be one call.",
    ],
    "verification": [
        "Every time the AI cites a statistic, ask 'what's the source?' before you use it.",
        "Run generated code before you ship it. 'Looks right' is not the same as 'is right'.",
        "When the AI sounds confident on something high-stakes, ask 'what would change your mind?' to surface uncertainty.",
    ],
}


def _heuristic_coaching(snapshot: ProfileSnapshot) -> Coaching:
    """Fallback when no LLM is available — uses the canned drills above."""
    sorted_dims = sorted(snapshot.dimension_means.items(), key=lambda kv: kv[1])
    weakest_two = [k for k, _v in sorted_dims[:2]]

    focus_areas: list[dict] = []
    for key in weakest_two:
        dim = by_key(key)
        focus_areas.append(
            {
                "dimension_key": key,
                "dimension_title": dim.title,
                "current_score": snapshot.dimension_means[key],
                "target_score": min(10.0, snapshot.dimension_means[key] + 2.0),
                "why_it_matters": dim.evidence,
                "drills": _FALLBACK_DRILLS.get(key, []),
            }
        )

    headline = (
        f"Overall {snapshot.overall}/10 across {snapshot.session_count} sessions. "
        f"Strongest: {by_key(snapshot.strongest_dimension).title}. "
        f"Two highest-leverage areas to lift next: "
        f"{by_key(weakest_two[0]).title} and {by_key(weakest_two[1]).title}."
    )

    daily_practice = (
        f"For the next week, before every new chat session, write one sentence "
        f"answering: 'What would make this answer great?' Treat that as your "
        f"acceptance criterion. This single habit lifts {by_key(weakest_two[0]).title.lower()}."
    )

    return Coaching(
        headline=headline,
        focus_areas=focus_areas,
        daily_practice=daily_practice,
        generated_by="heuristic",
    )


def _llm_coaching(snapshot: ProfileSnapshot) -> Coaching | None:
    """Generate personalized coaching by calling Claude (preferred) or OpenAI."""
    have_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if not (have_anthropic or have_openai):
        return None

    system_prompt = """You are an AI usage coach. You help people who work with Claude, ChatGPT, Codex, Copilot, and similar AI assistants get more value out of them.

The person has been scored on seven dimensions of AI usage. You'll see their profile — overall score, per-dimension scores, recent patterns. Your job is to give them coaching that lifts their two weakest dimensions.

# Audience

The person reading this is a working professional — usually a developer, sometimes a knowledge worker. They're using AI tools seriously. They want specific, do-it-tomorrow advice, not general prompting theory.

# What good coaching looks like

  - Two focus areas: their two weakest dimensions.
  - Three concrete drills per focus area. A drill is something they can DO in their next chat session, not something to ponder.
  - One daily practice habit: a single sentence describing a habit they can build into their routine.

Drills must pass this test: "Could I do this in the next 60 minutes?" If no, rewrite.

# Examples

For "Context richness" (weak):

  BAD:  "Provide more context in your prompts."
  GOOD: "When you ask about code, paste the function AND its immediate callers. Don't make the model guess the surrounding logic."

  BAD:  "Build a habit of richer prompting."
  GOOD: "Before your next debugging chat, paste the error message, the input that triggered it, and your expected output — in that order — before asking 'why?'"

For "Verification habits" (weak):

  BAD:  "Verify AI output."
  GOOD: "Every time the AI cites a statistic in this session, ask 'what's the source?' before you use it anywhere."

# Voice

Direct, warm, practical. Write the way a senior engineer would mentor a junior — honest, specific, encouraging without being performative. Don't moralize. Don't pad. No corporate jargon ("delve", "showcase", "leverage" as a verb). Active voice. Numbers with context.

# Edge cases

- If both weakest dimensions are very low (< 3.0), name that pattern in the headline. Don't paper over it.
- If the person's strongest dimension is something they should build on (e.g. they're already good at planning but weak at iteration), reference the strength in the headline.
- If the overall score is very high (>= 8.0), still find genuine weakest areas. Don't manufacture problems, but don't refuse to coach either. There's always a next level.

# Output

Return ONLY valid JSON, no preamble:

{
  "headline": "<2-3 sentences naming the pattern and the two focus areas>",
  "focus_areas": [
    {
      "dimension_key": "<rubric key>",
      "dimension_title": "<title>",
      "current_score": <number>,
      "target_score": <current + 1.5 to 2.5>,
      "why_it_matters": "<one sentence rooted in evidence>",
      "drills": [
        "<concrete action 1>",
        "<concrete action 2>",
        "<concrete action 3>"
      ]
    },
    { ...second focus area... }
  ],
  "daily_practice": "<one sentence, actionable, builds the habit>"
}

Exactly 2 focus areas. Exactly 3 drills per focus area."""

    profile_summary = {
        "overall": snapshot.overall,
        "session_count": snapshot.session_count,
        "providers": snapshot.provider_breakdown,
        "dimension_scores": {
            by_key(k).title: v for k, v in snapshot.dimension_means.items()
        },
        "strongest": by_key(snapshot.strongest_dimension).title,
        "weakest": by_key(snapshot.weakest_dimension).title,
        "recent_standouts": snapshot.standout_moments,
        "recent_failure_modes": snapshot.failure_modes,
    }

    user_msg = (
        "Generate the coaching for this profile.\n\n"
        f"<profile>\n{json.dumps(profile_summary, indent=2)}\n</profile>"
    )

    text: str = ""
    generated_by = ""
    try:
        if have_anthropic:
            from anthropic import Anthropic  # type: ignore

            client = Anthropic()
            response = client.messages.create(
                model="claude-opus-4-7",
                max_tokens=2000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                b.text for b in response.content if getattr(b, "type", None) == "text"
            )
            generated_by = "claude-opus-4-7"
        else:
            from openai import OpenAI  # type: ignore

            client = OpenAI()  # type: ignore[assignment]
            response = client.chat.completions.create(  # type: ignore[attr-defined]
                model="gpt-5",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
            )
            text = response.choices[0].message.content or ""  # type: ignore[attr-defined]
            generated_by = "gpt-5"
    except Exception as exc:  # noqa: BLE001
        print(f"[coach] LLM call failed: {exc!r}", file=sys.stderr)
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
        return Coaching(
            headline=payload.get("headline", ""),
            focus_areas=list(payload.get("focus_areas", [])),
            daily_practice=payload.get("daily_practice", ""),
            generated_by=generated_by,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[coach] failed to parse LLM coaching response: {exc!r}", file=sys.stderr)
        return None


def generate_coaching(snapshot: ProfileSnapshot) -> Coaching:
    """Top-level entry. Tries LLM first, falls back to heuristic."""
    if snapshot.session_count == 0:
        return Coaching(
            headline="No sessions scanned yet. Run `praxis scan` after using Claude, Codex, or Copilot for a day or two.",
            focus_areas=[],
            daily_practice="",
            generated_by="empty",
        )

    llm_result = _llm_coaching(snapshot)
    if llm_result is not None and llm_result.focus_areas:
        return llm_result
    return _heuristic_coaching(snapshot)
