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
import re
import sys
from dataclasses import dataclass, field
from typing import Any, cast

from praxis.models import Moment, Session, Severity
from praxis.scoring.rubric import RUBRIC


_VALID_SEVERITIES: frozenset[str] = frozenset({"minor", "moderate", "major"})
_VALID_DIM_KEYS: frozenset[str] = frozenset(d.key for d in RUBRIC)
_EXCERPT_MAX = 240
_WHY_MAX = 180
_ALT_MAX = 220
_WHITESPACE_RE = re.compile(r"\s+")


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
    moments: list[Moment] = field(default_factory=list)


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

# Moments

Alongside the dim scores, return a `moments` array: structured pointers to specific transcript spans where one rubric dimension dropped, with a concrete suggested alternative.

Rules:

- Emit AT MOST ONE moment per `dim_key` per session. Many sessions will have zero moments. That is fine.
- Only emit a moment when you actually saw a specific coachable lapse in the transcript. The judge decides this, not a score threshold. Do not emit a moment for a dim where you have nothing specific to coach on. A score of 5 with no specific lapse is not a moment; a score of 7 with one clearly avoidable mistake is. Use your judgment.
- If the session contains no specific coachable lapse, return an empty `moments` array (`"moments": []`).

Each moment object has six required fields:

- `dim_key`: one of {{planning, context, iteration, tools, fit, verification}}.
- `turn_index`: integer, the 0-indexed user turn where the lapse occurred.
- `quoted_excerpt`: <= 240 chars, copied VERBATIM from the transcript (usually the user's own words; may be the assistant's words if that is what shows the missed verification). Substring-faithfulness is non-negotiable; do not paraphrase here.
- `why_it_lost_score`: <= 180 chars, one specific sentence naming what was missing or wrong.
- `suggested_alternative`: <= 220 chars, what to do next time. Concrete enough to act on.
- `severity`: one of {{minor, moderate, major}}.

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
  "moments": [
    {{
      "dim_key": "<one of planning|context|iteration|tools|fit|verification>",
      "turn_index": <int, 0-indexed user turn>,
      "quoted_excerpt": "<verbatim substring from the transcript, <= 240 chars>",
      "why_it_lost_score": "<one specific sentence, <= 180 chars>",
      "suggested_alternative": "<concrete next-time action, <= 220 chars>",
      "severity": "<minor|moderate|major>"
    }}
  ],
  "overall_note": "<2-3 sentences capturing the pattern>"
}}

standout_moments and failure_modes should each have 1-3 entries. The moments array is empty if you saw no specific coachable lapse; otherwise it has at most one entry per dim_key."""


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
        moments=_parse_moments(payload.get("moments", []) or []),
    )


def _parse_moments(raw: Any) -> list[Moment]:
    """Build Moment objects from the judge's `moments` JSON array.

    Drops moments with missing or malformed required fields, unknown
    dim_key, invalid severity, or quoted_excerpt longer than 240 chars
    (truncating an excerpt would break the substring check in US-018).
    why_it_lost_score and suggested_alternative are length-capped by
    truncation since they are free-text explanations, not anchors.

    Caps to one moment per dim_key (spec §4.2). Later entries for the
    same dim_key are dropped with a log line.
    """
    if not isinstance(raw, list):
        return []
    out: list[Moment] = []
    seen_dims: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            print("[scorer] moment dropped: not a JSON object", file=sys.stderr)
            continue
        dim_key = item.get("dim_key")
        if not isinstance(dim_key, str) or dim_key not in _VALID_DIM_KEYS:
            print(f"[scorer] moment dropped: invalid dim_key {dim_key!r}", file=sys.stderr)
            continue
        if dim_key in seen_dims:
            print(
                f"[scorer] moment dropped: duplicate dim_key {dim_key!r} in same session",
                file=sys.stderr,
            )
            continue
        turn_index = item.get("turn_index")
        if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 0:
            print(f"[scorer] moment dropped: invalid turn_index {turn_index!r}", file=sys.stderr)
            continue
        excerpt = item.get("quoted_excerpt")
        if not isinstance(excerpt, str) or not excerpt:
            print("[scorer] moment dropped: missing quoted_excerpt", file=sys.stderr)
            continue
        if len(excerpt) > _EXCERPT_MAX:
            print(
                f"[scorer] moment dropped: quoted_excerpt exceeds {_EXCERPT_MAX} chars",
                file=sys.stderr,
            )
            continue
        why = item.get("why_it_lost_score")
        if not isinstance(why, str) or not why:
            print("[scorer] moment dropped: missing why_it_lost_score", file=sys.stderr)
            continue
        alt = item.get("suggested_alternative")
        if not isinstance(alt, str) or not alt:
            print("[scorer] moment dropped: missing suggested_alternative", file=sys.stderr)
            continue
        severity = item.get("severity")
        if not isinstance(severity, str) or severity not in _VALID_SEVERITIES:
            print(f"[scorer] moment dropped: invalid severity {severity!r}", file=sys.stderr)
            continue
        out.append(
            Moment(
                dim_key=dim_key,
                turn_index=turn_index,
                quoted_excerpt=excerpt,
                why_it_lost_score=why[:_WHY_MAX],
                suggested_alternative=alt[:_ALT_MAX],
                severity=cast(Severity, severity),
            )
        )
        seen_dims.add(dim_key)
    return out


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace to a single space and strip ends.

    Spec §4.4 mandates whitespace-normalized substring verification: a
    quoted_excerpt from the judge may differ from the transcript only in
    its whitespace shape (newlines, tabs, runs of spaces), nothing else.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def _session_corpus(session: Session) -> str:
    """Full-text corpus used to verify moment excerpts.

    Joins every turn's content with a single space. The judge sees the
    *compacted* transcript (per spec §9.2), but verification runs against
    the full text stored locally - the judge is not allowed to quote
    spans it never saw. Tool-call metadata is deliberately excluded:
    the judge prompt directs the model to quote rendered text only.
    """
    return " ".join(turn.content for turn in session.turns)


def verify_moment_substrings(session: Session, moments: list[Moment]) -> list[Moment]:
    """Drop moments whose quoted_excerpt is not a substring of the transcript.

    Per spec §4.4: the whitespace-normalized excerpt MUST be a substring
    of the whitespace-normalized session transcript. On failure, emit a
    `[scorer] moment failed substring check` log line and discard the
    moment. There is no fuzzy or approximate match fallback - a wrong
    quote is worse than no quote.
    """
    transcript = _normalize_whitespace(_session_corpus(session))
    survivors: list[Moment] = []
    for m in moments:
        excerpt = _normalize_whitespace(m.quoted_excerpt)
        if excerpt and excerpt in transcript:
            survivors.append(m)
            continue
        print(
            f"[scorer] moment failed substring check "
            f"(dim={m.dim_key}, turn={m.turn_index})",
            file=sys.stderr,
        )
    return survivors


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
    result = _parse_response(text, model)
    result.moments = verify_moment_substrings(session, result.moments)
    return result


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
    result = _parse_response(text, model)
    result.moments = verify_moment_substrings(session, result.moments)
    return result


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
