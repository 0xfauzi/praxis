"""LLM-driven task clustering.

Spec section 5: once per weekly digest, all sessions in the window are
sent in a single LLM call to the cheap-tier model from the user's
primary provider. The call groups sessions into tasks - clusters that
share a single underlying goal, even if they span repos or days.

Each session block sent to the model contains exactly four fields:
session_id, first_user_turn (truncated to 400 chars), project_hint,
and started_at. Project hint and timestamps are context, not the
grouping rule (spec 5.1).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

from praxis.models import Session


# Spec 5.2: "Truncate each first-user-turn to 400 chars before sending."
FIRST_TURN_MAX_CHARS = 400

# Cheap-tier model from each supported provider. Spec 5.2:
# "The cheap-tier model from the user's primary provider
# (Haiku if Anthropic, gpt-5-mini if OpenAI)."
ANTHROPIC_CHEAP_MODEL = "claude-haiku-4-5"
OPENAI_CHEAP_MODEL = "gpt-5-mini"


# Spec 5.3 / spec 14 schema: label_source distinguishes LLM-produced tasks
# ("llm") from singleton-fallback tasks ("fallback") created when the model's
# clustering response fails coverage validation twice in a row.
LABEL_SOURCE_LLM = "llm"
LABEL_SOURCE_FALLBACK = "fallback"

# Spec 5.3: "fall back to one task per session, deterministically labeled from
# the first 5 words of the session's first user turn."
SINGLETON_FALLBACK_LABEL_WORDS = 5


# Spec 5.3 label / task_type validation.
# "Labels reject as invalid if they exceed 60 chars or contain the literal
# strings 'I', 'you', 'the user', 'the assistant'. task_type must be one of
# the eight enumerated values."
LABEL_MAX_CHARS = 60

LABEL_FORBIDDEN_TOKENS: tuple[str, ...] = ("I", "you", "the user", "the assistant")

ALLOWED_TASK_TYPES: tuple[str, ...] = (
    "debugging",
    "refactoring",
    "building_new",
    "planning",
    "learning",
    "research",
    "ops",
    "other",
)

# Word-boundary, case-insensitive match for the forbidden tokens. Boundaries
# ensure "Iteration" does not trigger "I" and "your" does not trigger "you";
# case-insensitivity catches "You should fix X" alongside "you should fix X".
_LABEL_FORBIDDEN_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in LABEL_FORBIDDEN_TOKENS) + r")\b",
    re.IGNORECASE,
)


# Spec 5.6 shape thresholds. Both checks use strict inequality (N > threshold).
# - Anti-collapse: if the response collapses everything into one task while
#   N > ANTI_COLLAPSE_THRESHOLD sessions exist, ask the model to split.
# - Anti-singleton: if every task is a singleton while
#   N > ANTI_SINGLETON_THRESHOLD sessions exist, ask the model to merge.
# After the single shape retry the response is accepted regardless of shape
# (spec 5.6 "Accept after retry").
ANTI_COLLAPSE_THRESHOLD = 6
ANTI_SINGLETON_THRESHOLD = 8


@dataclass
class Task:
    """One task cluster returned by the LLM (or built by singleton fallback)."""

    label: str
    task_type: str
    session_ids: list[str]
    rationale: str
    label_source: str = LABEL_SOURCE_LLM


def _first_user_turn_truncated(session: Session) -> str:
    """Return the first user turn's content, truncated to FIRST_TURN_MAX_CHARS."""
    user_turns = session.user_turns
    if not user_turns:
        return ""
    content = user_turns[0].content
    return content[:FIRST_TURN_MAX_CHARS]


def build_session_blocks(sessions: list[Session]) -> list[dict[str, str]]:
    """Build the per-session input blocks sent to the clustering LLM.

    Each block is exactly the four fields the spec requires:
      - session_id (the stable hash id, since the provider-native id can
        collide across providers; the spec just says "session_id")
      - first_user_turn (truncated to FIRST_TURN_MAX_CHARS)
      - project_hint (filesystem path or the literal string "none")
      - started_at (ISO timestamp)
    """
    return [
        {
            "session_id": s.stable_id,
            "first_user_turn": _first_user_turn_truncated(s),
            "project_hint": s.project_hint or "none",
            "started_at": s.started_at.isoformat(),
        }
        for s in sessions
    ]


