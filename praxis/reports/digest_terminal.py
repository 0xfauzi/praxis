"""Terminal renderer for the v0.2 weekly digest (`praxis week`).

Spec section 6.2: same content order as the HTML digest, ANSI-rendered,
fits in <80 columns. The trajectory headline, the headline moment, and
the follow-up panel are the three sections that MUST render in the
terminal. The cost ledger and 'Where The Week Went' MAY render in a
compact form. The full six-dim panel SHOULD be a footer.

This module is the terminal counterpart to ``praxis/reports/html_report.py``
for the v0.2 weekly digest pipeline. It is distinct from the v0.1
``praxis/reports/terminal.py`` renderer, which is preserved unchanged
for the legacy `praxis scan` output. Subsequent stories
(US-068/069) flesh out the optional sections; US-066 established the
80-column hard constraint and US-067 wired the three mandatory
sections with placeholder degradation.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

from praxis.scoring.rubric import by_key


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
class WeeklyDigest:
    """Inputs the digest renderer reads.

    Spec section 6.2 names three MUST-render sections: trajectory
    headline, headline moment, follow-up panel. Each lives on this
    dataclass as an Optional field and defaults to ``None`` so the
    renderer can fall back to a clear placeholder rather than crashing
    when the upstream pipeline has not produced data yet. US-068's
    cost ledger / tasks and US-069's six-dim footer land as additional
    optional fields in later iterations.
    """

    week_label: str = ""
    trajectory_headline: str | None = None
    headline_moment: HeadlineMomentView | None = None
    follow_up: FollowUpView | None = None


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
    product name and the week label.
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
    # Spec section 6.2: the trajectory headline, headline moment, and
    # follow-up panel are the three sections that MUST render. They
    # appear in that order so the digest opens with the multi-week
    # behavioral read (trajectory), then the single most coachable
    # moment, then last week's commitment that closes the loop.
    parts.extend(_trajectory(digest.trajectory_headline))
    parts.extend(_headline_moment(digest.headline_moment))
    parts.extend(_follow_up(digest.follow_up))
    # Trailing newline so terminals that print the next prompt without
    # a leading newline don't clash with the last section's content.
    parts.append("")
    return "\n".join(parts)
