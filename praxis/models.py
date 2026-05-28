"""Normalized data model.

Every provider scanner emits Sessions in this shape, so the rest of the
pipeline doesn't need to know whether a turn came from Claude, Codex,
or Copilot.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal


Severity = Literal["minor", "moderate", "major"]
Confidence = Literal["low", "medium", "high"]


class Provider(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    COPILOT = "copilot"


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


@dataclass
class Turn:
    role: Role
    content: str
    timestamp: datetime | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Provider-specific extras (model name, token counts, etc.) live here.
    meta: dict[str, Any] = field(default_factory=dict)
    # True when this Role.USER turn's content is entirely synthesised by
    # the AI tool (Codex's AGENTS.md preamble, Claude Code's
    # `<system-reminder>` / `<command-name>` wrappers, slash-command
    # caveats, etc.) rather than authored by the human. Scanners set
    # this at parse time; behavioural-signal extractors iterate
    # ``session.user_authored_turns`` (which filters tool_injected=True
    # out) so the false-positive on a tool preamble doesn't inflate
    # engagement / atrophy counts. See issue #4.
    tool_injected: bool = False


@dataclass
class Session:
    """A normalized conversation. One Session = one chat thread."""

    provider: Provider
    session_id: str          # provider-native id
    started_at: datetime
    turns: list[Turn]
    source_path: str         # absolute path on disk, for traceability
    project_hint: str | None = None  # e.g. cwd or workspace name if known
    model_hint: str | None = None    # e.g. "claude-opus-4-7" if known

    @property
    def stable_id(self) -> str:
        """Hash-based id that survives provider changes."""
        h = hashlib.sha256()
        h.update(self.provider.value.encode())
        h.update(self.session_id.encode())
        return h.hexdigest()[:16]

    @property
    def user_turns(self) -> list[Turn]:
        """Every ``Role.USER`` turn, including tool-injected preambles.

        Kept for explicit "literal raw user-role turns" callers; most code
        should iterate :attr:`user_authored_turns` instead (which drops
        AGENTS.md / system-reminder wrappers).
        """
        return [t for t in self.turns if t.role == Role.USER]

    @property
    def user_authored_turns(self) -> list[Turn]:
        """``Role.USER`` turns the human actually wrote.

        Filters out turns that scanners marked ``tool_injected=True`` --
        Codex AGENTS.md preambles, Claude Code system-reminder wrappers,
        slash-command caveat blocks, etc. -- so regex-based signal
        extractors don't count the tool's content against the user.
        """
        return [
            t for t in self.turns if t.role == Role.USER and not t.tool_injected
        ]

    @property
    def assistant_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.role == Role.ASSISTANT]

    @property
    def turn_count(self) -> int:
        return len(self.turns)


@dataclass(frozen=True)
class Moment:
    """Structured pointer to a transcript span where one rubric dim dropped.

    Created by the judge (spec §4.2). The fields below `severity` are
    populated downstream: moment_id, session_stable_id, and created_at
    by the persistence layer (§4.1, §14); dollar_impact_estimate and
    minutes_impact_estimate by the cost-attribution pass (§10.2).
    """

    dim_key: str
    turn_index: int
    quoted_excerpt: str
    why_it_lost_score: str
    suggested_alternative: str
    severity: Severity
    moment_id: str | None = None
    session_stable_id: str | None = None
    created_at: datetime | None = None
    dollar_impact_estimate: float | None = None
    minutes_impact_estimate: int | None = None


def compute_moment_id(session_stable_id: str, dim_key: str, turn_index: int) -> str:
    """Spec §4.1: moment_id = sha256(session.stable_id + dim_key + turn_index)[:16].

    Deterministic: re-judging the same (session, dim, turn) yields the same id,
    which makes the moments table's primary-key upserts idempotent.
    """
    h = hashlib.sha256()
    h.update(session_stable_id.encode())
    h.update(dim_key.encode())
    h.update(str(turn_index).encode())
    return h.hexdigest()[:16]
