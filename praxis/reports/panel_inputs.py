"""Inputs for the v0.3 report-expansion panels (US-038..042).

These dataclasses sit between the orchestrator (which produces a
``WeeklyRunSummary``) and the renderers (HTML + terminal). The adapter
builds ``PanelInputs`` from the summary; each renderer reads the
relevant panel field and emits its section.

Each panel is independently optional so a partial-data run still
renders the panels for which data exists: missing input on one panel
does not block any other. The first panel implemented under this
contract is the behavioral-patterns panel (US-038).
"""
from __future__ import annotations

from dataclasses import dataclass

from praxis.behavior.signals import SIGNAL_KINDS_IN_PANEL_ORDER


# Spec section 12 ("Behavioral patterns" panel): "up to two concrete
# excerpts each, each excerpt clipped to <=120 chars." Kept as
# module-level constants so the adapter, the renderers, and the tests
# all agree on the numbers.
EXCERPT_CHAR_LIMIT = 120
MAX_EXCERPTS_PER_SIGNAL = 2


# Display labels for the seven signal kinds shipped in the existing
# ``praxis/behavior/signals.py``. Keys mirror
# ``SIGNAL_KINDS_IN_PANEL_ORDER`` so a panel can iterate that tuple and
# pick up the matching label.
SIGNAL_LABELS: dict[str, str] = {
    "why_question": "Why-questions",
    "comprehension_check": "Comprehension checks",
    "explanation_request": "Explanation requests",
    "pure_delegation": "Pure delegation",
    "outsourced_debug": "Outsourced debugging",
    "telegraphic": "Telegraphic prompts",
    "own_attempt": "Own-attempt markers",
}


# All seven shipped signal kinds trace to the same primary source
# (Shen & Tamkin's 2026 RCT). Citing per row keeps the contract uniform
# with later panels (US-039..042) whose signals draw from different
# papers; when those signals land they slot in here without changing
# the renderer.
_SHEN_TAMKIN = "Shen & Tamkin 2026 (arXiv 2601.20245)"
SIGNAL_CITATIONS: dict[str, str] = {
    "why_question": _SHEN_TAMKIN,
    "comprehension_check": _SHEN_TAMKIN,
    "explanation_request": _SHEN_TAMKIN,
    "pure_delegation": _SHEN_TAMKIN,
    "outsourced_debug": _SHEN_TAMKIN,
    "telegraphic": _SHEN_TAMKIN,
    "own_attempt": _SHEN_TAMKIN,
}


def clip_excerpt(text: str, limit: int = EXCERPT_CHAR_LIMIT) -> str:
    """Clip a user-turn excerpt for display in the behavioral-patterns panel.

    Strips leading/trailing whitespace and collapses internal newlines
    into single spaces so the excerpt renders as one line. If the result
    exceeds ``limit`` characters, truncates and appends an ellipsis so
    the reader knows the excerpt was cut.
    """
    normalised = " ".join(text.split())
    if len(normalised) <= limit:
        return normalised
    # -3 to leave room for the ellipsis without overshooting the limit.
    return normalised[: limit - 3].rstrip() + "..."


@dataclass(frozen=True)
class BehavioralPatternRow:
    """One row of the behavioral-patterns panel (US-038).

    Carries the aggregate count of this signal across the week and up
    to ``MAX_EXCERPTS_PER_SIGNAL`` example user-turn excerpts, each
    already clipped to ``EXCERPT_CHAR_LIMIT`` chars by the adapter.
    """

    signal_kind: str
    label: str
    count: int
    citation: str
    excerpts: tuple[str, ...] = ()


@dataclass(frozen=True)
class BehavioralPatternsPanel:
    """Behavioral-patterns panel input (US-038).

    ``rows`` is the ordered list of rows in panel-display order. When
    every row has ``count == 0`` the panel renders an empty-state
    message instead of a misleading empty table (US-038 acceptance
    criterion).
    """

    rows: tuple[BehavioralPatternRow, ...] = ()

    @property
    def has_signals(self) -> bool:
        """True when at least one signal kind has a positive count."""
        return any(row.count > 0 for row in self.rows)


@dataclass(frozen=True)
class PanelInputs:
    """Container for the v0.3 expansion-panel inputs (US-038..042).

    Each panel field is independently optional. The adapter populates
    the fields it can build from the in-flight ``WeeklyRunSummary``;
    renderers read only the fields they need. Future panels
    (US-039..042) slot in as additional fields without changing the
    surface of existing panels.
    """

    behavioral_signals: BehavioralPatternsPanel | None = None


__all__ = [
    "EXCERPT_CHAR_LIMIT",
    "MAX_EXCERPTS_PER_SIGNAL",
    "SIGNAL_LABELS",
    "SIGNAL_CITATIONS",
    "SIGNAL_KINDS_IN_PANEL_ORDER",
    "BehavioralPatternRow",
    "BehavioralPatternsPanel",
    "PanelInputs",
    "clip_excerpt",
]