_CLUSTERING_INSTRUCTIONS = """You are reading short summaries of N AI-coding sessions from one person's week. Group them into "tasks" - clusters of sessions that share a single underlying goal, even if they span repos or days. Two sessions belong together if a knowledgeable colleague would describe them as part of the same piece of work. Two sessions are separate tasks if the colleague would describe them as different work, even in the same repo within an hour.

For each session you will see:
  - session_id
  - first user turn (truncated to 400 chars)
  - project_hint (filesystem path or "none")
  - started_at (ISO timestamp)

Return JSON in this exact shape:
{
  "tasks": [
    {
      "label": "<3-7 words, e.g. 'auth migration debugging' or 'deckgen UI polish'>",
      "task_type": "<one of: debugging, refactoring, building_new, planning, learning, research, ops, other>",
      "session_ids": ["<id>", "<id>", ...],
      "rationale": "<one sentence saying why these belong together>"
    }
  ]
}

Every session_id from the input MUST appear in exactly one task. A single-session task is fine."""


def build_prompt(sessions: list[Session]) -> str:
    """Build the user-message prompt for one clustering call.

    All N sessions are sent in one call (spec 5.2).
    """
    blocks = build_session_blocks(sessions)
    return (
        _CLUSTERING_INSTRUCTIONS
        + "\n\n<sessions>\n"
        + json.dumps(blocks, indent=2)
        + "\n</sessions>"
    )


def _parse_response(text: str) -> list[Task]:
    """Tolerant JSON extraction. Same fence-stripping pattern as judge.py."""
    cleaned = text.strip()
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
        raise ValueError(f"No JSON object found in clustering response: {text[:200]}")
    payload = json.loads(cleaned[start : end + 1])
    tasks_raw = payload.get("tasks", []) or []
    tasks: list[Task] = []
    for t in tasks_raw:
        tasks.append(
            Task(
                label=str(t.get("label", "")),
                task_type=str(t.get("task_type", "other")),
                session_ids=[str(sid) for sid in (t.get("session_ids", []) or [])],
                rationale=str(t.get("rationale", "")),
            )
        )
    return tasks


def _validate_coverage(tasks: list[Task], expected_ids: set[str]) -> str | None:
    """Spec 5.3: every input session_id must appear in exactly one task.

    Returns None on success. Otherwise returns a human-readable error string
    naming the missing, duplicated, and/or invented (spec 5.6) session_ids,
    which is appended to the re-prompt so the model knows what to fix.
    """
    all_ids: list[str] = []
    for t in tasks:
        all_ids.extend(t.session_ids)

    seen: set[str] = set()
    duplicates: list[str] = []
    for sid in all_ids:
        if sid in seen:
            if sid not in duplicates:
                duplicates.append(sid)
        else:
            seen.add(sid)

    invented = sorted(sid for sid in seen if sid not in expected_ids)
    missing = sorted(sid for sid in expected_ids if sid not in seen)

    errors: list[str] = []
    if missing:
        errors.append(f"missing session_ids: {missing}")
    if duplicates:
        errors.append(f"duplicated session_ids: {sorted(duplicates)}")
    if invented:
        errors.append(f"invented session_ids not in input: {invented}")

    if errors:
        return "; ".join(errors)
    return None


def _validate_labels_and_types(tasks: list[Task]) -> str | None:
    """Spec 5.3: labels must be <= LABEL_MAX_CHARS and must not contain (with
    word-boundary, case-insensitive matching) any of LABEL_FORBIDDEN_TOKENS.
    task_type must be one of ALLOWED_TASK_TYPES.

    Returns None on success. Otherwise returns a human-readable error string
    naming the offending labels and types, which is appended to the re-prompt
    so the model knows what to fix.

    Word-boundary matching means "Iteration" does not trigger "I", "your" does
    not trigger "you", and "the users" does not trigger "the user". The spec's
    forbidden tokens are intended as the persona pronouns / referents, not as
    substrings buried inside longer words.
    """
    long_labels: list[str] = []
    persona_labels: list[str] = []
    invalid_types: list[str] = []

    for t in tasks:
        if len(t.label) > LABEL_MAX_CHARS:
            long_labels.append(t.label)
        if _LABEL_FORBIDDEN_PATTERN.search(t.label):
            persona_labels.append(t.label)
        if t.task_type not in ALLOWED_TASK_TYPES:
            invalid_types.append(t.task_type)

    errors: list[str] = []
    if long_labels:
        errors.append(f"labels exceed {LABEL_MAX_CHARS} chars: {long_labels}")
    if persona_labels:
        errors.append(
            f"labels contain forbidden tokens "
            f"({list(LABEL_FORBIDDEN_TOKENS)}): {persona_labels}"
        )
    if invalid_types:
        errors.append(
            f"task_type values not in {list(ALLOWED_TASK_TYPES)}: {invalid_types}"
        )

    if errors:
        return "; ".join(errors)
    return None


