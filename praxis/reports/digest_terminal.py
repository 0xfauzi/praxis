"""Terminal renderer for the v0.2 weekly digest (`praxis week`).

Spec section 6.2: same content order as the HTML digest, ANSI-rendered,
fits in <80 columns. The trajectory headline, the headline moment, and
the follow-up panel are the three sections that MUST render in the
terminal. The cost ledger and 'Where The Week Went' MAY render in a
compact form. The full six-dim panel SHOULD be a footer.

This module is the terminal counterpart to ``praxis/reports/html_report.py``
for the v0.2 weekly digest pipeline. It is distinct from the v0.1
``praxis/reports/terminal.py`` renderer, which is preserved unchanged
for the legacy `praxis scan` output. US-066 established the 80-column
hard constraint, US-067 wired the three mandatory sections with
placeholder degradation, US-068 added the compact cost ledger and
'Where The Week Went' task breakdown, and US-069 landed the full
six-dim panel as the digest's footer.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING

from praxis.reports.baseline_panel import format_baseline_value
from praxis.reports.gating import format_delta
from praxis.scoring.rubric import by_key

if TYPE_CHECKING:
    from praxis.reports.commitment_rollup import CommitmentRollup


# Spec section 6.2: the digest fits in <80 columns. 79 is the hard cap
# for visible characters per line, leaving the 80th column free so that
# terminal emulators that don't soft-wrap don't double-wrap a line that
# happened to land exactly at the edge of the viewport.
MAX_LINE_WIDTH = 79

# The visible-content width, before adding the per-line indent. Keeping
# the indent stable lets section helpers compose lines without each
# rederiving the margin.
INDENT = "  "
CONTENT_WIDTH = MAX_LINE_WIDTH - len(INDENT)


# Reused ANSI palette from the v0.1 renderer so the digest reads with
# the same editorial voice. Kept verbatim so a future palette change
# is one audit point across both surfaces.
TERRA = "\033[38;5;166m"
DIM = "\033[2m"
ITALIC = "\033[3m"
RESET = "\033[0m"


# Matches every ANSI CSI escape (e.g. "\033[1m", "\033[38;5;166m",
# "\033[0m"). The visible-width helper strips these before measuring,
# since spec section 6.2's "<80 columns" constraint is about what
# the terminal renders, not the raw byte length of the line.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def visible_width(line: str) -> int:
    """Visible-character count of `line`, ignoring ANSI escape codes.

    This is the same definition used by terminals when they decide
    whether a line fits in the viewport: control sequences move the
    cursor or change attributes but consume no columns. The 80-column
    constraint in spec section 6.2 is on this measurement, not on
    ``len(line)``.
    """
    return len(_ANSI_RE.sub("", line))


@dataclass
class HeadlineMomentView:
    """Renderer-facing slice of the week's headline coachable moment.

    Spec section 4.1's ``Moment`` dataclass carries additional fields
    (severity, dollar impact, timestamps) that the digest does not
    display; the renderer reads only what it shows. Keeping a narrow
    view at the renderer boundary lets the caller construct one from a
    ``praxis.scoring.moment_selector.Moment`` without dragging the
    selector's storage shape into rendering tests.
    """

    dim_key: str
    quoted_excerpt: str
    why_it_lost_score: str
    suggested_alternative: str


@dataclass
class FollowUpView:
    """Renderer-facing slice of one row of the follow-ups table.

    ``measured_value`` is ``None`` until next week's run closes the
    follow-up (spec section 6.3); while pending, the digest shows the
    commitment and the baseline but not a delta. Once closed,
    ``outcome`` is one of 'improved' | 'unchanged' | 'worse' (spec
    section 6.3 thresholds).
    """

    commitment_text: str
    target_metric: str
    baseline_value: float
    measured_value: float | None = None
    outcome: str = "pending"


@dataclass
class CostLedgerView:
    """Renderer-facing slice of the week's cost ledger (spec section 10.1).

    The cost ledger panel in the HTML digest (section 6.1) carries four
    pieces of information: this week's spend, the 90-day rolling
    baseline, the biggest (model, task) line, and the tier-fit savings
    callout. Each field on this view is pre-aggregated so the renderer
    does no math beyond formatting.

    ``baseline_usd`` is ``None`` when the user has less than 14 days of
    data and no baseline can yet be computed (spec section 7's edge
    case); in that case the renderer shows ``--`` and skips the delta.
    ``over_tier_sessions = 0`` is rendered as a positive "clean" signal
    rather than suppressed, because absence of over-tier waste is
    itself worth surfacing.
    """

    this_week_usd: float
    baseline_usd: float | None = None
    biggest_model: str = ""
    biggest_task_label: str = ""
    biggest_line_usd: float = 0.0
    biggest_line_sessions: int = 0
    over_tier_sessions: int = 0
    tier_fit_savings_usd: float = 0.0


@dataclass
class TaskRowView:
    """One row of the 'Where The Week Went' table (spec section 5.5).

    The acceptance criteria for US-068 fix the visible columns to
    label, session count, total cost, and worst dim. ``worst_dim_key``
    is resolved through ``_dim_title`` so an unknown key renders as
    the raw key rather than crashing the digest.
    """

    label: str
    sessions: int
    total_usd: float
    worst_dim_key: str


@dataclass
class DimRowView:
    """One row of the six-dim footer panel (spec section 6.2).

    ``score`` is this week's mean for the dim on the 0-10 scale.
    ``baseline`` is the 90-day rolling mean, or ``None`` when the user
    has less than 14 days of data and the baseline is still forming
    (spec section 8.4). The renderer derives the delta and runs it
    through the significance gate (spec section 8.3, threshold 0.3 on
    the /10 scale), so callers do not pre-compute the delta string.
    ``dim_key`` is the rubric key; an unknown key renders through
    ``_dim_title`` as the raw key rather than crashing the digest.
    """

    dim_key: str
    score: float
    baseline: float | None = None


@dataclass
class WeeklyDigest:
    """Inputs the digest renderer reads.

    Spec section 6.2 names three MUST-render sections: trajectory
    headline, headline moment, follow-up panel. The cost ledger and
    tasks breakdown ("Where The Week Went") MAY render in a compact
    form; in v0.2 we choose to always render them, falling back to a
    placeholder when the upstream pipeline has not produced data yet
    (spec section 10.1: the cost ledger is "always shown"). The full
    six-dim panel SHOULD render as a footer (US-069). Each section's
    input lives on this dataclass as an Optional field and defaults
    to ``None`` so the renderer can degrade gracefully.
    """

    week_label: str = ""
    trajectory_headline: str | None = None
    headline_moment: HeadlineMomentView | None = None
    follow_up: FollowUpView | None = None
    cost_ledger: CostLedgerView | None = None
    tasks: list[TaskRowView] | None = None
    dimensions: list[DimRowView] | None = None
    # Spec section 2 (coaching-reposition): masthead's commitment block
    # reads this. None means no follow-up exists for the week so the
    # masthead omits the block rather than rendering placeholder copy.
    commitment_rollup: "CommitmentRollup | None" = None


# -------------------------------------------------------------------- helpers


def _wrap(text: str, width: int = CONTENT_WIDTH) -> list[str]:
    """Word-wrap `text` to fit inside the per-line content width.

    Returns an empty list for empty input so callers can `.extend()`
    unconditionally. `break_long_words=False` and
    `break_on_hyphens=False` preserve identifiers and CLI flags that
    happen to contain hyphens; if a single word exceeds the width,
    it is allowed to overflow and the test will catch it. That is the
    right failure mode: silently breaking an identifier would produce
    output that reads correctly but copy-pastes broken.
    """
    if not text:
        return []
    return textwrap.wrap(
        text, width=width, break_long_words=False, break_on_hyphens=False
    )


def _section_rule(title: str) -> list[str]:
    """Eyebrow line: terracotta uppercase title followed by a dim rule.

    The rule is sized so the assembled visible line (indent + title +
    spaces + rule) lands exactly at ``MAX_LINE_WIDTH`` characters in
    the typical case. If the title alone would exceed the budget the
    rule shrinks to zero rather than wrapping; that's a degenerate
    case the test catches if it ever happens in practice.
    """
    title_up = title.upper()
    used = len(INDENT) + len(title_up) + 2  # +2 for the spacer
    rule_len = max(0, MAX_LINE_WIDTH - used)
    return [
        "",
        f"{INDENT}{TERRA}{title_up}{RESET}  {DIM}{'─' * rule_len}{RESET}",
        "",
    ]


def _masthead(week_label: str) -> list[str]:
    """Header block: 'PRAXIS  -  Week of <label>' plus a separator rule.

    Spec section 6.1 anchors the digest on the trajectory headline,
    not the overall /10. The masthead intentionally only carries the
    product name and the week label; the commitment block (spec
    section 2 coaching-reposition) is appended by ``_commitment_block``
    when the digest carries a rollup.
    """
    lines: list[str] = [""]
    title = "PRAXIS"
    label = week_label.strip() or "Weekly read"
    # Compose with explicit hyphens; em dashes are house-banned per
    # CLAUDE.md and a real hyphen also stays inside the visible-width
    # budget without surprising terminal widths.
    header = f"{INDENT}{TERRA}{title}{RESET}  {DIM}-  {label}{RESET}"
    lines.append(header)
    rule = f"{INDENT}{DIM}{'─' * (MAX_LINE_WIDTH - len(INDENT))}{RESET}"
    lines.append(rule)
    return lines


def _self_report_signals_progress(tally: dict[str, int]) -> bool | None:
    """Read the user's self-report tally as a single yes/no signal.

    Returns True when 'yes' outweighs 'no'+'partial', False when the
    reverse, and None when the tally is empty or perfectly balanced
    (no signal either way). 'skip' is intentionally ignored: a skip
    is "no answer given", not a claim of progress in either direction.
    """
    yes = int(tally.get("yes", 0))
    no_partial = int(tally.get("no", 0)) + int(tally.get("partial", 0))
    if yes == 0 and no_partial == 0:
        return None
    if yes > no_partial:
        return True
    if no_partial > yes:
        return False
    return None


def _dim_movement_signal(
    dim_before: float | None, dim_after: float
) -> bool | None:
    """Classify the dim's week-over-week movement against the noise band.

    Returns True when the dim improved by at least
    ``_GAP_DIM_DELTA_THRESHOLD`` points, False when it regressed by at
    least that much, and None when the move is within the noise band
    or no baseline exists (so we can't compute a delta).
    """
    if dim_before is None:
        return None
    delta = dim_after - dim_before
    if delta >= _GAP_DIM_DELTA_THRESHOLD:
        return True
    if delta <= -_GAP_DIM_DELTA_THRESHOLD:
        return False
    return None


def _gap_summary_line(
    self_report_tally: dict[str, int],
    dim_before: float | None,
    dim_after: float,
    *,
    gap_prose: str | None = None,
) -> str:
    """Pick the gap-summary line for the masthead's "Gap:" field.

    Agreement when the self-report and the dim movement point the same
    way, OR when either signal is None (no evidence of contradiction --
    we don't accuse the user of mismatch when the data is silent). The
    agree path always returns the static neutral phrasing.

    Disagreement uses ``gap_prose`` when one was attached upstream by
    the constrained cheap-judge call (US-037). The prose is run through
    ``truncate_to_two_sentences`` here so a runaway response is bounded
    at the renderer boundary rather than relying solely on the prompt's
    "<= 2 sentences" rule. When ``gap_prose`` is None or empty (no API
    key, the judge call failed, or no upstream wiring) the renderer
    falls back to the static disagree line so the field never goes
    blank.
    """
    self_signal = _self_report_signals_progress(self_report_tally)
    data_signal = _dim_movement_signal(dim_before, dim_after)
    if self_signal is None or data_signal is None:
        return _GAP_AGREE_LINE
    if self_signal == data_signal:
        return _GAP_AGREE_LINE
    if gap_prose:
        # Late import keeps the renderer free of the optional Anthropic /
        # OpenAI SDKs that ``gap_judge`` is allowed to touch. Only the
        # pure truncation helper is reached from here.
        from praxis.reports.gap_judge import truncate_to_two_sentences

        truncated = truncate_to_two_sentences(gap_prose)
        if truncated:
            return truncated
    return _GAP_DISAGREE_LINE


def _format_self_report_tally(tally: dict[str, int]) -> str:
    """Render the four-bucket tally as '4 yes / 1 partial / 2 no'.

    Skips zero-count buckets so the line stays tight when only one or
    two buckets fired. When every bucket is zero (no reflections this
    week) returns the explicit 'no check-ins yet' placeholder rather
    than a confusing blank line.
    """
    parts: list[str] = []
    # Spec section 2 example orders yes -> partial -> no in the rendered
    # tally; 'skip' is shown last since it carries the least signal.
    for key in ("yes", "partial", "no", "skip"):
        count = int(tally.get(key, 0))
        if count > 0:
            parts.append(f"{count} {key}")
    if not parts:
        return "no check-ins yet"
    return " / ".join(parts)


def _format_data_says_line(
    target_dim_key: str,
    dim_before: float | None,
    dim_after: float,
) -> str:
    """Render the 'Data says' field value for the targeted dimension.

    With a baseline on file:  '<Dim title>  X.X -> Y.Y  (annotation)'.
    Without a baseline:        '<Dim title>  Y.Y (baseline forming)'.
    The annotation reuses the same significance gate as the gap-summary
    so the two lines never contradict each other.
    """
    dim_title = _dim_title(target_dim_key)
    if dim_before is None:
        return f"{dim_title}  {dim_after:.1f} (baseline forming)"
    delta = dim_after - dim_before
    if delta >= _GAP_DIM_DELTA_THRESHOLD:
        annotation = "improved"
    elif delta <= -_GAP_DIM_DELTA_THRESHOLD:
        annotation = "worse"
    else:
        annotation = "unchanged"
    return (
        f"{dim_title}  {dim_before:.1f} -> {dim_after:.1f}  ({annotation})"
    )


def _format_sessions_line(this_week: int, prior_week: int) -> str:
    """Render the 'Sessions' field value with the prior-week anchor.

    Always includes the '(vs. N last week)' suffix so the reader sees
    the direction-of-travel without having to remember last week's
    count. Both counts are non-negative ints by construction (the
    rollup builder coerces them).
    """
    return f"{this_week} (vs. {prior_week} last week)"


def _field_line(label: str, value: str) -> str:
    """One labelled field row inside the 'How it went' block.

    Label is left-justified to ``_FIELD_LABEL_WIDTH`` then followed by
    a single space and the value, so the values line up under each
    other regardless of which label is on the row.
    """
    return f"{label:<{_FIELD_LABEL_WIDTH}} {value}"


def _commitment_block(rollup: "CommitmentRollup | None") -> list[str]:
    """Render the masthead's commitment block (spec section 2).

    When ``rollup`` is None there is no active commitment for the week
    and the block is omitted entirely (no header, no lines). When a
    rollup is present we render:

        Your focus this week:
          "<display_text>"

        How it went:
          Sessions:    N (vs. M last week)
          You said:    A yes / B partial / C no
          Data says:   <Dim title>  X.X -> Y.Y  (improved)
          Gap:         your self-report and the data agree this week.

    The 'How it went' block degrades to a single 'No sessions logged
    this week.' line when ``sessions_this_week == 0`` so the masthead
    doesn't render confusing zero-comparison numbers.
    """
    if rollup is None:
        return []

    lines: list[str] = [""]
    # Focus quote section ----------------------------------------------
    lines.append(f"{INDENT}{_FOCUS_HEADER}")
    quote = f"\"{rollup.display_text}\""
    for wrapped in _wrap(quote, width=_BODY_WIDTH):
        lines.append(_body_line(wrapped, ansi=ITALIC))
    lines.append("")
    # How it went status block -----------------------------------------
    lines.append(f"{INDENT}{_HOW_IT_WENT_HEADER}")
    if rollup.sessions_this_week <= 0:
        # Spec acceptance: with no sessions there is no per-dim or
        # self-report content worth rendering; the explicit empty-state
        # line keeps the masthead readable without a divide-by-zero.
        lines.append(_body_line(_NO_SESSIONS_LOGGED, ansi=DIM))
        return lines
    sessions_line = _format_sessions_line(
        rollup.sessions_this_week, rollup.sessions_prior_week
    )
    lines.append(_body_line(_field_line("Sessions:", sessions_line)))
    you_said_line = _format_self_report_tally(rollup.self_report_tally)
    lines.append(_body_line(_field_line("You said:", you_said_line)))
    target_key = rollup.target_dim_key
    dim_after = float(rollup.dim_after.get(target_key, 0.0))
    dim_before_raw = rollup.dim_before.get(target_key)
    dim_before = float(dim_before_raw) if dim_before_raw is not None else None
    data_says = _format_data_says_line(target_key, dim_before, dim_after)
    # The full "Data says:" row can exceed the body width with a long
    # dim title; wrap onto continuation rows aligned with the field
    # value column so a multi-line value still reads as one field.
    data_line = _field_line("Data says:", data_says)
    wrapped_data = _wrap(data_line, width=_BODY_WIDTH)
    if not wrapped_data:
        wrapped_data = [data_line]
    lines.append(_body_line(wrapped_data[0]))
    cont_indent = " " * (_FIELD_LABEL_WIDTH + 1)
    for cont in wrapped_data[1:]:
        lines.append(_body_line(cont_indent + cont))
    gap_text = _gap_summary_line(
        rollup.self_report_tally,
        dim_before,
        dim_after,
        gap_prose=rollup.gap_prose,
    )
    gap_line = _field_line("Gap:", gap_text)
    wrapped_gap = _wrap(gap_line, width=_BODY_WIDTH)
    if not wrapped_gap:
        wrapped_gap = [gap_line]
    lines.append(_body_line(wrapped_gap[0]))
    for cont in wrapped_gap[1:]:
        lines.append(_body_line(cont_indent + cont))
    return lines


_BODY_INDENT = INDENT + INDENT  # 4 visible chars: section content sits
# inside the section eyebrow's column, not at the masthead's left edge.
_BODY_WIDTH = CONTENT_WIDTH - len(INDENT)  # 75 visible chars after the
# extra indent; this is what _wrap() should use for content-line copy.

# Placeholder copy when an upstream pipeline has not produced the data
# for a mandatory section yet. Kept as module-level constants so the
# tests can assert on the exact strings and a future copy change is one
# audit point. The wording leans on the existing v0.1 voice ("Run more
# sessions to surface patterns") to stay editorially consistent.
_TRAJECTORY_PLACEHOLDER = "Not enough sessions yet to call a trajectory."
_HEADLINE_MOMENT_PLACEHOLDER = (
    "No coachable moment surfaced this week. Run more sessions to "
    "surface one."
)
_FOLLOW_UP_PLACEHOLDER = (
    "No commitment in flight yet. Next week's digest will open one."
)
_COST_LEDGER_PLACEHOLDER = (
    "Cost ledger pending. Pricing data fills in after the first run."
)
_TASKS_PLACEHOLDER = (
    "No task breakdown yet. Clustering surfaces tasks once it runs."
)
_DIMENSIONS_PLACEHOLDER = (
    "Dim panel pending. Run a session to populate per-dim scores."
)

# Spec section 10.1: spend, baseline, biggest (model, task) line, and
# tier-fit savings. The literal "--" stands in for the baseline when
# the user has < 14 days of data and no baseline exists yet (spec edge
# case at section 7); keeping the placeholder as a single token keeps
# the column alignment in the "vs baseline X" phrasing.
_BASELINE_UNAVAILABLE = "--"


# Spec section 2 (coaching-reposition): the masthead's commitment block
# carries the focus quote, the "how it went" status, and the gap-summary
# closing line. These constants pin the exact label strings so tests can
# assert on them verbatim and a future copy change is one audit point.
_FOCUS_HEADER = "Your focus this week:"
_HOW_IT_WENT_HEADER = "How it went:"
_NO_SESSIONS_LOGGED = "No sessions logged this week."
_GAP_AGREE_LINE = "your self-report and the data agree this week."
# Static fallback for the disagreement path. US-037 swaps this for
# constrained-judge prose when an API key is available; here we ship
# the documented neutral phrasing so the renderer never emits an empty
# gap line and never crashes when the judge is unreachable.
_GAP_DISAGREE_LINE = (
    "Self-report and data differ this week. Worth a moment of curiosity."
)

# Width of the "Sessions:" / "You said:" / "Data says:" / "Gap:" label
# column so the four field values left-align under each other inside
# the "How it went" block.
_FIELD_LABEL_WIDTH = 12

# Significance gate on the /10 scale for deciding whether the dim mean
# moved enough to count as agreement / disagreement with the self-report
# tally. Reuses spec section 8.3's 0.3-per-dim-point noise band (the
# same threshold the six-dim footer uses for delta arrows) so the
# masthead's "Data says" annotation reads on the same scale as the rest
# of the digest.
_GAP_DIM_DELTA_THRESHOLD = 0.3


def _dim_title(dim_key: str) -> str:
    """Resolve a rubric dim_key to its human-facing title.

    Unknown keys fall through to the raw key rather than raising, so a
    moment whose dim_key drifted out of the rubric still renders. The
    test suite catches a hard rubric drift; this just keeps the digest
    from crashing in the wild while we investigate.
    """
    try:
        return by_key(dim_key).title
    except KeyError:
        return dim_key


def _body_line(text: str, *, ansi: str = "") -> str:
    """One content line at the body indent, with optional ANSI wrapper.

    The ANSI wrapper is applied around the visible content only, so
    the wrapped output still measures to the body indent + content
    length when ``visible_width`` strips the escapes.
    """
    if ansi:
        return f"{_BODY_INDENT}{ansi}{text}{RESET}"
    return f"{_BODY_INDENT}{text}"


def _placeholder_lines(text: str) -> list[str]:
    """Wrap a placeholder string at the body indent in DIM ink.

    DIM (not ITALIC) marks placeholder copy so the eye distinguishes
    'this section has no data' from real italicized headlines.
    """
    return [_body_line(line, ansi=DIM) for line in _wrap(text, width=_BODY_WIDTH)]


def _trajectory(headline: str | None) -> list[str]:
    """Render the trajectory headline (spec sections 6.2, 7).

    Mandatory section: always emits the eyebrow plus either the
    italicized headline or a DIM placeholder when the upstream pipeline
    has not produced one yet.
    """
    lines: list[str] = []
    lines.extend(_section_rule("Trajectory"))
    if headline:
        for wrapped in _wrap(headline, width=_BODY_WIDTH):
            lines.append(_body_line(wrapped, ansi=ITALIC))
    else:
        lines.extend(_placeholder_lines(_TRAJECTORY_PLACEHOLDER))
    return lines


def _headline_moment(moment: HeadlineMomentView | None) -> list[str]:
    """Render the headline coachable moment (spec sections 6.2, 4.3).

    Mandatory section. Layout, top to bottom:
      - terracotta dim title (e.g. 'Verification habits')
      - italic quoted excerpt
      - 'Why:' line explaining what cost the score
      - 'Try:' line with the suggested alternative

    Lines wrap to the body width so a long excerpt or suggestion
    cannot push the section over the 80-column budget.
    """
    lines: list[str] = []
    lines.extend(_section_rule("Headline moment"))
    if moment is None:
        lines.extend(_placeholder_lines(_HEADLINE_MOMENT_PLACEHOLDER))
        return lines
    lines.append(_body_line(_dim_title(moment.dim_key), ansi=TERRA))
    quoted = f"\"{moment.quoted_excerpt}\""
    for wrapped in _wrap(quoted, width=_BODY_WIDTH):
        lines.append(_body_line(wrapped, ansi=ITALIC))
    lines.append("")
    for wrapped in _wrap(f"Why: {moment.why_it_lost_score}", width=_BODY_WIDTH):
        lines.append(_body_line(wrapped))
    for wrapped in _wrap(f"Try: {moment.suggested_alternative}", width=_BODY_WIDTH):
        lines.append(_body_line(wrapped))
    return lines


def _format_follow_up_tracking(follow_up: FollowUpView) -> str:
    """One-line tracking summary for the follow-up panel.

    Pending follow-ups (measured_value is None) show only the baseline.
    Closed follow-ups show baseline -> measured and the outcome label
    in brackets. Hyphens (not arrows or em dashes) keep the line in
    the ASCII range that terminals render without surprises.
    """
    if follow_up.measured_value is None:
        return (
            f"Track: {follow_up.target_metric} "
            f"(baseline {follow_up.baseline_value:.2f}, {follow_up.outcome})"
        )
    return (
        f"Track: {follow_up.target_metric} "
        f"{follow_up.baseline_value:.2f} -> {follow_up.measured_value:.2f} "
        f"[{follow_up.outcome}]"
    )


def _follow_up(follow_up: FollowUpView | None) -> list[str]:
    """Render the follow-up panel (spec sections 6.2, 6.3).

    Mandatory section. Shows the commitment text on top, with the
    tracking line ('Track: <metric> ...') underneath. When no
    follow-up has been opened yet (first week of digests, or a
    pipeline that has not yet emitted one) a DIM placeholder takes
    the place of both lines.
    """
    lines: list[str] = []
    lines.extend(_section_rule("Follow-up"))
    if follow_up is None:
        lines.extend(_placeholder_lines(_FOLLOW_UP_PLACEHOLDER))
        return lines
    for wrapped in _wrap(
        f"Commit: {follow_up.commitment_text}", width=_BODY_WIDTH
    ):
        lines.append(_body_line(wrapped))
    for wrapped in _wrap(
        _format_follow_up_tracking(follow_up), width=_BODY_WIDTH
    ):
        lines.append(_body_line(wrapped, ansi=DIM))
    return lines


def _format_spend_line(ledger: CostLedgerView) -> str:
    """Top-of-ledger summary: this-week spend vs baseline, with delta.

    Renders as ``This week: $X.XX vs baseline $Y.YY (+$Z.ZZ)`` when a
    baseline exists, or ``This week: $X.XX vs baseline -- (baseline
    forming)`` when fewer than 14 days of data are on file. The delta
    label uses plus/minus signs (not arrows or em dashes) so the line
    stays ASCII-clean across terminal emulators.
    """
    if ledger.baseline_usd is None:
        return (
            f"This week: ${ledger.this_week_usd:.2f}  "
            f"vs baseline {_BASELINE_UNAVAILABLE} (baseline forming)"
        )
    delta = ledger.this_week_usd - ledger.baseline_usd
    # Sign-aware formatting so a $0.00 delta still reads as "+$0.00"
    # (a deliberate non-result) rather than a confusing bare $0.00.
    sign = "+" if delta >= 0 else "-"
    return (
        f"This week: ${ledger.this_week_usd:.2f}  "
        f"vs baseline ${ledger.baseline_usd:.2f} "
        f"({sign}${abs(delta):.2f})"
    )


def _format_tier_fit_line(ledger: CostLedgerView) -> str:
    """One-line tier-fit callout (spec section 10.1).

    When ``over_tier_sessions`` is zero we surface the clean signal
    instead of suppressing the line; the absence of over-tier waste is
    itself worth flagging so the user can recognize the habit.
    """
    if ledger.over_tier_sessions == 0:
        return "Tier-fit: clean (no over-tier sessions this week)"
    return (
        f"Tier-fit: {ledger.over_tier_sessions} over-tier "
        f"(~${ledger.tier_fit_savings_usd:.2f} saved on the cheaper tier)"
    )


def _cost_ledger(ledger: CostLedgerView | None) -> list[str]:
    """Render the cost ledger panel (spec section 10.1).

    Compact form, sized for the 79-column budget. Layout, top to
    bottom: spend-vs-baseline summary, the biggest (model, task) line
    on its own row with a continuation row carrying the dollar amount
    and session count, then the tier-fit callout.

    When ``ledger`` is ``None`` the section degrades to a DIM
    placeholder, matching the US-067 pattern; spec section 10.1 says
    the panel is "always shown in the digest", so a missing input
    still emits the eyebrow.
    """
    lines: list[str] = []
    lines.extend(_section_rule("Cost ledger"))
    if ledger is None:
        lines.extend(_placeholder_lines(_COST_LEDGER_PLACEHOLDER))
        return lines
    for wrapped in _wrap(_format_spend_line(ledger), width=_BODY_WIDTH):
        lines.append(_body_line(wrapped))
    # Biggest line: "<model> on <task label>" on top, the dollar amount
    # and session count on a continuation row. Splitting the model/task
    # from the spend keeps long task labels from forcing a mid-phrase
    # wrap that would split "$X.XX on N sessions" awkwardly.
    biggest_lead = f"Biggest: {ledger.biggest_model} on {ledger.biggest_task_label}"
    for wrapped in _wrap(biggest_lead, width=_BODY_WIDTH):
        lines.append(_body_line(wrapped))
    biggest_meta = (
        f"${ledger.biggest_line_usd:.2f} over "
        f"{ledger.biggest_line_sessions} sessions"
    )
    # Continuation indent ("  ") lines the meta row up with the words
    # that follow "Biggest: " on the previous row.
    for wrapped in _wrap(biggest_meta, width=_BODY_WIDTH - 2):
        lines.append(_body_line("  " + wrapped, ansi=DIM))
    for wrapped in _wrap(_format_tier_fit_line(ledger), width=_BODY_WIDTH):
        lines.append(_body_line(wrapped))
    return lines


# Spec section 5.5: the digest renders the user's top-3 tasks for the
# week. ``MAX_TASKS_RENDERED`` is the hard cap the renderer enforces in
# case the caller hands over a longer list; ranking has already been
# done upstream (session count, with total cost as tiebreaker).
MAX_TASKS_RENDERED = 3


def _format_task_meta(task: TaskRowView) -> str:
    """One-line metadata for a task row: sessions, cost, worst dim."""
    plural = "session" if task.sessions == 1 else "sessions"
    return (
        f"{task.sessions} {plural}, ${task.total_usd:.2f}, "
        f"worst: {_dim_title(task.worst_dim_key)}"
    )


def _where_the_week_went(tasks: list[TaskRowView] | None) -> list[str]:
    """Render the 'Where The Week Went' task breakdown (spec section 5.5).

    Each top-3 task renders across two body lines: a numbered label
    row (e.g. "1. auth migration debugging") and a metadata row in DIM
    ink with the session count, total cost, and worst dim
    (e.g. "   4 sessions, $5.20, worst: Verification habits"). The
    two-line layout keeps long labels from forcing a mid-row wrap that
    would visually break the column structure.

    Tasks beyond the top-3 are silently dropped: the renderer trusts
    upstream ranking but caps the visible rows regardless, so an
    accidentally-long list from a future caller still respects the
    spec ceiling.
    """
    lines: list[str] = []
    lines.extend(_section_rule("Where the week went"))
    if not tasks:
        lines.extend(_placeholder_lines(_TASKS_PLACEHOLDER))
        return lines
    for rank, task in enumerate(tasks[:MAX_TASKS_RENDERED], start=1):
        # Rank prefix is fixed-width ("1. ") so the continuation row's
        # 3-space indent visually aligns with the label.
        prefix = f"{rank}. "
        wrapped_label = _wrap(task.label, width=_BODY_WIDTH - len(prefix))
        if not wrapped_label:
            wrapped_label = [""]
        lines.append(_body_line(prefix + wrapped_label[0]))
        for cont in wrapped_label[1:]:
            lines.append(_body_line("   " + cont))
        for wrapped in _wrap(
            _format_task_meta(task), width=_BODY_WIDTH - 3
        ):
            lines.append(_body_line("   " + wrapped, ansi=DIM))
    return lines


# Spec section 6.2: every dim renders on one body line so the footer
# stays compact and readable. The title column is padded out to the
# longest rubric title plus a small breather so the score/baseline/
# delta columns align across all six rows. 27 chars covers the longest
# title ("Planning before prompting", 25 chars) with room for one or
# two future renamings without re-tuning the layout.
_DIM_TITLE_COL_WIDTH = 27

# The "baseline X.X" / "baseline --" stub renders to one of two widths
# depending on whether the baseline is forming. The renderer pads the
# shorter form so the delta column aligns across rows; padding to the
# longer form ("baseline X.X" = 12 chars) keeps all 6 rows tidy.
_BASELINE_COL_WIDTH = len("baseline X.X")


def _format_dim_row(view: DimRowView, dim_title: str) -> str:
    """One line: '<title>  X.X/10  baseline Y.Y  <delta>'.

    Both the baseline value and the delta gate on the same boundary:
    when ``baseline`` is ``None``, the row shows ``baseline --`` and a
    parenthetical ``(forming)`` in place of the delta (spec section 8.4).
    When the baseline exists, the delta is the difference between this
    week's score and the baseline, rendered through ``format_delta``
    so the significance gate (spec section 8.3) is applied uniformly
    with the HTML renderer.
    """
    title_col = dim_title.ljust(_DIM_TITLE_COL_WIDTH)
    score_col = f"{view.score:.1f}/10"
    forming = view.baseline is None
    baseline_value = view.baseline if view.baseline is not None else 0.0
    baseline_str = f"baseline {format_baseline_value(baseline_value, forming)}"
    baseline_col = baseline_str.ljust(_BASELINE_COL_WIDTH)
    if forming:
        # No delta when there is no baseline to compare against. The
        # parenthetical mirrors the cost ledger's "baseline forming"
        # phrasing so both panels read the same way under the same
        # precondition.
        delta_col = "(forming)"
    else:
        # ``score`` and ``baseline`` are both /10; the delta lives on
        # the same scale, which is what ``format_delta`` expects.
        delta_col = format_delta(view.score - (view.baseline or 0.0))
    return f"{title_col}  {score_col}  {baseline_col}  {delta_col}"


def _six_dim_panel(dimensions: list[DimRowView] | None) -> list[str]:
    """Render the full six-dim panel as the digest's footer (spec 6.2).

    Spec section 6.2 says the full six-dim panel SHOULD be a footer.
    The renderer takes the list of ``DimRowView`` entries verbatim and
    emits one row per entry; the caller is responsible for supplying
    all six in rubric order, since the renderer treats the panel as a
    pre-ranked list (consistent with the ``tasks`` section's contract).
    When the input is ``None`` or empty the section degrades to a DIM
    placeholder line, mirroring the US-067 / US-068 fallback pattern.
    """
    lines: list[str] = []
    lines.extend(_section_rule("The six dimensions"))
    if not dimensions:
        lines.extend(_placeholder_lines(_DIMENSIONS_PLACEHOLDER))
        return lines
    for view in dimensions:
        lines.append(_body_line(_format_dim_row(view, _dim_title(view.dim_key))))
    return lines


# --------------------------------------------------------------------- render


def render(digest: WeeklyDigest) -> str:
    """Render a weekly digest as ANSI text fitting in <80 columns.

    Returns the assembled string; the caller writes it to stdout or
    captures it in tests. Lines are joined with ``\\n`` and contain no
    trailing whitespace beyond what the section helpers emit.

    Spec contract (section 6.2): every line in the returned string has
    a visible width (post-ANSI-strip) of at most ``MAX_LINE_WIDTH``.
    This is enforced by ``tests/test_digest_terminal.py`` and any
    helper that adds a new section MUST keep the constraint.
    """
    parts: list[str] = []
    parts.extend(_masthead(digest.week_label))
    # Spec section 2 (coaching-reposition): the commitment block sits
    # immediately under the masthead so the digest opens with the
    # active commitment status. When the digest carries no rollup
    # (no active commitment for the week, or an older summary built
    # before US-034) the helper returns an empty list and the block
    # is omitted entirely.
    parts.extend(_commitment_block(digest.commitment_rollup))
    # Spec section 6.2: the trajectory headline, headline moment, and
    # follow-up panel are the three sections that MUST render. They
    # appear first so the digest opens with the multi-week behavioral
    # read (trajectory), then the single most coachable moment, then
    # last week's commitment that closes the loop. The cost ledger and
    # 'Where The Week Went' (US-068) sit after the follow-up so the
    # editorial cadence keeps coaching content before bookkeeping.
    parts.extend(_trajectory(digest.trajectory_headline))
    parts.extend(_headline_moment(digest.headline_moment))
    parts.extend(_follow_up(digest.follow_up))
    parts.extend(_cost_ledger(digest.cost_ledger))
    parts.extend(_where_the_week_went(digest.tasks))
    # Spec section 6.2: the full six-dim panel SHOULD be a footer.
    # It sits AFTER all coaching and bookkeeping sections so the digest
    # closes on the structural readout of the week.
    parts.extend(_six_dim_panel(digest.dimensions))
    # Trailing newline so terminals that print the next prompt without
    # a leading newline don't clash with the last section's content.
    parts.append("")
    return "\n".join(parts)
