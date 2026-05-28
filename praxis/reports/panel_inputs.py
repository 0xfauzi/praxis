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

from praxis.behavior.signals import (
    KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER,
    KNOWLEDGE_GAP_LABELS,
    LADDER_KINDS_IN_PANEL_ORDER,
    LADDER_LABELS,
    SCAFFOLDING_KINDS_IN_PANEL_ORDER,
    SIGNAL_KINDS_IN_PANEL_ORDER,
)


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


# US-040 anchors: repeat-task radar and verification-calibration panels.
# The radar surfaces RepeatTask rows from the token-overlap detector
# alongside two primary sources (OpenAI's ChatGPT usage paper documents
# the recurring-prompt finding; Anthropic's Skills work motivates the
# "could become a skill" reframing). The verification-calibration panel
# breaks the week's sessions into four rigor buckets and anchors against
# Sonar's AI Code Trust Index, the Stack Overflow 2025 developer survey,
# and the broader automation-bias literature.
REPEAT_TASK_CITATION = (
    "OpenAI 2025 ChatGPT usage paper + Anthropic Skills documentation"
)
# Spec section 12: a repeat-task is a candidate for skill extraction. The
# renderer surfaces this label as a small tag next to each row so the
# reader can scan the radar for skill candidates.
REPEAT_TASK_SKILL_TAG = "Could become a skill"

# Default rolling window the radar measures recurrence over. Surfaces
# both in the adapter (when it calls detect_repeats) and in the renderer
# (so the empty-state copy can name the window honestly).
REPEAT_TASK_WINDOW_DAYS = 7

VERIFICATION_CALIBRATION_CITATION = (
    "Sonar AI Code Trust Index + Stack Overflow Developer Survey 2025 + "
    "automation-bias literature"
)


# Verification-calibration buckets in display order (highest to lowest
# rigor). The adapter categorizes each session by its highest-rigor
# verification activity; the renderer iterates this tuple so the row
# order is stable across the document.
VERIFICATION_CALIBRATION_KINDS_IN_PANEL_ORDER: tuple[str, ...] = (
    "source_check",
    "test_run",
    "spot_check",
    "blanket_accept",
)

VERIFICATION_CALIBRATION_LABELS: dict[str, str] = {
    "source_check": "Source-check",
    "test_run": "Test-run",
    "spot_check": "Spot-check",
    "blanket_accept": "Blanket-accept",
}


@dataclass(frozen=True)
class RepeatTaskRow:
    """One row of the repeat-task radar panel (US-040).

    Carries the canonical first sentence, the recurrence count, and the
    estimated minutes per occurrence so the renderer can surface the
    "a skill could reclaim ~N min" hint. The ``skill_tag`` is the
    canonical "Could become a skill" string the renderer prints next to
    every row, kept as a field so a future per-row tag override is one
    point of change rather than a renderer-side branch.
    """

    canonical_first_sentence: str
    occurrences: int
    estimated_minutes_per_occurrence: float
    skill_tag: str = REPEAT_TASK_SKILL_TAG


@dataclass(frozen=True)
class RepeatTaskRadarPanel:
    """Repeat-task radar panel input (US-040).

    ``rows`` is the list of detected repeat tasks for the week. When
    ``detect_repeats`` returns an empty list the panel renders the
    explicit "No repeat tasks detected this week." copy instead of an
    empty table, per US-040 acceptance.
    """

    rows: tuple[RepeatTaskRow, ...] = ()
    window_days: int = REPEAT_TASK_WINDOW_DAYS
    citation: str = REPEAT_TASK_CITATION

    @property
    def has_repeats(self) -> bool:
        """True when at least one repeat task was detected this week."""
        return bool(self.rows)

    @property
    def total_reclaimable_minutes(self) -> float:
        """Sum of (occurrences * estimated_minutes_per_occurrence) across
        rows. Surfaces as the radar's "skills could reclaim ~N min/week"
        callout in the renderer when at least one row is present.
        """
        return sum(
            row.occurrences * row.estimated_minutes_per_occurrence
            for row in self.rows
        )


