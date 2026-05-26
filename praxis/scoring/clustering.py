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
import sys
from dataclasses import dataclass

from praxis.models import Session


# Spec 5.2: "Truncate each first-user-turn to 400 chars before sending."
FIRST_TURN_MAX_CHARS = 400

# Cheap-tier model from each supported provider. Spec 5.2:
# "The cheap-tier model from the user's primary provider
# (Haiku if Anthropic, gpt-5-mini if OpenAI)."
ANTHROPIC_CHEAP_MODEL = "claude-haiku-4-5"
OPENAI_CHEAP_MODEL = "gpt-5-mini"


@dataclass
class Task:
    """One task cluster returned by the LLM."""

    label: str
    task_type: str
    session_ids: list[str]
    rationale: str


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


def cluster_with_anthropic(
    sessions: list[Session], model: str = ANTHROPIC_CHEAP_MODEL
) -> list[Task]:
    """One clustering call against the Anthropic cheap-tier model."""
    from anthropic import Anthropic  # type: ignore

    client = Anthropic()
    prompt = build_prompt(sessions)
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text"
    )
    return _parse_response(text)


def cluster_with_openai(
    sessions: list[Session], model: str = OPENAI_CHEAP_MODEL
) -> list[Task]:
    """One clustering call against the OpenAI cheap-tier model."""
    from openai import OpenAI  # type: ignore

    client = OpenAI()
    prompt = build_prompt(sessions)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content or ""
    return _parse_response(text)


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
    "ANTHROPIC_CHEAP_MODEL",
    "FIRST_TURN_MAX_CHARS",
    "OPENAI_CHEAP_MODEL",
    "Task",
    "build_prompt",
    "build_session_blocks",
    "cluster_sessions",
    "cluster_with_anthropic",
    "cluster_with_openai",
]