def _validate_shape(tasks: list[Task], n_sessions: int) -> str | None:
    """Spec 5.6: anti-collapse and anti-singleton shape checks.

    Anti-collapse: exactly one task with N > ANTI_COLLAPSE_THRESHOLD sessions
    suggests the LLM lumped distinct work together to be safe; ask it to split
    when the work is genuinely distinct.

    Anti-singleton: every task is a singleton AND N > ANTI_SINGLETON_THRESHOLD
    suggests the LLM split everything to be safe; ask it to merge sessions
    that share an underlying goal.

    Returns None on success. Otherwise returns a human-readable error string
    appended to the original prompt by the re-prompt path.

    Intended to run AFTER coverage validation passes - the "every task has
    exactly one session" check would be misleading if some input session_ids
    were missing from the response.
    """
    if not tasks:
        return None

    if len(tasks) == 1 and n_sessions > ANTI_COLLAPSE_THRESHOLD:
        return (
            f"shape: all {n_sessions} sessions were collapsed into a single task; "
            f"please split when the work is genuinely distinct."
        )

    if n_sessions > ANTI_SINGLETON_THRESHOLD and all(
        len(t.session_ids) == 1 for t in tasks
    ):
        return (
            f"shape: every one of {n_sessions} sessions was given its own singleton "
            f"task; please merge sessions that share an underlying goal."
        )

    return None


def _build_retry_message(original_prompt: str, validation_error: str) -> str:
    """Spec 5.3: re-prompt once with the validation error appended."""
    return (
        f"{original_prompt}\n\n"
        f"Your previous response failed validation: {validation_error}\n"
        f"Please return a corrected JSON response in the same shape, using only "
        f"the session_ids from the input above, each appearing in exactly one task."
    )


def _singleton_fallback_label(session: Session) -> str:
    """First N words of `session`'s first user turn, where N is
    SINGLETON_FALLBACK_LABEL_WORDS. Uses the raw first user turn, not the
    400-char-truncated send-form, so the label is the speaker's actual words.
    Returns "" if the session has no user turn (defensive; the parser already
    filters non-USER turns when building sessions).
    """
    user_turns = session.user_turns
    if not user_turns:
        return ""
    return " ".join(user_turns[0].content.split()[:SINGLETON_FALLBACK_LABEL_WORDS])


def _singleton_fallback(sessions: list[Session]) -> list[Task]:
    """Spec 5.3: when both LLM attempts fail coverage validation, return one
    task per session with a deterministic label from the first 5 words of that
    session's first user turn. Tasks carry label_source='fallback' so the
    persistence layer (spec section 14 tasks.label_source) can distinguish
    them from LLM-labeled tasks.

    This is graceful degradation for LLM protocol failure, not a heuristic
    substitute for clustering.
    """
    return [
        Task(
            label=_singleton_fallback_label(s),
            task_type="other",
            session_ids=[s.stable_id],
            rationale="",
            label_source=LABEL_SOURCE_FALLBACK,
        )
        for s in sessions
    ]


