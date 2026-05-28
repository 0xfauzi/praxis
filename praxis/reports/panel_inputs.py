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


# US-039 anchors: the industry-share reference for the augmentation /
# automation balance panel and the high-adopter spectrum citation for
# the cadence panel. Strings are kept as module-level constants so the
# adapter, the renderers, and the tests all share one source of truth
# and a future copy change is one audit point.
AUG_AUTO_ANCHOR_CITATION = (
    "Anthropic Economic Index 2025 (~52% augmentation / 45% automation)"
)
AUG_AUTO_INDUSTRY_AUG_SHARE = 0.52
AUG_AUTO_INDUSTRY_AUTO_SHARE = 0.45

CADENCE_ANCHOR_CITATION = "arXiv 2509.19708 (high-adopter spectrum)"

# v0.3 cadence panel: the rolling window the cadence-detector reports
# against. Surfaces both in the renderer (for the empty-state copy) and
# in the adapter (so the count of substantive sessions can be sliced
# uniformly).
CADENCE_WINDOW_DAYS = 21


@dataclass(frozen=True)
class AugAutoBalancePanel:
    """Augmentation/automation balance panel input (US-039).

    The three counts are session-level: how many sessions in the week
    the aug_auto classifier labelled as augmentation, automation, or
    mixed. ``unclassified_count`` is sessions in the week with no
    classifier output (e.g. the row predates the classifier shipping).

    ``classifier_unavailable`` is the renderer's gate for the "no API
    key" path: when every session in the week is unclassified the panel
    surfaces the explicit unavailable message rather than rendering a
    misleading 0/0/0 split. The classifier-NULL state is structurally
    different from "the user ran no sessions this week" (which renders
    as the zero-sessions empty state).
    """

    augmentation_count: int = 0
    automation_count: int = 0
    mixed_count: int = 0
    unclassified_count: int = 0
    classifier_unavailable: bool = False
    industry_anchor_citation: str = AUG_AUTO_ANCHOR_CITATION
    industry_augmentation_share: float = AUG_AUTO_INDUSTRY_AUG_SHARE
    industry_automation_share: float = AUG_AUTO_INDUSTRY_AUTO_SHARE

    @property
    def classified_total(self) -> int:
        """Sessions with a non-NULL aug_auto label this week."""
        return self.augmentation_count + self.automation_count + self.mixed_count

    @property
    def augmentation_share(self) -> float:
        """Share of CLASSIFIED sessions labelled augmentation.

        Computed against ``classified_total`` (not session_count) so the
        share reflects the classifier's vote, not a denominator inflated
        by NULL rows. Returns 0.0 when no classified sessions exist;
        callers should check ``classifier_unavailable`` before reading.
        """
        total = self.classified_total
        return self.augmentation_count / total if total else 0.0

    @property
    def automation_share(self) -> float:
        total = self.classified_total
        return self.automation_count / total if total else 0.0

    @property
    def mixed_share(self) -> float:
        total = self.classified_total
        return self.mixed_count / total if total else 0.0


# US-039 cadence panel: the three positions on the high-adopter
# spectrum reported by cadence-detector. Surfaced as a literal so the
# renderer can resolve a position to a display string without dragging
# in cadence.py at import time (cadence-detector ships in a sibling
# story; the renderer here only sees the resolved label).
CadencePosition = str  # "low" | "moderate" | "high"


CADENCE_POSITION_LABELS: dict[str, str] = {
    "low": "Low-adopter",
    "moderate": "Moderate-adopter",
    "high": "High-adopter",
}


@dataclass(frozen=True)
class CadencePanel:
    """Cadence panel input (US-039).

    Carries the weekday-active streak in the rolling window (default 21
    days) plus the resolved high-adopter spectrum position. When
    ``substantive_session_count`` is zero the renderer surfaces the
    explicit "no substantive sessions" message and omits the high-
    adopter label, since the spectrum position is undefined without any
    activity to place on it.
    """

    weekday_streak: int = 0
    substantive_session_count: int = 0
    window_days: int = CADENCE_WINDOW_DAYS
    high_adopter_position: CadencePosition | None = None
    citation: str = CADENCE_ANCHOR_CITATION

    @property
    def has_activity(self) -> bool:
        """True when at least one substantive session fell in the window."""
        return self.substantive_session_count > 0

    @property
    def position_label(self) -> str:
        """User-facing label for the high-adopter position, or empty
        string when the position is undefined."""
        if self.high_adopter_position is None:
            return ""
        return CADENCE_POSITION_LABELS.get(self.high_adopter_position, "")


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
    aug_auto_balance: AugAutoBalancePanel | None = None
    cadence: CadencePanel | None = None


__all__ = [
    "EXCERPT_CHAR_LIMIT",
    "MAX_EXCERPTS_PER_SIGNAL",
    "SIGNAL_LABELS",
    "SIGNAL_CITATIONS",
    "SIGNAL_KINDS_IN_PANEL_ORDER",
    "AUG_AUTO_ANCHOR_CITATION",
    "AUG_AUTO_INDUSTRY_AUG_SHARE",
    "AUG_AUTO_INDUSTRY_AUTO_SHARE",
    "CADENCE_ANCHOR_CITATION",
    "CADENCE_WINDOW_DAYS",
    "CADENCE_POSITION_LABELS",
    "BehavioralPatternRow",
    "BehavioralPatternsPanel",
    "AugAutoBalancePanel",
    "CadencePanel",
    "PanelInputs",
    "clip_excerpt",
]
