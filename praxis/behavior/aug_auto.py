"""Augmentation-vs-automation session classifier.

Reads a session transcript and decides whether the user was AUGMENTING
their own work (driving the thinking, asking the model to fill in parts)
or AUTOMATING it (handing the task to the model and accepting output
without much engagement). 'mixed' covers sessions that legitimately blend
both modes.

Provider selection mirrors models_advisor / judge: at call time we check
the environment - Haiku 4.5 if ANTHROPIC_API_KEY is set, else gpt-5-mini.
The check happens inside ``classify_session`` so tests can flip the env
between calls without re-importing.

The module raises ``AugAutoParseError`` for malformed JSON or for any
classification value outside the three accepted literals. Callers must
handle it (the orchestrator wraps the call so a parse failure writes
NULL for the aug_auto columns rather than aborting the run).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal, cast

AugAutoClassification = Literal["augmentation", "automation", "mixed"]

_VALID_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"augmentation", "automation", "mixed"}
)

CLAUDE_CLASSIFIER_MODEL = "claude-haiku-4-5"
OPENAI_CLASSIFIER_MODEL = "gpt-5-mini"

# Cap the transcript we send to the classifier. Long enough to ground the
# augmentation-vs-automation judgment; short enough to keep the cheap-tier
# call genuinely cheap.
MAX_TRANSCRIPT_CHARS = 8_000


class AugAutoError(RuntimeError):
    """Base error for the aug_auto classifier."""


class AugAutoParseError(AugAutoError):
    """Raised when the classifier's JSON cannot be parsed or violates the schema.

    Triggers:
      - response is not valid JSON (after fence stripping)
      - response is missing a required field (classification / confidence / rationale)
      - ``classification`` is not one of {'augmentation', 'automation', 'mixed'}
      - ``confidence`` is not a number in [0.0, 1.0]
    """


class AugAutoUnavailableError(AugAutoError):
    """Raised when no provider API key is configured at call time.

    Distinct from AugAutoParseError so the orchestrator can log
    "classifier unavailable" once per run instead of treating it like a
    parse failure on every session.
    """


@dataclass(frozen=True)
class AugAutoResult:
    """One classifier call's output."""

    classification: AugAutoClassification
    confidence: float
    rationale: str


_SYSTEM_PROMPT = """You read one chat session between a person and an AI coding assistant, and decide whether the person was AUGMENTING their own work or AUTOMATING it.

# Definitions

- **augmentation**: the person is the primary thinker. They bring the goal, the context, and the constraints. They use the model to fill in specific gaps (look something up, draft a function from a spec, propose alternatives), then read, question, and adapt the output. The work would not exist without their judgment.

- **automation**: the person hands the task to the model and accepts the result with little engagement. Prompts are short, context is thin, follow-ups are 'ok', 'thanks', 'do it', or absent. The model's output IS the work; the person is a dispatcher.

- **mixed**: parts of the session look like augmentation, parts look like automation. Use this when the session genuinely blends both modes; do not use it as a hedge when you actually have a clear read.

# How to decide

Look at the user turns specifically:
  - Goal framing: did the user explain WHAT and WHY, or just paste a task?
  - Context: did they include files, constraints, prior decisions, or skip them?
  - Iteration: did they push back, ask follow-up questions, or accept v1?
  - Verification: did they read the output (e.g. asked about a detail, caught a bug), or take it on faith?

Short transcripts are not automatically 'automation'. A two-turn session where the user gave a tight, well-scoped prompt and got exactly what they needed can still be augmentation.

# Confidence

Return a `confidence` float in [0.0, 1.0]:
  - 0.8-1.0: clear signal across multiple turns, hard to dispute the read.
  - 0.5-0.8: typical case - some signal, some ambiguity.
  - 0.0-0.5: the transcript was very short, atypical, or contradictory.

# Output

Return ONLY valid JSON, no preamble, no markdown fences, in exactly this shape:

{
  "classification": "<augmentation|automation|mixed>",
  "confidence": <float 0.0-1.0>,
  "rationale": "<one or two sentences pointing at specific things in the transcript>"
}
"""