@dataclass(frozen=True)
class VerificationCalibrationPanel:
    """Verification-calibration panel input (US-040).

    The four counts represent how many SESSIONS in the week landed in
    each rigor bucket. Each session is categorized by its highest-rigor
    verification activity observed across its user turns: a session that
    both ran tests and source-checked counts in the source-check bucket
    only, so the panel reads as a histogram of the user's verification
    ceiling rather than a tally of activities.

    ``blanket_accept_count`` is sessions with no detected verification
    activity. This is the automation-bias bucket and is the bar the
    renderer surfaces inline so the reader can see how often the week
    defaulted to trust.
    """

    source_check_count: int = 0
    test_run_count: int = 0
    spot_check_count: int = 0
    blanket_accept_count: int = 0
    citation: str = VERIFICATION_CALIBRATION_CITATION

    @property
    def total_sessions(self) -> int:
        """Total sessions placed in any bucket this week."""
        return (
            self.source_check_count
            + self.test_run_count
            + self.spot_check_count
            + self.blanket_accept_count
        )

    @property
    def has_sessions(self) -> bool:
        """True when at least one session was categorized this week."""
        return self.total_sessions > 0

    def count_for(self, kind: str) -> int:
        """Look up a bucket's count by its display-order key.

        Returns 0 for unknown keys rather than raising, so the renderer
        can iterate ``VERIFICATION_CALIBRATION_KINDS_IN_PANEL_ORDER``
        without a try/except.
        """
        return {
            "source_check": self.source_check_count,
            "test_run": self.test_run_count,
            "spot_check": self.spot_check_count,
            "blanket_accept": self.blanket_accept_count,
        }.get(kind, 0)


# US-041 anchors: specification adoption, context engineering depth,
# knowledge-gap distribution. Each citation is a module-level constant
# so the adapter, renderers, and tests share one source of truth.
SPECIFICATION_ADOPTION_CITATION = (
    "Woodward (Google I/O 2026 Dialogues) + SpecKit + Sean Grove "
    "\"The New Code\""
)

CONTEXT_ENGINEERING_CITATION = (
    "DORA 2025 (top-7 AI capability) + Anthropic Agent Skills"
)

KNOWLEDGE_GAP_CITATION = (
    "arXiv 2501.11709 (44.6% vs 12.6% gap rate)"
)


# Display labels for the scaffolding kinds shipped in signals.py. Keys
# mirror SCAFFOLDING_KINDS_IN_PANEL_ORDER so the renderer can iterate
# that tuple safely.
SCAFFOLDING_LABELS: dict[str, str] = {
    "claude_md": "CLAUDE.md",
    "agents_md": "AGENTS.md",
    "copilot_instructions": "copilot-instructions.md",
    "projects": "Projects / Custom GPT",
    "skills": "Skills / subagents / hooks",
}


@dataclass(frozen=True)
class SpecificationAdoptionPanel:
    """Specification-adoption panel input (US-041).

    Counts how many sessions in the week OPENED with a structured spec
    block (Markdown headings or label-colon form) per the spec section
    11 signal. ``adoption_share`` is the ratio of sessions that opened
    with a spec block to the total session count; the renderer surfaces
    this share as a percentage with the Woodward / SpecKit / Sean Grove
    citation inline.
    """

    sessions_with_spec: int = 0
    total_sessions: int = 0
    citation: str = SPECIFICATION_ADOPTION_CITATION

    @property
    def has_sessions(self) -> bool:
        """True when at least one session was observed this week."""
        return self.total_sessions > 0

    @property
    def adoption_share(self) -> float:
        """Share of sessions that opened with a spec block.

        Returns 0.0 when no sessions exist so the renderer can guard on
        ``has_sessions`` to decide between empty-state and populated
        paths; consumers should not interpret 0.0 directly without that
        check.
        """
        if self.total_sessions <= 0:
            return 0.0
        return self.sessions_with_spec / self.total_sessions


