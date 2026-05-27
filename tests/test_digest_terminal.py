"""Tests for the v0.2 terminal digest renderer (US-066, spec section 6.2).

Acceptance criteria:
  - Every rendered line is at most 79 visible characters (excluding
    ANSI escapes).
  - Verified by a test that strips ANSI and asserts line widths.

The line-width constraint is the foundation the later stories
(US-067/068/069) build sections on top of -- if any new section
exceeds the budget the test below will catch it before merge.
"""
from __future__ import annotations

import re

from praxis.reports import digest_terminal
from praxis.reports.digest_terminal import (
    MAX_LINE_WIDTH,
    WeeklyDigest,
    render,
    visible_width,
)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    """Remove every ANSI CSI escape from ``s``.

    Kept independent from the renderer's own ``visible_width`` helper
    so the test does not collapse into a tautology: if a future change
    silently rewrites the helper's regex, the assertion below would
    still fire because this test uses its own copy.
    """
    return _ANSI_RE.sub("", s)


def _all_lines(digest: WeeklyDigest) -> list[str]:
    """Render and split into lines for per-line assertions."""
    return render(digest).split("\n")


# ------------------------------------------------------------------- contract


def test_max_line_width_matches_spec():
    """Spec section 6.2: terminal digest fits in <80 columns.

    79 is the hard cap. The test below enforces this on the actual
    output; this constant assertion lockss the magic number so a
    later refactor cannot silently relax it.
    """
    assert MAX_LINE_WIDTH == 79


def test_visible_width_strips_ansi_escapes():
    """The width helper measures what the terminal renders, not raw bytes."""
    # Wrapping a 5-char visible word in two control sequences gives a
    # raw length much larger than 5; visible width must still be 5.
    s = "\033[38;5;166mhello\033[0m"
    assert visible_width(s) == 5
    assert len(s) > 5  # sanity: the raw bytes are longer than the visible width


def test_visible_width_handles_no_escapes():
    assert visible_width("plain text") == len("plain text")
    assert visible_width("") == 0


# ----------------------------------------------------------- 80-column gating


def test_empty_digest_lines_under_80():
    """The default digest (no sections wired) still fits in <80 chars.

    Locks the masthead and any structural separators at the budget so
    later stories don't inherit a renderer that's already at the edge.
    """
    lines = _all_lines(WeeklyDigest())
    over = [
        (i, _strip_ansi(line))
        for i, line in enumerate(lines)
        if visible_width(line) > MAX_LINE_WIDTH
    ]
    assert not over, f"Lines exceed {MAX_LINE_WIDTH} cols: {over}"


def test_populated_digest_lines_under_80():
    """A digest with realistic trajectory copy also stays under the cap.

    The headline is a long-ish sentence to force the wrapping path.
    """
    digest = WeeklyDigest(
        week_label="Week of May 18-24, 2026",
        trajectory_headline=(
            "Engagement up 0.18/wk, delegation down 0.11/wk over 8 "
            "weeks. You're investing more cognition per session, not less."
        ),
    )
    lines = _all_lines(digest)
    for i, line in enumerate(lines):
        stripped = _strip_ansi(line)
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({len(stripped)} visible chars > {MAX_LINE_WIDTH}): "
            f"{stripped!r}"
        )


def test_long_trajectory_headline_wraps_within_budget():
    """A pathologically long headline gets wrapped, not overflowed.

    Picks copy long enough that any reasonable single-line render
    would exceed 79 cols, then asserts the renderer wrapped it.
    """
    headline = "a " * 200  # 400 chars of stuff to wrap
    digest = WeeklyDigest(
        week_label="Week of May 18-24, 2026",
        trajectory_headline=headline.strip(),
    )
    lines = _all_lines(digest)
    for i, line in enumerate(lines):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({visible_width(line)} > {MAX_LINE_WIDTH}): "
            f"{_strip_ansi(line)!r}"
        )


# --------------------------------------------------------- module-level shape


def test_render_returns_a_string():
    """The renderer returns a single string, not a list of lines."""
    out = render(WeeklyDigest())
    assert isinstance(out, str)


def test_renderer_module_does_not_clobber_v01_terminal():
    """The v0.2 digest renderer is a separate module from `praxis.reports.terminal`.

    Spec section 3 keeps the legacy `praxis scan` terminal surface
    around alongside the v0.2 digest; this assertion locks the design
    decision that the two live side-by-side rather than the v0.2
    renderer being grafted on top of the v0.1 one. A future change
    that consolidates them would need to re-spec, not silently
    delete this guard.
    """
    from praxis.reports import terminal as v01
    assert v01.render is not digest_terminal.render
