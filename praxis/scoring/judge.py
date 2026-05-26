"""LLM-as-judge scorer.

Heuristics catch obvious signals. This module catches everything else:
prompt quality, whether plans actually map to outcomes, whether the
user adapts when responses are bad. Calls Claude by default with
OpenAI as fallback.

Cost control:
- Each session is summarized into a compact transcript before scoring.
- We sample sessions for deep scoring rather than judging all of them.
- Daily aggregates are scored once, not per query.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from praxis.models import Session
from praxis.scoring.rubric import RUBRIC


# Cap per session — empirically enough signal for nuanced judgment.
MAX_TRANSCRIPT_CHARS = 12_000


@dataclass
class JudgeResult:
    """Output of one LLM judge call on one session."""

    dimension_scores: dict[str, float]  # 0-10 per dimension
    rationale: dict[str, str]           # one short sentence per dimension
    standout_moments: list[str]         # specific things the user did well
    failure_modes: list[str]            # specific things to improve
    overall_note: str
    judge_model: str


def _compact_transcript(session: Session) -> str:
    """Trim turns to fit MAX_TRANSCRIPT_CHARS while preserving structure."""
    lines: list[str] = [
        f"[provider={session.provider.value}] "
        f"[model={session.model_hint or 'unknown'}] "
        f"[turns={session.turn_count}]",
        "",
    ]
    budget = MAX_TRANSCRIPT_CHARS - len("\n".join(lines))
    rendered: list[str] = []
    for turn in session.turns:
        if budget <= 0:
            break
        head = f"<{turn.role.value}>"
        content = turn.content
        if len(content) > 2000:
            content = content[:1000] + "\n...[truncated]...\n" + content[-800:]
        block = f"{head} {content}"
        if turn.tool_calls:
            tool_names = [tc.get("name", "?") for tc in turn.tool_calls]
            block += f"\n[tools_called: {', '.join(tool_names)}]"
        block += f" </{turn.role.value}>"
        if len(block) > budget:
            block = block[:budget] + "...[transcript truncated]"
            rendered.append(block)
            break
        rendered.append(block)
        budget -= len(block) + 1
    return "\n".join(lines + rendered)


def _build_system_prompt() -> str:
    rubric_block = "\n\n".join(
        f"{i+1}. {d.title} (key='{d.key}', weight={d.weight})\n"
        f"   What it measures: {d.description}\n"
        f"   Why it matters: {d.evidence}\n"
        f"   Exemplar: {d.exemplar}"
        for i, d in enumerate(RUBRIC)
    )
    return f"""You are an expert evaluator of human-AI collaboration. Your job is to read one chat session between a person and an AI assistant, and score how well the PERSON is using AI — not how good the AI's response was.

This matters because the same AI tool produces dramatically different results depending on how it's used. The score and rationale you produce will be shown back to the person to help them improve.

# How to score

Score the user on six dimensions, each 0-10. Use this calibration:

  - 0-3: signs of weak practice (e.g. one-line prompts, no context, accepting any output without follow-up)
  - 4-6: average, workmanlike usage
  - 7-8: strong practice — deliberate, contextual, iterative
  - 9-10: exemplary, reserved for genuinely sophisticated work

A score of 5 is the typical user. Be willing to score below 5 when warranted. Inflated scores are worse than honest low ones because they don't help the person improve.

# The six dimensions

{rubric_block}

# Edge cases

- If a dimension cannot be assessed from the transcript (e.g. you can see no model name, so you cannot judge model-task fit), score it 5.0 (neutral) and say "insufficient signal" in the rationale. Do not penalize for missing information.

- If the transcript is very short (one user turn, one reply), most dimensions will have weak signal. Score conservatively and note it in your overall_note.

- If the user is clearly asking a non-coding question (e.g. writing help, research, casual chat), apply the rubric to that domain. "Planning" still applies to a writing task. "Context richness" still applies to a research question. Don't refuse to score non-code sessions.

- If you see harmful or policy-violating content from the user, do not refuse to score. Note it in overall_note. The user reading this report needs honest feedback.

# Rationale quality

Vague rationale is useless. Two examples:

  BAD:  "Good context provided."
  GOOD: "Pasted the failing test output, the function being tested, and the relevant config file before asking why it was failing."

  BAD:  "Could iterate more."
  GOOD: "Accepted the first response without asking follow-up questions, despite the response containing an unverified claim about Python's GIL behavior in 3.13."