@dataclass(frozen=True)
class ContextEngineeringRow:
    """One row of the context-engineering-depth panel (US-041).

    Carries the count of sessions in the week that referenced this
    scaffolding kind (e.g. CLAUDE.md, AGENTS.md, Projects, Skills).
    """

    kind: str
    label: str
    sessions_with_artifact: int


@dataclass(frozen=True)
class ContextEngineeringDepthPanel:
    """Context-engineering-depth panel input (US-041).

    ``rows`` is the ordered list of rows in panel-display order. The
    renderer surfaces the count per scaffolding kind plus the DORA 2025
    + Anthropic Skills citation inline.
    """

    rows: tuple[ContextEngineeringRow, ...] = ()
    total_sessions: int = 0
    citation: str = CONTEXT_ENGINEERING_CITATION

    @property
    def has_any_artifact(self) -> bool:
        """True when at least one scaffolding kind fired this week."""
        return any(row.sessions_with_artifact > 0 for row in self.rows)


@dataclass(frozen=True)
class KnowledgeGapRow:
    """One row of the knowledge-gap distribution panel (US-041).

    Carries the count of USER TURNS in the week classified into this
    knowledge-gap category. Always present in panel-display order, even
    when ``count`` is zero (US-041 acceptance: no silent drops).
    """

    kind: str
    label: str
    count: int


@dataclass(frozen=True)
class KnowledgeGapDistributionPanel:
    """Knowledge-gap distribution panel input (US-041).

    ``rows`` lists the four arXiv 2501.11709 categories in display
    order. Every row is present even when its count is zero so the
    panel reads as an honest histogram; the renderer surfaces the
    explicit empty-state copy ("No knowledge gaps detected this week.")
    only when every category is zero.
    """

    rows: tuple[KnowledgeGapRow, ...] = ()
    citation: str = KNOWLEDGE_GAP_CITATION

    @property
    def has_gaps(self) -> bool:
        """True when at least one category has a positive count."""
        return any(row.count > 0 for row in self.rows)

    @property
    def total_gaps(self) -> int:
        """Sum of all per-category counts across the week."""
        return sum(row.count for row in self.rows)


# US-042 anchors: tool/agent ladder and refined cost-effectiveness.
# The ladder panel surfaces the user's max ladder rung (prompt-only ->
# tools-on -> skills -> hooks -> subagents) across the week; the
# cost-effectiveness panel applies the deterministic counterfactual
# rule documented in praxis/models_advisor/advisor.py.
TOOL_AGENT_LADDER_CITATION = (
    "Anthropic Skills/hooks/subagents + OpenAI harness engineering"
)

COST_EFFECTIVENESS_CITATION = (
    "Anthropic + OpenAI model card pricing"
)


@dataclass(frozen=True)
class LadderRungRow:
    """One row of the tool/agent ladder panel (US-042).

    Carries the count of sessions that topped out at this rung during
    the week. The renderer iterates ``LADDER_KINDS_IN_PANEL_ORDER`` so
    the rows are always present in stable order, even when a particular
    rung's count is zero.
    """

    kind: str
    label: str
    session_count: int


@dataclass(frozen=True)
class ToolAgentLadderPanel:
    """Tool/agent ladder panel input (US-042).

    ``rows`` is the list of rung counts in panel-display order;
    ``max_rung_kind`` is the highest rung any session reached this
    week (the renderer's headline value). When no sessions were
    observed, ``max_rung_kind`` is ``None`` and ``has_activity`` is
    False so the renderer surfaces an empty-state placeholder rather
    than misleading zero counts.
    """

    rows: tuple[LadderRungRow, ...] = ()
    max_rung_kind: str | None = None
    max_rung_label: str = ""
    citation: str = TOOL_AGENT_LADDER_CITATION

    @property
    def has_activity(self) -> bool:
        """True when at least one session was observed this week."""
        return self.max_rung_kind is not None

    @property
    def total_sessions(self) -> int:
        """Sum of session counts across every rung this week."""
        return sum(row.session_count for row in self.rows)


