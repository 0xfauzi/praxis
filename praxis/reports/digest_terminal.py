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
(US-067/068/069) flesh out the per-section content; this story
(US-066) establishes the 80-column hard constraint and the test that
locks it in.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field


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
class WeeklyDigest:
    """Inputs the digest renderer reads.

    Subsequent stories (US-067, US-068, US-069) populate the
    section-specific fields. For US-066 the renderer only needs the
    masthead label and the trajectory headline to exercise the
    80-column constraint end-to-end; later stories will add headline
    moment, follow-up, cost ledger, tasks, and the six-dim panel as
    typed nested dataclasses, defaulting to ``None`` so a missing
    section degrades to a placeholder rather than crashing.
    """

    week_label: str = ""
    trajectory_headline: str | None = None
    # Future-story fields land here as Optional dataclasses. Keeping
    # them out for now would force a churn-y signature change for the
    # very next iteration; an empty schema today is OK, but the shape
    # is locked so the renderer's call sites don't drift.
    _reserved: dict[str, object] = field(default_factory=dict)


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


def _trajectory(headline: str | None) -> list[str]:
    """Render the trajectory headline (spec section 7).

    US-066's job is the width constraint, not the prose; US-067 will
    define the placeholder copy when ``headline`` is ``None``. For now
    we simply omit the section when there's nothing to render, which
    is the conservative behavior under the spec (omission, not noise).
    """
    if not headline:
        return []
    lines: list[str] = []
    lines.extend(_section_rule("Trajectory"))
    for wrapped in _wrap(headline, width=CONTENT_WIDTH - len(INDENT)):
        lines.append(f"{INDENT}{INDENT}{ITALIC}{wrapped}{RESET}")
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
    parts.extend(_trajectory(digest.trajectory_headline))
    # Trailing newline so terminals that print the next prompt without
    # a leading newline don't clash with the last section's content.
    parts.append("")
    return "\n".join(parts)