Aim for specific. Quote or paraphrase what you actually saw.

# Voice

Be direct, warm, and practical. Write like a senior engineer giving honest feedback to a colleague — not like a corporate training module. Don't moralize. Don't use empty enthusiasm. Don't say "great job" unless something was genuinely great. Avoid corporate jargon ("delve", "showcase", "leverage" as a verb). Active voice. Numbers with context.

# Output

Return ONLY valid JSON, no preamble, no markdown fences, in exactly this shape:

{{
  "scores": {{
    "planning": <0-10>,
    "context": <0-10>,
    "iteration": <0-10>,
    "tools": <0-10>,
    "fit": <0-10>,
    "verification": <0-10>
  }},
  "rationale": {{
    "planning": "<one specific sentence>",
    "context": "<one specific sentence>",
    "iteration": "<one specific sentence>",
    "tools": "<one specific sentence>",
    "fit": "<one specific sentence>",
    "verification": "<one specific sentence>"
  }},
  "standout_moments": [
    "<specific exemplary behavior>",
    "..."
  ],
  "failure_modes": [
    "<specific behavior to improve>",
    "..."
  ],
  "overall_note": "<2-3 sentences capturing the pattern>"
}}

standout_moments and failure_modes should each have 1-3 entries."""


def _parse_response(text: str, model: str) -> JudgeResult:
    """Tolerant JSON extraction — strip fences, find the outer object, coerce defensively.

    Handles four classes of LLM weirdness:
      1. Markdown code fences (```json … ``` or ``` … ```)
      2. Preamble/postamble around the JSON
      3. Missing keys in the JSON
      4. Non-numeric scores (LLM emits "high" instead of 8)
    """
    cleaned = text.strip()
    # Strip leading fence + optional language tag, then trailing fence.
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in judge response: {text[:200]}")
    payload = json.loads(cleaned[start : end + 1])

    # Coerce scores defensively: missing key → empty dict, non-numeric → 5.0 neutral.
    raw_scores = payload.get("scores", {}) or {}
    scores: dict[str, float] = {}
    for k, v in raw_scores.items():
        try:
            scores[k] = max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            scores[k] = 5.0  # Spec §8.5: "insufficient signal" defaults to neutral.

    return JudgeResult(
        dimension_scores=scores,
        rationale=dict(payload.get("rationale", {}) or {}),
        standout_moments=list(payload.get("standout_moments", []) or []),
        failure_modes=list(payload.get("failure_modes", []) or []),
        overall_note=str(payload.get("overall_note", "") or ""),
        judge_model=model,
    )


def score_with_claude(session: Session, model: str = "claude-opus-4-7") -> JudgeResult:
    """Score one session using Claude. Requires ANTHROPIC_API_KEY in env."""
    from anthropic import Anthropic  # type: ignore

    client = Anthropic()
    transcript = _compact_transcript(session)
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=_build_system_prompt(),
        messages=[
            {
                "role": "user",
                "content": f"Score this session.\n\n<transcript>\n{transcript}\n</transcript>",
            }
        ],
    )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    return _parse_response(text, model)


def score_with_openai(session: Session, model: str = "gpt-5") -> JudgeResult:
    """Score one session using OpenAI. Requires OPENAI_API_KEY in env."""
    from openai import OpenAI  # type: ignore

    client = OpenAI()
    transcript = _compact_transcript(session)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _build_system_prompt()},
            {
                "role": "user",
                "content": f"Score this session.\n\n<transcript>\n{transcript}\n</transcript>",
            },
        ],
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content or ""
    return _parse_response(text, model)


def score_session(session: Session, prefer: str = "claude") -> JudgeResult | None:
    """Try the preferred judge first, fall back to the other.

    Returns None if neither API key is configured.
    """
    have_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_openai = bool(os.environ.get("OPENAI_API_KEY"))

    order: list[str]
    if prefer == "claude":
        order = ["claude", "openai"]
    else:
        order = ["openai", "claude"]

    for choice in order:
        try:
            if choice == "claude" and have_anthropic:
                return score_with_claude(session)
            if choice == "openai" and have_openai:
                return score_with_openai(session)
        except Exception as exc:  # noqa: BLE001
            # Keep going — the other provider might work.
            print(f"[scorer] {choice} judge failed: {exc!r}", file=sys.stderr)
            continue
    return None