def _run_clustering(
    sessions: list[Session],
    call_fn: Callable[[str], list[Task]],
) -> list[Task]:
    """Shared retry pipeline used by both Anthropic and OpenAI clustering paths.

    Two retry gates, each with its own single-retry budget:

    1. Correctness gate (spec 5.3): coverage + label/type validation. On first
       failure, re-prompt once with the validation error. If the retry also
       fails (validation or unparseable), fall back to one task per session
       (US-033 / US-034 / US-035).

    2. Shape gate (spec 5.6): anti-collapse + anti-singleton. On failure,
       re-prompt once with the shape error. Per US-036 AC #3 the shape retry
       is accepted regardless of shape; the retry response is gated only on
       coverage + label/type so a reshape that breaks correctness falls back
       to the original (known-correct, suboptimally-shaped) tasks rather than
       being accepted blindly.

    Worst case: 3 LLM calls (original + correctness retry + shape retry). Spec
    5.4's <$0.10/week budget on cheap-tier models accommodates this comfortably.
    """
    prompt = build_prompt(sessions)
    expected_ids = {s.stable_id for s in sessions}

    tasks = call_fn(prompt)

    correctness_error = (
        _validate_coverage(tasks, expected_ids)
        or _validate_labels_and_types(tasks)
    )
    if correctness_error is not None:
        try:
            retried = call_fn(_build_retry_message(prompt, correctness_error))
        except ValueError:
            return _singleton_fallback(sessions)
        retry_error = (
            _validate_coverage(retried, expected_ids)
            or _validate_labels_and_types(retried)
        )
        if retry_error is not None:
            return _singleton_fallback(sessions)
        tasks = retried

    shape_error = _validate_shape(tasks, len(sessions))
    if shape_error is None:
        return tasks
    try:
        reshaped = call_fn(_build_retry_message(prompt, shape_error))
    except ValueError:
        return tasks
    if (
        _validate_coverage(reshaped, expected_ids) is None
        and _validate_labels_and_types(reshaped) is None
    ):
        return reshaped
    return tasks


def cluster_with_anthropic(
    sessions: list[Session], model: str = ANTHROPIC_CHEAP_MODEL
) -> list[Task]:
    """One clustering call against the Anthropic cheap-tier model, with the
    shared correctness + shape retry pipeline (see _run_clustering)."""
    from anthropic import Anthropic  # type: ignore

    client = Anthropic()

    def _call(content: str) -> list[Task]:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        return _parse_response(text)

    return _run_clustering(sessions, _call)


def cluster_with_openai(
    sessions: list[Session], model: str = OPENAI_CHEAP_MODEL
) -> list[Task]:
    """One clustering call against the OpenAI cheap-tier model, with the
    shared correctness + shape retry pipeline (see _run_clustering)."""
    from openai import OpenAI  # type: ignore

    client = OpenAI()

    def _call(content: str) -> list[Task]:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
        )
        text = response.choices[0].message.content or ""
        return _parse_response(text)

    return _run_clustering(sessions, _call)


def cluster_sessions(
    sessions: list[Session], prefer: str = "anthropic"
) -> list[Task] | None:
    """Cluster all in-window sessions in one call to the primary provider's cheap-tier model.

    Returns None if neither API key is configured. Returns an empty list if
    `sessions` is empty (no call is made).
    """
    if not sessions:
        return []

    have_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_openai = bool(os.environ.get("OPENAI_API_KEY"))

    order: list[str]
    if prefer == "anthropic":
        order = ["anthropic", "openai"]
    else:
        order = ["openai", "anthropic"]

    for choice in order:
        try:
            if choice == "anthropic" and have_anthropic:
                return cluster_with_anthropic(sessions)
            if choice == "openai" and have_openai:
                return cluster_with_openai(sessions)
        except Exception as exc:  # noqa: BLE001
            print(f"[clustering] {choice} call failed: {exc!r}", file=sys.stderr)
            continue
    return None


__all__ = [
    "ALLOWED_TASK_TYPES",
    "ANTHROPIC_CHEAP_MODEL",
    "ANTI_COLLAPSE_THRESHOLD",
    "ANTI_SINGLETON_THRESHOLD",
    "FIRST_TURN_MAX_CHARS",
    "LABEL_FORBIDDEN_TOKENS",
    "LABEL_MAX_CHARS",
    "LABEL_SOURCE_FALLBACK",
    "LABEL_SOURCE_LLM",
    "OPENAI_CHEAP_MODEL",
    "SINGLETON_FALLBACK_LABEL_WORDS",
    "Task",
    "build_prompt",
    "build_session_blocks",
    "cluster_sessions",
    "cluster_with_anthropic",
    "cluster_with_openai",
]
