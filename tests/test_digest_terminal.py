"""Tests for the v0.2 terminal digest renderer.

Covers US-066 (80-column hard cap) and US-067 (the three mandatory
sections always render, with a clear placeholder when an upstream
input is missing). Later stories (US-068/069) will add their own
section-specific tests on top of the contracts locked in here.
"""
from __future__ import annotations

import re

from praxis.reports import digest_terminal
from praxis.reports.digest_terminal import (
    MAX_LINE_WIDTH,
    FollowUpView,
    HeadlineMomentView,
    WeeklyDigest,
    render,
    visible_width,
)
from praxis.reports.digest_terminal import (
    _FOLLOW_UP_PLACEHOLDER,
    _HEADLINE_MOMENT_PLACEHOLDER,
    _TRAJECTORY_PLACEHOLDER,
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


# ----------------------------------------------------- US-067 mandatory sections


# All three section eyebrow strings live here so the assertions below
# stay literal: a future copy change for any one of them would need to
# update this constant in one place.
_MANDATORY_EYEBROWS = ("TRAJECTORY", "HEADLINE MOMENT", "FOLLOW-UP")


def _realistic_headline_moment() -> HeadlineMomentView:
    """Fixture: a plausible moment on the verification dim.

    The strings are sized roughly like real selector output so the
    wrap path exercises naturally. Identifiers in the suggested
    alternative (e.g. ``pytest``) confirm the _wrap helper preserves
    code-style tokens at the 80-column budget.
    """
    return HeadlineMomentView(
        dim_key="verification",
        quoted_excerpt="Implemented and tested, all green.",
        why_it_lost_score=(
            "Claimed the change was verified without showing test output, "
            "and the next session immediately failed on the same path."
        ),
        suggested_alternative=(
            "Run `pytest` and paste the actual output before saying "
            "'all green'; one specific failing path beats a confident summary."
        ),
    )


def _realistic_follow_up_pending() -> FollowUpView:
    return FollowUpView(
        commitment_text=(
            "Run `pytest` and paste the actual output before saying "
            "'all green'."
        ),
        target_metric="verification_rate",
        baseline_value=0.32,
        measured_value=None,
        outcome="pending",
    )


def _realistic_follow_up_closed() -> FollowUpView:
    return FollowUpView(
        commitment_text=(
            "Run `pytest` and paste the actual output before saying "
            "'all green'."
        ),
        target_metric="verification_rate",
        baseline_value=0.32,
        measured_value=0.58,
        outcome="improved",
    )


def _full_digest() -> WeeklyDigest:
    return WeeklyDigest(
        week_label="Week of May 18-24, 2026",
        trajectory_headline=(
            "Engagement up 0.18/wk, delegation down 0.11/wk over 8 "
            "weeks. You're investing more cognition per session, not less."
        ),
        headline_moment=_realistic_headline_moment(),
        follow_up=_realistic_follow_up_pending(),
    )


def test_all_mandatory_sections_render_with_data():
    """Spec section 6.2: trajectory + headline moment + follow-up render.

    With full inputs, every eyebrow shows up exactly once in the output.
    """
    text = _strip_ansi(render(_full_digest()))
    for eyebrow in _MANDATORY_EYEBROWS:
        assert text.count(eyebrow) == 1, (
            f"expected eyebrow {eyebrow!r} exactly once; got {text.count(eyebrow)} "
            f"in:\n{text}"
        )


def test_all_mandatory_sections_render_when_inputs_missing():
    """A digest with no section data still emits all three eyebrows.

    The placeholder copy stands in for the missing input so the digest's
    structural shape is preserved (spec section 6.2: mandatory sections
    are mandatory, not 'conditional on data').
    """
    text = _strip_ansi(render(WeeklyDigest()))
    for eyebrow in _MANDATORY_EYEBROWS:
        assert eyebrow in text, f"missing eyebrow {eyebrow!r} in:\n{text}"


def test_placeholders_appear_when_inputs_missing():
    """Each mandatory section emits its exact placeholder copy when None."""
    text = _strip_ansi(render(WeeklyDigest()))
    assert _TRAJECTORY_PLACEHOLDER in text
    assert _HEADLINE_MOMENT_PLACEHOLDER in text
    assert _FOLLOW_UP_PLACEHOLDER in text


def test_placeholders_absent_when_inputs_present():
    """When real data is provided, the placeholder copy does NOT leak in.

    Guards against a future refactor that accidentally always emits the
    placeholder alongside real content.
    """
    text = _strip_ansi(render(_full_digest()))
    assert _TRAJECTORY_PLACEHOLDER not in text
    assert _HEADLINE_MOMENT_PLACEHOLDER not in text
    assert _FOLLOW_UP_PLACEHOLDER not in text


def test_render_does_not_crash_on_empty_digest():
    """Spec section 6.2: 'a clear placeholder ... rather than crashing'.

    Locks the structural guarantee even if the placeholder strings
    above are renamed: render(WeeklyDigest()) must return a string.
    """
    out = render(WeeklyDigest())
    assert isinstance(out, str)
    assert out  # not the empty string


def test_partial_inputs_render_other_sections_as_placeholders():
    """Only one section populated: the other two get placeholders.

    Tests the per-section independence of the placeholder fallback so
    the renderer doesn't all-or-nothing on a missing input.
    """
    digest = WeeklyDigest(
        headline_moment=_realistic_headline_moment(),
    )
    text = _strip_ansi(render(digest))
    assert _TRAJECTORY_PLACEHOLDER in text
    assert _HEADLINE_MOMENT_PLACEHOLDER not in text  # real moment is rendered
    assert _FOLLOW_UP_PLACEHOLDER in text


def test_headline_moment_renders_quote_and_alternative():
    """The headline moment shows the dim title, the quote, and the 'Try:' line.

    These are the three pieces of information a coachable moment needs
    to be actionable; if any one drops out, the section becomes noise.
    """
    moment = _realistic_headline_moment()
    digest = WeeklyDigest(headline_moment=moment)
    text = _strip_ansi(render(digest))
    # The rubric resolves "verification" to "Verification habits".
    assert "Verification habits" in text
    assert moment.quoted_excerpt in text
    # "Try:" prefix and the start of the suggested alternative.
    assert "Try: " in text
    assert "Run `pytest`" in text


def test_follow_up_pending_shows_baseline_only():
    """A pending follow-up shows the commitment and baseline, no measured.

    Pending means next week hasn't closed it yet; showing a measured
    value would be a fiction. The outcome label 'pending' is included.
    """
    digest = WeeklyDigest(follow_up=_realistic_follow_up_pending())
    text = _strip_ansi(render(digest))
    assert "Commit: " in text
    assert "verification_rate" in text
    assert "0.32" in text
    assert "pending" in text
    # The measured-value arrow must NOT appear before close.
    assert "->" not in text


def test_follow_up_closed_shows_baseline_and_measured():
    """A closed follow-up shows baseline -> measured with the outcome.

    Verifies the closed branch of _format_follow_up_tracking.
    """
    digest = WeeklyDigest(follow_up=_realistic_follow_up_closed())
    text = _strip_ansi(render(digest))
    assert "0.32" in text
    assert "0.58" in text
    assert "->" in text
    assert "[improved]" in text


def test_full_digest_lines_under_80():
    """Re-checks the 80-col contract with EVERY mandatory section populated.

    US-066 only exercised the trajectory section; US-067 added two
    more. This test extends the line-width gate to cover all three so
    a future copy or layout change in any section trips the assertion.
    """
    lines = _all_lines(_full_digest())
    over = [
        (i, _strip_ansi(line))
        for i, line in enumerate(lines)
        if visible_width(line) > MAX_LINE_WIDTH
    ]
    assert not over, f"Lines exceed {MAX_LINE_WIDTH} cols: {over}"


def test_empty_digest_with_placeholders_lines_under_80():
    """The default (placeholder-only) digest also fits in the budget.

    Locks the placeholder copy at the budget so a future copy change
    that pushes a placeholder over 75 chars trips this assertion.
    """
    lines = _all_lines(WeeklyDigest())
    over = [
        (i, _strip_ansi(line))
        for i, line in enumerate(lines)
        if visible_width(line) > MAX_LINE_WIDTH
    ]
    assert not over, f"Lines exceed {MAX_LINE_WIDTH} cols: {over}"


def test_long_strings_in_all_sections_still_wrap():
    """Pathological inputs in every section still respect the budget.

    Covers the case where the upstream pipeline emits unusually long
    copy across multiple sections at once; the renderer must keep all
    of them inside the 80-col gate.
    """
    long_words = "alpha " * 200
    digest = WeeklyDigest(
        week_label="Week of May 18-24, 2026",
        trajectory_headline=long_words.strip(),
        headline_moment=HeadlineMomentView(
            dim_key="planning",
            quoted_excerpt=long_words.strip(),
            why_it_lost_score=long_words.strip(),
            suggested_alternative=long_words.strip(),
        ),
        follow_up=FollowUpView(
            commitment_text=long_words.strip(),
            target_metric="planning_dim_mean",
            baseline_value=1.234567,
            measured_value=None,
            outcome="pending",
        ),
    )
    lines = _all_lines(digest)
    for i, line in enumerate(lines):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({visible_width(line)} > {MAX_LINE_WIDTH}): "
            f"{_strip_ansi(line)!r}"
        )


def test_mandatory_sections_render_in_spec_order():
    """Spec section 6.2 lists the sections trajectory -> moment -> follow-up.

    The digest opens with the multi-week behavioral read, then the
    week's most coachable moment, then the commitment that closes the
    loop with next week. A future refactor that reorders these would
    silently change the editorial cadence.
    """
    text = _strip_ansi(render(_full_digest()))
    traj_pos = text.find("TRAJECTORY")
    moment_pos = text.find("HEADLINE MOMENT")
    follow_pos = text.find("FOLLOW-UP")
    assert -1 < traj_pos < moment_pos < follow_pos