@dataclass(frozen=True)
class RefinedCostEffectivenessPanel:
    """Refined cost-effectiveness panel input (US-042).

    Carries the four fields the renderer reads to surface the
    counterfactual: the dominant higher-tier model name, the
    corresponding fast-tier sibling, the dollars spent on the higher
    tier across qualifying sessions, and the deterministic overspend
    figure.

    ``has_cost_data`` is the gate the renderer uses to decide between
    "$Y overspend" copy and the explicit "No cost data this week."
    empty-state. Per US-042 AC, a $0 overspend WITH cost data is a real
    "you optimized well" signal that the panel should surface; the
    empty-state copy fires only when no priced session was observed
    at all this week.
    """

    higher_tier_display: str = ""
    lower_tier_display: str = ""
    spent_on_higher_tier_usd: float = 0.0
    overspend_usd: float = 0.0
    qualifying_session_count: int = 0
    has_cost_data: bool = False
    citation: str = COST_EFFECTIVENESS_CITATION

    @property
    def has_overspend(self) -> bool:
        """True when the panel can surface a non-trivial overspend.

        Requires both a positive overspend dollar figure AND at least
        one qualifying session. A $0 overspend with cost data is still
        a positive signal (the user optimized well) and the renderer
        surfaces a different sentence for that case; this property
        gates the "you spent $X for tasks $Y could have done" copy
        specifically.
        """
        return (
            self.has_cost_data
            and self.overspend_usd > 0.0
            and self.qualifying_session_count > 0
        )


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
    repeat_task_radar: RepeatTaskRadarPanel | None = None
    verification_calibration: VerificationCalibrationPanel | None = None
    specification_adoption: SpecificationAdoptionPanel | None = None
    context_engineering: ContextEngineeringDepthPanel | None = None
    knowledge_gap_distribution: KnowledgeGapDistributionPanel | None = None
    tool_agent_ladder: ToolAgentLadderPanel | None = None
    refined_cost_effectiveness: RefinedCostEffectivenessPanel | None = None


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
    "REPEAT_TASK_CITATION",
    "REPEAT_TASK_SKILL_TAG",
    "REPEAT_TASK_WINDOW_DAYS",
    "VERIFICATION_CALIBRATION_CITATION",
    "VERIFICATION_CALIBRATION_KINDS_IN_PANEL_ORDER",
    "VERIFICATION_CALIBRATION_LABELS",
    "SPECIFICATION_ADOPTION_CITATION",
    "CONTEXT_ENGINEERING_CITATION",
    "KNOWLEDGE_GAP_CITATION",
    "SCAFFOLDING_KINDS_IN_PANEL_ORDER",
    "SCAFFOLDING_LABELS",
    "KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER",
    "KNOWLEDGE_GAP_LABELS",
    "LADDER_KINDS_IN_PANEL_ORDER",
    "LADDER_LABELS",
    "TOOL_AGENT_LADDER_CITATION",
    "COST_EFFECTIVENESS_CITATION",
    "BehavioralPatternRow",
    "BehavioralPatternsPanel",
    "AugAutoBalancePanel",
    "CadencePanel",
    "RepeatTaskRow",
    "RepeatTaskRadarPanel",
    "VerificationCalibrationPanel",
    "SpecificationAdoptionPanel",
    "ContextEngineeringRow",
    "ContextEngineeringDepthPanel",
    "KnowledgeGapRow",
    "KnowledgeGapDistributionPanel",
    "LadderRungRow",
    "ToolAgentLadderPanel",
    "RefinedCostEffectivenessPanel",
    "PanelInputs",
    "clip_excerpt",
]