def _compact(transcript_text: str) -> str:
    """Trim transcript to MAX_TRANSCRIPT_CHARS, keeping head + tail.

    Mid-session reasoning is usually the most repetitive part; the open
    (goal framing) and close (final exchange + verification) carry more
    augmentation-vs-automation signal, so we preserve both ends when
    truncating.
    """
    if len(transcript_text) <= MAX_TRANSCRIPT_CHARS:
        return transcript_text
    half = (MAX_TRANSCRIPT_CHARS - 24) // 2
    return (
        transcript_text[:half]
        + "\n...[transcript truncated]...\n"
        + transcript_text[-half:]
    )


def _strip_fences(text: str) -> str:
    """Strip optional ```json fences and locate the outer JSON object."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    return cleaned


def _parse_response(text: str) -> AugAutoResult:
    """Parse the LLM JSON into an AugAutoResult or raise AugAutoParseError."""
    cleaned = _strip_fences(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise AugAutoParseError(
            f"no JSON object in classifier response: {text[:200]!r}"
        )
    try:
        payload = json.loads(cleaned[start : end + 1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise AugAutoParseError(
            f"classifier response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AugAutoParseError(
            f"classifier response is not a JSON object: {type(payload).__name__}"
        )

    classification = payload.get("classification")
    if not isinstance(classification, str) or classification not in _VALID_CLASSIFICATIONS:
        raise AugAutoParseError(
            f"invalid classification {classification!r}: "
            f"expected one of {sorted(_VALID_CLASSIFICATIONS)}"
        )

    raw_confidence = payload.get("confidence")
    try:
        confidence = float(raw_confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AugAutoParseError(
            f"confidence {raw_confidence!r} is not a number"
        ) from exc
    if not 0.0 <= confidence <= 1.0:
        raise AugAutoParseError(
            f"confidence {confidence} is outside [0.0, 1.0]"
        )

    rationale = payload.get("rationale", "")
    if not isinstance(rationale, str):
        raise AugAutoParseError(
            f"rationale must be a string, got {type(rationale).__name__}"
        )

    return AugAutoResult(
        classification=cast(AugAutoClassification, classification),
        confidence=confidence,
        rationale=rationale,
    )


def _classify_with_anthropic(transcript_text: str) -> str:
    """One Anthropic call. Returns the raw text body for _parse_response."""
    from anthropic import Anthropic  # type: ignore

    client = Anthropic()
    response = client.messages.create(
        model=CLAUDE_CLASSIFIER_MODEL,
        max_tokens=600,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify this session.\n\n"
                    f"<transcript>\n{_compact(transcript_text)}\n</transcript>"
                ),
            }
        ],
    )
    return "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    )


def _classify_with_openai(transcript_text: str) -> str:
    """One OpenAI call. Returns the raw text body for _parse_response."""
    from openai import OpenAI  # type: ignore

    client = OpenAI()
    response = client.chat.completions.create(
        model=OPENAI_CLASSIFIER_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Classify this session.\n\n"
                    f"<transcript>\n{_compact(transcript_text)}\n</transcript>"
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


def classify_session(transcript_text: str) -> AugAutoResult:
    """Classify one session transcript as augmentation, automation, or mixed.

    Provider selection happens at call time, not at import time, so tests
    can monkeypatch the env between calls. ANTHROPIC_API_KEY wins; falls
    back to OPENAI_API_KEY. With neither set, raises
    ``AugAutoUnavailableError`` so the orchestrator can log a single
    "classifier unavailable" message per run.

    Raises:
        AugAutoParseError: the LLM returned non-JSON, a classification
            outside {'augmentation', 'automation', 'mixed'}, or a
            confidence that is not a float in [0.0, 1.0].
        AugAutoUnavailableError: neither ANTHROPIC_API_KEY nor
            OPENAI_API_KEY is set at call time.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        raw = _classify_with_anthropic(transcript_text)
    elif os.environ.get("OPENAI_API_KEY"):
        raw = _classify_with_openai(transcript_text)
    else:
        raise AugAutoUnavailableError(
            "no API key configured for aug_auto classifier "
            "(set ANTHROPIC_API_KEY or OPENAI_API_KEY)"
        )
    return _parse_response(raw)


__all__ = [
    "AugAutoClassification",
    "AugAutoError",
    "AugAutoParseError",
    "AugAutoResult",
    "AugAutoUnavailableError",
    "CLAUDE_CLASSIFIER_MODEL",
    "OPENAI_CLASSIFIER_MODEL",
    "classify_session",
]
