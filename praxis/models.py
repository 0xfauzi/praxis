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
        return [t for t in self.turns if t.role == Role.USER]

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
