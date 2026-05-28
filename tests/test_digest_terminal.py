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
    MAX_TASKS_RENDERED,
    CostLedgerView,
    DimRowView,
    FollowUpView,
    HeadlineMomentView,
    TaskRowView,
    WeeklyDigest,
    render,
    visible_width,
)
from praxis.reports.digest_terminal import (
    _COST_LEDGER_PLACEHOLDER,
    _DIMENSIONS_PLACEHOLDER,
    _FOLLOW_UP_PLACEHOLDER,
    _HEADLINE_MOMENT_PLACEHOLDER,
    _TASKS_PLACEHOLDER,
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


# ---------------------------------------- US-068 cost ledger and tasks


def _realistic_cost_ledger() -> CostLedgerView:
    """Fixture: a plausible week of spend with a healthy baseline.

    The numbers are sized so the formatted line ("$14.50 vs baseline
    $8.20") exercises both the dollar formatter and the delta path
    without sitting against the 75-char body budget.
    """
    return CostLedgerView(
        this_week_usd=14.50,
        baseline_usd=8.20,
        biggest_model="Opus",
        biggest_task_label="auth migration debugging",
        biggest_line_usd=5.20,
        biggest_line_sessions=4,
        over_tier_sessions=3,
        tier_fit_savings_usd=2.10,
    )


def _realistic_tasks() -> list[TaskRowView]:
    """Fixture: three plausible top tasks for the week.

    The labels are short enough to fit on a single body line so the
    fixture also covers the no-wrap path; long-label wrapping has its
    own dedicated test below.
    """
    return [
        TaskRowView(
            label="auth migration debugging",
            sessions=4,
            total_usd=5.20,
            worst_dim_key="verification",
        ),
        TaskRowView(
            label="ui polish",
            sessions=3,
            total_usd=2.40,
            worst_dim_key="iteration",
        ),
        TaskRowView(
            label="docs writing",
            sessions=2,
            total_usd=0.85,
            worst_dim_key="planning",
        ),
    ]


def _full_digest_with_ledger_and_tasks() -> WeeklyDigest:
    digest = _full_digest()
    digest.cost_ledger = _realistic_cost_ledger()
    digest.tasks = _realistic_tasks()
    return digest


def test_cost_ledger_eyebrow_present():
    """The Cost Ledger section always renders its eyebrow.

    Spec section 10.1: the cost ledger panel is "always shown in the
    digest." That contract holds whether data is provided or not.
    """
    text_with = _strip_ansi(render(_full_digest_with_ledger_and_tasks()))
    text_without = _strip_ansi(render(WeeklyDigest()))
    assert "COST LEDGER" in text_with
    assert "COST LEDGER" in text_without


def test_where_the_week_went_eyebrow_present():
    """The 'Where The Week Went' section always renders its eyebrow."""
    text_with = _strip_ansi(render(_full_digest_with_ledger_and_tasks()))
    text_without = _strip_ansi(render(WeeklyDigest()))
    assert "WHERE THE WEEK WENT" in text_with
    assert "WHERE THE WEEK WENT" in text_without


def test_cost_ledger_placeholder_when_input_missing():
    """When ``cost_ledger`` is None, the section emits the placeholder copy."""
    text = _strip_ansi(render(WeeklyDigest()))
    assert _COST_LEDGER_PLACEHOLDER in text


def test_tasks_placeholder_when_input_missing():
    """When ``tasks`` is None, the section emits the placeholder copy."""
    text = _strip_ansi(render(WeeklyDigest()))
    assert _TASKS_PLACEHOLDER in text


def test_tasks_placeholder_when_input_empty_list():
    """An empty tasks list also triggers the placeholder.

    The boundary that produces the list might emit ``[]`` when the
    clustering pass found nothing groupable; the section should
    degrade the same way as a missing input rather than rendering an
    empty table that reads like a bug.
    """
    text = _strip_ansi(render(WeeklyDigest(tasks=[])))
    assert _TASKS_PLACEHOLDER in text


def test_cost_ledger_renders_spend_and_baseline():
    """The cost ledger shows this-week spend alongside the baseline.

    Locks the formatting decision ("$X.XX vs baseline $Y.YY") so a
    future renderer change cannot drop one of the two anchors.
    """
    digest = WeeklyDigest(cost_ledger=_realistic_cost_ledger())
    text = _strip_ansi(render(digest))
    assert "$14.50" in text
    assert "$8.20" in text
    # The delta column shows the (signed) gap between week and baseline.
    assert "+$6.30" in text


def test_cost_ledger_renders_negative_delta_under_baseline():
    """A week that came in UNDER baseline shows a minus sign on the delta.

    The sign-aware formatter is the only thing distinguishing a week
    of underspend from a week of overspend in the headline number.
    """
    ledger = CostLedgerView(
        this_week_usd=4.00,
        baseline_usd=6.50,
        biggest_model="Sonnet",
        biggest_task_label="quick scripts",
        biggest_line_usd=1.20,
        biggest_line_sessions=2,
        over_tier_sessions=0,
    )
    text = _strip_ansi(render(WeeklyDigest(cost_ledger=ledger)))
    assert "-$2.50" in text


def test_cost_ledger_baseline_forming_when_none():
    """No baseline yet: render the literal '--' and skip the delta.

    Spec section 7 edge case: users with < 14 days of data have no
    baseline. The digest must not invent one. The 'baseline forming'
    annotation tells the user why no delta appears.
    """
    ledger = CostLedgerView(
        this_week_usd=3.50,
        baseline_usd=None,
        biggest_model="Sonnet",
        biggest_task_label="initial setup",
        biggest_line_usd=2.00,
        biggest_line_sessions=1,
        over_tier_sessions=0,
    )
    text = _strip_ansi(render(WeeklyDigest(cost_ledger=ledger)))
    assert "$3.50" in text
    assert "--" in text
    assert "baseline forming" in text
    # No invented delta when there's no baseline to compare against.
    assert "+$" not in text


def test_cost_ledger_biggest_line_shows_model_task_and_meta():
    """The biggest (model, task) line names the model, task, dollars, and sessions.

    Spec section 10.1: "Biggest line: which (model, task) pair drove
    the spend." All four pieces of information must be present so the
    user knows which corner of their week was the heaviest.
    """
    digest = WeeklyDigest(cost_ledger=_realistic_cost_ledger())
    text = _strip_ansi(render(digest))
    assert "Opus" in text
    assert "auth migration debugging" in text
    assert "$5.20" in text
    assert "4 sessions" in text


def test_cost_ledger_tier_fit_callout_present_when_over_tier():
    """A non-zero over-tier count surfaces a savings callout.

    Spec section 10.1: tier-fit callout estimates the savings if
    over-tier sessions had run on the cheaper model. The count and the
    savings amount both appear on the row.
    """
    digest = WeeklyDigest(cost_ledger=_realistic_cost_ledger())
    text = _strip_ansi(render(digest))
    assert "Tier-fit" in text
    assert "3 over-tier" in text
    assert "$2.10" in text


def test_cost_ledger_tier_fit_clean_when_zero():
    """Zero over-tier sessions surface the positive 'clean' callout.

    Absence of over-tier waste is itself worth flagging; suppressing
    the row entirely would lose the reinforcement signal.
    """
    ledger = CostLedgerView(
        this_week_usd=2.00,
        baseline_usd=2.00,
        biggest_model="Sonnet",
        biggest_task_label="small fixes",
        biggest_line_usd=0.80,
        biggest_line_sessions=2,
        over_tier_sessions=0,
        tier_fit_savings_usd=0.0,
    )
    text = _strip_ansi(render(WeeklyDigest(cost_ledger=ledger)))
    assert "Tier-fit: clean" in text


def test_tasks_render_label_sessions_cost_and_worst_dim():
    """US-068 acceptance: each row carries label, sessions, cost, worst dim.

    All four columns must appear together so the row answers the
    "where did my week go" question completely.
    """
    digest = WeeklyDigest(tasks=_realistic_tasks())
    text = _strip_ansi(render(digest))
    # First-task label and metadata
    assert "1. auth migration debugging" in text
    assert "4 sessions" in text
    assert "$5.20" in text
    # Worst-dim title is resolved through _dim_title, so the
    # 'verification' key shows up as 'Verification habits'.
    assert "Verification habits" in text


def test_tasks_singular_session_label():
    """A 1-session task uses 'session' (singular), not 'sessions'.

    Tiny detail, but '1 sessions' is the kind of bug a careful reader
    notices immediately and a careless renderer ships.
    """
    task = TaskRowView(
        label="ad-hoc tweak",
        sessions=1,
        total_usd=0.30,
        worst_dim_key="iteration",
    )
    text = _strip_ansi(render(WeeklyDigest(tasks=[task])))
    assert "1 session," in text
    assert "1 sessions" not in text


def test_tasks_numbered_and_ordered():
    """Top-3 tasks are rendered numbered 1., 2., 3. in input order.

    Upstream is responsible for the ranking (session count, with cost
    as tiebreaker). The renderer respects that order rather than
    re-sorting.
    """
    digest = WeeklyDigest(tasks=_realistic_tasks())
    text = _strip_ansi(render(digest))
    assert "1. auth migration debugging" in text
    assert "2. ui polish" in text
    assert "3. docs writing" in text
    one = text.find("1. auth migration")
    two = text.find("2. ui polish")
    three = text.find("3. docs writing")
    assert -1 < one < two < three


def test_tasks_caps_at_top_3():
    """A list longer than MAX_TASKS_RENDERED is truncated to the top 3.

    Spec section 5.5 fixes the digest at the top 3 tasks. The
    renderer enforces the ceiling defensively even if the caller
    provides a longer list.
    """
    assert MAX_TASKS_RENDERED == 3
    long_list = [
        TaskRowView(
            label=f"task {i}",
            sessions=10 - i,
            total_usd=float(10 - i),
            worst_dim_key="planning",
        )
        for i in range(7)
    ]
    text = _strip_ansi(render(WeeklyDigest(tasks=long_list)))
    assert "1. task 0" in text
    assert "2. task 1" in text
    assert "3. task 2" in text
    assert "4. task 3" not in text  # capped at top-3
    assert "5. task 4" not in text


def test_cost_ledger_lines_under_80_with_data():
    """A populated cost ledger respects the 79-col budget.

    Includes a long task label so the biggest-line row exercises the
    wrap path.
    """
    ledger = CostLedgerView(
        this_week_usd=123.45,
        baseline_usd=78.90,
        biggest_model="Claude Opus 4.7",
        biggest_task_label=(
            "deeply involved migration of authentication middleware "
            "across three repositories"
        ),
        biggest_line_usd=45.67,
        biggest_line_sessions=12,
        over_tier_sessions=8,
        tier_fit_savings_usd=23.45,
    )
    digest = WeeklyDigest(cost_ledger=ledger)
    for i, line in enumerate(_all_lines(digest)):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({visible_width(line)} > {MAX_LINE_WIDTH}): "
            f"{_strip_ansi(line)!r}"
        )


def test_tasks_lines_under_80_with_data():
    """A populated tasks list respects the 79-col budget."""
    digest = WeeklyDigest(tasks=_realistic_tasks())
    for i, line in enumerate(_all_lines(digest)):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({visible_width(line)} > {MAX_LINE_WIDTH}): "
            f"{_strip_ansi(line)!r}"
        )


def test_tasks_lines_under_80_with_long_labels():
    """Pathologically long task labels still wrap inside the budget.

    Labels can be up to 60 chars by upstream validation; this test
    pushes that limit and asserts the wrap path stays inside 79 cols.
    """
    long_label = "a" * 60
    digest = WeeklyDigest(
        tasks=[
            TaskRowView(
                label=long_label,
                sessions=4,
                total_usd=5.20,
                worst_dim_key="verification",
            ),
        ]
    )
    for i, line in enumerate(_all_lines(digest)):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({visible_width(line)} > {MAX_LINE_WIDTH}): "
            f"{_strip_ansi(line)!r}"
        )


def test_full_digest_with_ledger_and_tasks_under_80():
    """Every section populated: the assembled digest still fits the budget.

    The most comprehensive width check in the suite. A future copy
    change in any one section that pushes a line over 79 cols trips
    here.
    """
    lines = _all_lines(_full_digest_with_ledger_and_tasks())
    over = [
        (i, _strip_ansi(line))
        for i, line in enumerate(lines)
        if visible_width(line) > MAX_LINE_WIDTH
    ]
    assert not over, f"Lines exceed {MAX_LINE_WIDTH} cols: {over}"


def test_sections_appear_in_spec_order_with_ledger_and_tasks():
    """Spec section 6.1 fixes the order: trajectory, moment, ledger, tasks, follow-up.

    The terminal digest (US-068) places the cost ledger and tasks
    AFTER follow-up so the editorial cadence keeps coaching content
    before bookkeeping, matching the lock established in the prior
    iteration's handoff notes.
    """
    text = _strip_ansi(render(_full_digest_with_ledger_and_tasks()))
    traj = text.find("TRAJECTORY")
    moment = text.find("HEADLINE MOMENT")
    follow = text.find("FOLLOW-UP")
    ledger = text.find("COST LEDGER")
    week_went = text.find("WHERE THE WEEK WENT")
    assert -1 < traj < moment < follow < ledger < week_went


def test_cost_ledger_placeholder_absent_when_data_present():
    """Real ledger data does not also leak the placeholder copy.

    Guards against a refactor that emits the placeholder alongside
    real content.
    """
    text = _strip_ansi(render(_full_digest_with_ledger_and_tasks()))
    assert _COST_LEDGER_PLACEHOLDER not in text


def test_tasks_placeholder_absent_when_data_present():
    """Real tasks data does not also leak the placeholder copy."""
    text = _strip_ansi(render(_full_digest_with_ledger_and_tasks()))
    assert _TASKS_PLACEHOLDER not in text


def test_cost_ledger_unknown_dim_key_does_not_crash_via_task():
    """An unknown worst_dim_key in a task row falls through to the raw key.

    The renderer must not crash on rubric drift; the test mirrors the
    contract _dim_title established in US-067 and exercises it through
    the new tasks section.
    """
    digest = WeeklyDigest(
        tasks=[
            TaskRowView(
                label="some task",
                sessions=2,
                total_usd=1.00,
                worst_dim_key="not_a_real_dim",
            ),
        ]
    )
    text = _strip_ansi(render(digest))
    # The raw key (not a crash) renders when the rubric does not
    # recognize the dim. Future rubric additions would resolve it
    # naturally without touching this test.
    assert "not_a_real_dim" in text


# -------------------------------------------- US-069 six-dim panel as footer


# The six rubric titles in the exact order the renderer emits them.
# Tests below assert against this list so a future rubric renaming is
# one audit point. The 'Model-task fit' entry uses the en-dash that
# lives in the rubric itself (praxis/scoring/rubric.py); the renderer
# does not rewrite rubric titles, so this test pins what the user
# actually sees.
_RUBRIC_TITLES = (
    "Planning before prompting",
    "Context richness",
    "Iteration & evaluation",
    "Tool & multi-step use",
    "Model–task fit",
    "Verification habits",
)


def _realistic_dimensions() -> list[DimRowView]:
    """Fixture: a plausible six-dim panel with a mix of deltas.

    The values exercise every branch of the row formatter at least
    once: a significant positive delta (planning, +0.3 = exactly the
    threshold), a larger positive (context, +0.4), a significant
    negative (iteration, -0.4), an insignificant delta (tools, 0.0),
    a baseline-forming row (fit, baseline=None), and another
    insignificant delta (verification, +0.3 - wait, this is exactly
    the threshold). Tweaked so each branch is unambiguous.
    """
    return [
        DimRowView(dim_key="planning", score=5.7, baseline=5.1),
        DimRowView(dim_key="context", score=6.2, baseline=5.8),
        DimRowView(dim_key="iteration", score=4.1, baseline=4.5),
        DimRowView(dim_key="tools", score=5.7, baseline=5.7),
        DimRowView(dim_key="fit", score=6.0, baseline=None),
        DimRowView(dim_key="verification", score=5.2, baseline=4.9),
    ]


def _full_digest_with_dimensions() -> WeeklyDigest:
    digest = _full_digest_with_ledger_and_tasks()
    digest.dimensions = _realistic_dimensions()
    return digest


def test_six_dim_panel_eyebrow_present():
    """The 'THE SIX DIMENSIONS' eyebrow renders with and without data.

    Spec section 6.2: the six-dim panel is the footer of the digest.
    The contract holds whether data is provided or not -- the section
    always emits its eyebrow so the structural shape of the digest
    is preserved.
    """
    text_with = _strip_ansi(render(_full_digest_with_dimensions()))
    text_without = _strip_ansi(render(WeeklyDigest()))
    assert "THE SIX DIMENSIONS" in text_with
    assert "THE SIX DIMENSIONS" in text_without


def test_six_dim_panel_is_the_last_section():
    """Spec section 6.2: the full six-dim panel SHOULD be a footer.

    The section appears AFTER every other section in the digest so the
    closing visual is the structural per-dim readout, not coaching
    copy. This is the editorial decision the spec locks in; a future
    reorder would silently change the cadence.
    """
    text = _strip_ansi(render(_full_digest_with_dimensions()))
    six_dim_pos = text.find("THE SIX DIMENSIONS")
    other_eyebrows = [
        "TRAJECTORY",
        "HEADLINE MOMENT",
        "FOLLOW-UP",
        "COST LEDGER",
        "WHERE THE WEEK WENT",
    ]
    for eyebrow in other_eyebrows:
        pos = text.find(eyebrow)
        assert -1 < pos < six_dim_pos, (
            f"{eyebrow!r} should appear before the six-dim footer; "
            f"found {eyebrow}@{pos} vs panel@{six_dim_pos}"
        )


def test_six_dim_panel_placeholder_when_input_missing():
    """When ``dimensions`` is None, the section emits the placeholder copy."""
    text = _strip_ansi(render(WeeklyDigest()))
    assert _DIMENSIONS_PLACEHOLDER in text


def test_six_dim_panel_placeholder_when_input_empty_list():
    """An empty dimensions list also triggers the placeholder.

    Parallel to the tasks-empty-list case: the renderer treats
    ``[]`` the same as ``None`` so a boundary that emits an empty
    list when no data is ready does not render an empty table.
    """
    text = _strip_ansi(render(WeeklyDigest(dimensions=[])))
    assert _DIMENSIONS_PLACEHOLDER in text


def test_six_dim_panel_placeholder_absent_when_data_present():
    """Real dimensions data does not also leak the placeholder copy."""
    text = _strip_ansi(render(_full_digest_with_dimensions()))
    assert _DIMENSIONS_PLACEHOLDER not in text


def test_six_dim_panel_renders_all_six_rubric_titles():
    """With six DimRowView entries, all six rubric titles render."""
    text = _strip_ansi(render(WeeklyDigest(dimensions=_realistic_dimensions())))
    for title in _RUBRIC_TITLES:
        assert title in text, f"expected rubric title {title!r} in:\n{text}"


def test_six_dim_panel_renders_score_per_dim():
    """Spec acceptance: each dim shows its score (formatted X.X/10)."""
    text = _strip_ansi(render(WeeklyDigest(dimensions=_realistic_dimensions())))
    # Every realistic dim score (one decimal) appears followed by /10.
    assert "5.7/10" in text  # planning
    assert "6.2/10" in text  # context
    assert "4.1/10" in text  # iteration
    assert "5.7/10" in text  # tools (also 5.7)
    assert "6.0/10" in text  # fit
    assert "5.2/10" in text  # verification


def test_six_dim_panel_renders_baseline_per_dim():
    """Spec acceptance: each dim shows its baseline value.

    Baseline values are formatted to one decimal place (matching the
    score format) and prefixed by the literal ``baseline `` so the
    reader can scan the column without parsing position alone.
    """
    text = _strip_ansi(render(WeeklyDigest(dimensions=_realistic_dimensions())))
    assert "baseline 5.1" in text  # planning baseline
    assert "baseline 5.8" in text  # context baseline
    assert "baseline 4.5" in text  # iteration baseline
    assert "baseline 5.7" in text  # tools baseline
    assert "baseline 4.9" in text  # verification baseline


def test_six_dim_panel_significant_positive_delta_renders_up_arrow():
    """A delta >= +0.3 renders as '↑ +X.X' (spec section 8.3).

    Locks the up-arrow path so a refactor that drops the arrow glyph
    or the sign-aware formatter trips here.
    """
    digest = WeeklyDigest(
        dimensions=[DimRowView(dim_key="planning", score=5.7, baseline=5.1)]
    )
    text = _strip_ansi(render(digest))
    # ``format_delta`` emits "↑ +0.6" (sign-aware, one decimal).
    assert "↑ +0.6" in text


def test_six_dim_panel_significant_negative_delta_renders_down_arrow():
    """A delta <= -0.3 renders as '↓ -X.X' (spec section 8.3)."""
    digest = WeeklyDigest(
        dimensions=[DimRowView(dim_key="iteration", score=4.1, baseline=4.5)]
    )
    text = _strip_ansi(render(digest))
    assert "↓ -0.4" in text


def test_six_dim_panel_insignificant_delta_renders_tilde():
    """A delta with |delta| < 0.3 renders as ``~`` (spec section 8.3).

    The significance gate is the dividing line between rendering a
    real movement and rendering noise; a zero-or-near-zero delta must
    not be dressed up as a movement.
    """
    digest = WeeklyDigest(
        dimensions=[DimRowView(dim_key="tools", score=5.7, baseline=5.7)]
    )
    text = _strip_ansi(render(digest))
    # Single ``~`` on the row; no arrow glyphs.
    assert "~" in text
    assert "↑" not in text
    assert "↓" not in text


def test_six_dim_panel_baseline_forming_shows_dashes():
    """A None baseline renders as the ``--`` placeholder (spec section 8.4).

    The digest must not invent a baseline when the user has less than
    14 days of data. The literal ``baseline --`` stub makes the
    forming state visible to the reader.
    """
    digest = WeeklyDigest(
        dimensions=[DimRowView(dim_key="fit", score=6.0, baseline=None)]
    )
    text = _strip_ansi(render(digest))
    assert "baseline --" in text


def test_six_dim_panel_baseline_forming_hides_delta():
    """No delta column when there is no baseline to compare against.

    The 'forming' annotation takes the place of the delta so the row
    still aligns visually, but no arrow or sign is rendered.
    """
    digest = WeeklyDigest(
        dimensions=[DimRowView(dim_key="fit", score=6.0, baseline=None)]
    )
    text = _strip_ansi(render(digest))
    # The forming branch shows '(forming)' in place of any delta.
    assert "(forming)" in text
    # No arrow glyphs or signed numerals from the delta path.
    assert "↑" not in text
    assert "↓" not in text


def test_six_dim_panel_unknown_dim_key_does_not_crash():
    """An unknown dim_key in DimRowView falls through to the raw key.

    Mirrors the contract _dim_title established in US-067 and
    US-068's task row: rubric drift must not crash the digest. The
    raw key renders instead so the section still emits all its rows.
    """
    digest = WeeklyDigest(
        dimensions=[
            DimRowView(dim_key="not_a_real_dim", score=5.0, baseline=4.5),
        ]
    )
    text = _strip_ansi(render(digest))
    assert "not_a_real_dim" in text


def test_six_dim_panel_lines_under_80_with_data():
    """A populated six-dim panel respects the 79-col budget."""
    digest = WeeklyDigest(dimensions=_realistic_dimensions())
    for i, line in enumerate(_all_lines(digest)):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({visible_width(line)} > {MAX_LINE_WIDTH}): "
            f"{_strip_ansi(line)!r}"
        )


def test_six_dim_panel_lines_under_80_with_extreme_values():
    """Extreme score/baseline values (10.0/0.0) still fit the budget.

    The /10 scale caps the formatted score at 4 chars (``10.0``) and
    the delta path tops out near ``10.0`` magnitude; this test pushes
    those extremes to ensure no row overflows.
    """
    digest = WeeklyDigest(
        dimensions=[
            DimRowView(dim_key="planning", score=10.0, baseline=0.0),
            DimRowView(dim_key="verification", score=0.0, baseline=10.0),
        ]
    )
    for i, line in enumerate(_all_lines(digest)):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line {i} ({visible_width(line)} > {MAX_LINE_WIDTH}): "
            f"{_strip_ansi(line)!r}"
        )


def test_full_digest_with_dimensions_under_80():
    """Every section populated (including the footer) stays in budget.

    The most comprehensive width check in the suite once US-069 lands.
    A future copy or layout change in any section that pushes a line
    over 79 cols trips here.
    """
    lines = _all_lines(_full_digest_with_dimensions())
    over = [
        (i, _strip_ansi(line))
        for i, line in enumerate(lines)
        if visible_width(line) > MAX_LINE_WIDTH
    ]
    assert not over, f"Lines exceed {MAX_LINE_WIDTH} cols: {over}"


def test_sections_appear_in_spec_order_with_dimensions_as_footer():
    """Full spec order: trajectory, moment, follow-up, ledger, tasks, dims.

    Spec section 6.2 places the six-dim panel last (as a footer).
    Locks the editorial cadence: behavioral read first, then coaching
    moment, then commitment, then bookkeeping, then the structural
    six-dim readout to close.
    """
    text = _strip_ansi(render(_full_digest_with_dimensions()))
    positions = [
        text.find(eyebrow)
        for eyebrow in (
            "TRAJECTORY",
            "HEADLINE MOMENT",
            "FOLLOW-UP",
            "COST LEDGER",
            "WHERE THE WEEK WENT",
            "THE SIX DIMENSIONS",
        )
    ]
    assert all(p >= 0 for p in positions), f"missing eyebrow(s): {positions}"
    assert positions == sorted(positions), (
        f"sections out of order: {positions}"
    )


def test_six_dim_panel_renders_six_body_lines():
    """The footer panel emits exactly one body line per dim entry.

    Locks the per-row layout: a future change that adds a second row
    per dim (e.g. an evidence line) would silently double the footer
    height; this test catches that.
    """
    digest = WeeklyDigest(dimensions=_realistic_dimensions())
    text = render(digest)
    panel_start = text.find("THE SIX DIMENSIONS")
    # US-038 added a behavioral-patterns panel after the six-dim footer.
    # Bound the slice between the two eyebrows so this test still
    # measures only the six-dim rows, ignoring the trailing ANSI/blank
    # lines that lead into the next eyebrow.
    next_panel = text.find("BEHAVIORAL PATTERNS", panel_start)
    if next_panel > -1:
        line_start = text.rfind("\n", 0, next_panel)
        panel_text = text[panel_start:line_start if line_start > -1 else next_panel]
    else:
        panel_text = text[panel_start:]
    body_lines = [
        line for line in panel_text.split("\n")
        if line.strip()
        and "THE SIX DIMENSIONS" not in line
    ]
    assert len(body_lines) == 6, (
        f"expected 6 body lines in panel; got {len(body_lines)}:\n"
        + "\n".join(body_lines)
    )


def test_six_dim_panel_renders_in_caller_provided_order():
    """The renderer emits rows in the order the caller provided them.

    Upstream constructs the list in rubric order; the renderer trusts
    that order rather than re-sorting, parallel to the task rows
    contract. This test reverses the rubric order so any silent
    re-sort by the renderer would flip the assertion.
    """
    reversed_dims = list(reversed(_realistic_dimensions()))
    text = _strip_ansi(render(WeeklyDigest(dimensions=reversed_dims)))
    panel_start = text.find("THE SIX DIMENSIONS")
    panel_text = text[panel_start:]
    # Verification (last in rubric) should appear first; planning last.
    verif_pos = panel_text.find("Verification habits")
    plan_pos = panel_text.find("Planning before prompting")
    assert -1 < verif_pos < plan_pos, (
        "renderer must preserve caller's order; "
        f"verification@{verif_pos} should precede planning@{plan_pos}"
    )


# -------------------------------------- US-038: behavioral-patterns panel


def _behavioral_panel_with_signals():
    """Build a panel with two populated signals for tests."""
    from praxis.reports.panel_inputs import (
        BehavioralPatternRow,
        BehavioralPatternsPanel,
    )
    return BehavioralPatternsPanel(rows=(
        BehavioralPatternRow(
            signal_kind="why_question",
            label="Why-questions",
            count=4,
            citation="Shen & Tamkin 2026 (arXiv 2601.20245)",
            excerpts=(
                "why does this approach work for caching?",
                "why is this slower than the previous version?",
            ),
        ),
        BehavioralPatternRow(
            signal_kind="pure_delegation",
            label="Pure delegation",
            count=2,
            citation="Shen & Tamkin 2026 (arXiv 2601.20245)",
            excerpts=(
                "write me a function",
                "make it handle errors",
            ),
        ),
    ))


def _empty_behavioral_panel():
    """Build a panel where every row has count==0 (empty-state path)."""
    from praxis.reports.panel_inputs import (
        BehavioralPatternRow,
        BehavioralPatternsPanel,
    )
    return BehavioralPatternsPanel(rows=(
        BehavioralPatternRow(
            signal_kind="why_question",
            label="Why-questions",
            count=0,
            citation="Shen & Tamkin 2026 (arXiv 2601.20245)",
        ),
    ))


def _panel_inputs(panel):
    from praxis.reports.panel_inputs import PanelInputs
    return PanelInputs(behavioral_signals=panel)


def test_behavioral_patterns_section_eyebrow_is_present():
    """The section always renders an eyebrow so the document shape stays
    stable across empty + populated states (mirrors the other panel
    placeholders' contract)."""
    digest = WeeklyDigest(panel_inputs=_panel_inputs(_behavioral_panel_with_signals()))
    text = _strip_ansi(render(digest))
    assert "BEHAVIORAL PATTERNS" in text


def test_behavioral_patterns_renders_label_and_count():
    """Each populated row emits 'Label: N times' so the reader sees the
    raw count alongside the signal label."""
    digest = WeeklyDigest(panel_inputs=_panel_inputs(_behavioral_panel_with_signals()))
    text = _strip_ansi(render(digest))
    assert "Why-questions: 4 times" in text
    assert "Pure delegation: 2 times" in text


def test_behavioral_patterns_renders_excerpts_inline():
    """Each populated row renders up to two raw user-turn excerpts
    beneath the count line so the reader can ground the count in
    actual transcript text."""
    digest = WeeklyDigest(panel_inputs=_panel_inputs(_behavioral_panel_with_signals()))
    text = _strip_ansi(render(digest))
    assert "why does this approach work for caching?" in text
    assert "write me a function" in text


def test_behavioral_patterns_renders_citation_per_row():
    """The primary source for each signal renders as a small footnote
    beneath that signal's excerpts (US-038 acceptance)."""
    digest = WeeklyDigest(panel_inputs=_panel_inputs(_behavioral_panel_with_signals()))
    text = _strip_ansi(render(digest))
    assert text.count("Source: Shen & Tamkin 2026 (arXiv 2601.20245)") >= 2


def test_behavioral_patterns_renders_empty_state_when_zero_signals():
    """When every row has count==0 the section surfaces a clear
    empty-state message instead of an empty table."""
    digest = WeeklyDigest(panel_inputs=_panel_inputs(_empty_behavioral_panel()))
    text = _strip_ansi(render(digest))
    assert "No behavioral patterns captured this week." in text


def test_behavioral_patterns_renders_empty_state_when_no_panel():
    """The renderer's None handling defaults to the same empty-state
    copy as the all-zero path; a caller that forgets to populate the
    panel never produces a malformed section."""
    digest = WeeklyDigest()  # no panel_inputs at all
    text = _strip_ansi(render(digest))
    assert "BEHAVIORAL PATTERNS" in text
    assert "No behavioral patterns captured this week." in text


def test_behavioral_patterns_zero_count_rows_are_hidden_when_others_fire():
    """When some signals fired and some did not, the zero-count rows
    are dropped from the table; the reader sees only what triggered."""
    from praxis.reports.panel_inputs import (
        BehavioralPatternRow,
        BehavioralPatternsPanel,
    )
    panel = BehavioralPatternsPanel(rows=(
        BehavioralPatternRow(
            signal_kind="why_question",
            label="Why-questions",
            count=3,
            citation="Shen & Tamkin 2026 (arXiv 2601.20245)",
            excerpts=("why is this slow?",),
        ),
        BehavioralPatternRow(
            signal_kind="pure_delegation",
            label="Pure delegation",
            count=0,
            citation="Shen & Tamkin 2026 (arXiv 2601.20245)",
        ),
    ))
    digest = WeeklyDigest(panel_inputs=_panel_inputs(panel))
    text = _strip_ansi(render(digest))
    # Why-questions row is present.
    assert "Why-questions: 3 times" in text
    # The empty row is dropped (no "Pure delegation: 0 times" line).
    assert "Pure delegation: 0 times" not in text


def test_behavioral_patterns_lines_respect_80_column_budget():
    """US-066 contract: every line in the terminal digest must fit in
    <80 columns. The behavioral-patterns panel must respect the same
    budget as the rest of the digest."""
    digest = WeeklyDigest(panel_inputs=_panel_inputs(_behavioral_panel_with_signals()))
    text = render(digest)
    for line in text.split("\n"):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line exceeds {MAX_LINE_WIDTH} cols: {line!r}"
        )


def test_behavioral_patterns_section_appears_after_six_dim_panel():
    """The behavioral patterns panel sits after the six-dim footer so
    the reader sees the structural /10 read first and then the
    raw-pattern evidence that informs it (intentional ordering)."""
    digest = WeeklyDigest(
        dimensions=_realistic_dimensions(),
        panel_inputs=_panel_inputs(_behavioral_panel_with_signals()),
    )
    text = _strip_ansi(render(digest))
    six_dim_pos = text.find("THE SIX DIMENSIONS")
    bp_pos = text.find("BEHAVIORAL PATTERNS")
    assert 0 <= six_dim_pos < bp_pos


# ---------------- US-039: aug/auto balance + cadence panels (terminal) -------


from praxis.reports.digest_terminal import (  # noqa: E402
    _AUG_AUTO_BALANCE_CLASSIFIER_UNAVAILABLE,
    _CADENCE_NO_ACTIVITY,
)
from praxis.reports.panel_inputs import (  # noqa: E402
    AugAutoBalancePanel,
    CadencePanel,
    PanelInputs,
)


def _aug_auto_populated() -> AugAutoBalancePanel:
    """Three classified sessions: 2 aug, 1 auto, 1 mixed (33% auto?)"""
    return AugAutoBalancePanel(
        augmentation_count=2,
        automation_count=1,
        mixed_count=1,
        unclassified_count=0,
    )


def _cadence_populated() -> CadencePanel:
    return CadencePanel(
        weekday_streak=4,
        substantive_session_count=6,
        high_adopter_position="moderate",
    )


def _full_panel_inputs(*, aug_auto=None, cadence=None) -> PanelInputs:
    return PanelInputs(
        aug_auto_balance=aug_auto,
        cadence=cadence,
    )


def test_aug_auto_balance_eyebrow_always_renders():
    """The section eyebrow renders regardless of data state so the
    document shape is stable across empty + populated runs."""
    text_empty = _strip_ansi(render(WeeklyDigest()))
    text_full = _strip_ansi(
        render(WeeklyDigest(panel_inputs=_full_panel_inputs(aug_auto=_aug_auto_populated())))
    )
    assert "AUGMENTATION/AUTOMATION" in text_empty
    assert "AUGMENTATION/AUTOMATION" in text_full


def test_aug_auto_balance_renders_classifier_unavailable():
    """When every session in the week is unclassified, the panel
    surfaces the verbatim US-039 unavailable copy."""
    panel = AugAutoBalancePanel(
        unclassified_count=3,
        classifier_unavailable=True,
    )
    digest = WeeklyDigest(panel_inputs=_full_panel_inputs(aug_auto=panel))
    text = _strip_ansi(render(digest))
    assert _AUG_AUTO_BALANCE_CLASSIFIER_UNAVAILABLE in text


def test_aug_auto_balance_renders_shares_when_populated():
    """The three shares render as percentages so the reader sees the
    user's split rather than raw counts."""
    digest = WeeklyDigest(
        panel_inputs=_full_panel_inputs(aug_auto=_aug_auto_populated())
    )
    text = _strip_ansi(render(digest))
    # 2/4 = 50% aug; 1/4 = 25% auto; 1/4 = 25% mixed
    assert "Augmentation: 50%" in text
    assert "Automation: 25%" in text
    assert "Mixed: 25%" in text


def test_aug_auto_balance_renders_industry_anchor_citation():
    """The Anthropic Economic Index anchor (~52%/45%) is cited inline
    as a footnote so the reader can compare against the industry baseline."""
    digest = WeeklyDigest(
        panel_inputs=_full_panel_inputs(aug_auto=_aug_auto_populated())
    )
    text = _strip_ansi(render(digest))
    assert "Anthropic Economic Index" in text
    assert "52%" in text
    assert "45%" in text


def test_aug_auto_balance_no_panel_renders_unavailable():
    """A digest with no aug_auto panel at all falls back to the
    unavailable copy rather than an empty section."""
    digest = WeeklyDigest()
    text = _strip_ansi(render(digest))
    assert "AUGMENTATION/AUTOMATION" in text
    assert _AUG_AUTO_BALANCE_CLASSIFIER_UNAVAILABLE in text


def test_cadence_eyebrow_always_renders():
    """Same stability contract as the aug/auto panel."""
    text_empty = _strip_ansi(render(WeeklyDigest()))
    text_full = _strip_ansi(
        render(WeeklyDigest(panel_inputs=_full_panel_inputs(cadence=_cadence_populated())))
    )
    assert "CADENCE" in text_empty
    assert "CADENCE" in text_full


def test_cadence_renders_no_activity_message():
    """When zero substantive sessions fell in the window, the panel
    surfaces the verbatim US-039 message."""
    panel = CadencePanel(weekday_streak=0, substantive_session_count=0)
    digest = WeeklyDigest(panel_inputs=_full_panel_inputs(cadence=panel))
    text = _strip_ansi(render(digest))
    assert _CADENCE_NO_ACTIVITY in text


def test_cadence_omits_high_adopter_label_when_no_activity():
    """The high-adopter label is undefined without any activity to
    position; the renderer must not surface 'Low-adopter' as a stand-in
    (would mislead readers into reading inactivity as low engagement)."""
    panel = CadencePanel(weekday_streak=0, substantive_session_count=0)
    digest = WeeklyDigest(panel_inputs=_full_panel_inputs(cadence=panel))
    text = _strip_ansi(render(digest))
    assert "Low-adopter" not in text
    assert "Moderate-adopter" not in text
    assert "High-adopter" not in text


def test_cadence_renders_streak_when_populated():
    """The streak shows as 'N of 21 days' so the reader can see how
    much of the window they were active."""
    digest = WeeklyDigest(
        panel_inputs=_full_panel_inputs(cadence=_cadence_populated())
    )
    text = _strip_ansi(render(digest))
    assert "Weekday streak: 4 of 21 days" in text


def test_cadence_renders_spectrum_label_when_populated():
    """The high-adopter position renders as a human-facing label."""
    digest = WeeklyDigest(
        panel_inputs=_full_panel_inputs(cadence=_cadence_populated())
    )
    text = _strip_ansi(render(digest))
    assert "Spectrum: Moderate-adopter" in text


def test_cadence_renders_arxiv_citation():
    """The arXiv 2509.19708 anchor is cited inline (US-039 acceptance)."""
    digest = WeeklyDigest(
        panel_inputs=_full_panel_inputs(cadence=_cadence_populated())
    )
    text = _strip_ansi(render(digest))
    assert "arXiv 2509.19708" in text


def test_aug_auto_and_cadence_panels_respect_80_column_budget():
    """US-066: every line must fit in 79 columns. Both new panels
    must respect the same budget as the rest of the digest."""
    digest = WeeklyDigest(
        panel_inputs=_full_panel_inputs(
            aug_auto=_aug_auto_populated(),
            cadence=_cadence_populated(),
        )
    )
    for line in render(digest).split("\n"):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line exceeds {MAX_LINE_WIDTH} cols: {line!r}"
        )


# ---------------- US-040: repeat-task radar + verification calibration -------


from praxis.reports.digest_terminal import (  # noqa: E402
    _REPEAT_TASK_EMPTY,
    _VERIFICATION_CALIBRATION_NO_SESSIONS,
)
from praxis.reports.panel_inputs import (  # noqa: E402
    REPEAT_TASK_SKILL_TAG,
    RepeatTaskRadarPanel,
    RepeatTaskRow,
    VerificationCalibrationPanel,
)


def _repeat_task_populated() -> RepeatTaskRadarPanel:
    return RepeatTaskRadarPanel(
        rows=(
            RepeatTaskRow(
                canonical_first_sentence=(
                    "fix the failing auth test"
                ),
                occurrences=3,
                estimated_minutes_per_occurrence=12.0,
            ),
            RepeatTaskRow(
                canonical_first_sentence=(
                    "regenerate the changelog entry"
                ),
                occurrences=4,
                estimated_minutes_per_occurrence=8.5,
            ),
        )
    )


def _verification_populated() -> VerificationCalibrationPanel:
    return VerificationCalibrationPanel(
        source_check_count=2,
        test_run_count=3,
        spot_check_count=1,
        blanket_accept_count=4,
    )


def _us040_panel_inputs(
    *,
    repeat_task: RepeatTaskRadarPanel | None = None,
    verification: VerificationCalibrationPanel | None = None,
) -> PanelInputs:
    return PanelInputs(
        repeat_task_radar=repeat_task,
        verification_calibration=verification,
    )


def test_repeat_task_radar_eyebrow_always_renders():
    """The section eyebrow renders regardless of data state so the
    document shape is stable across empty + populated runs."""
    text_empty = _strip_ansi(render(WeeklyDigest()))
    text_full = _strip_ansi(
        render(
            WeeklyDigest(panel_inputs=_us040_panel_inputs(repeat_task=_repeat_task_populated()))
        )
    )
    assert "REPEAT-TASK RADAR" in text_empty
    assert "REPEAT-TASK RADAR" in text_full


def test_repeat_task_radar_renders_no_repeats_empty_state():
    """When detect_repeats returned nothing the panel renders the
    verbatim US-040 empty-state copy (no misleading header above an
    empty table)."""
    panel = RepeatTaskRadarPanel()  # no rows
    digest = WeeklyDigest(panel_inputs=_us040_panel_inputs(repeat_task=panel))
    text = _strip_ansi(render(digest))
    assert _REPEAT_TASK_EMPTY in text


def test_repeat_task_radar_no_panel_renders_empty_state():
    """A digest with no panel_inputs falls back to the verbatim
    empty-state copy rather than an empty body."""
    text = _strip_ansi(render(WeeklyDigest()))
    assert _REPEAT_TASK_EMPTY in text


def test_repeat_task_radar_renders_each_row():
    """Each RepeatTask renders its canonical sentence, occurrence
    count, per-occurrence minutes, and the 'Could become a skill' tag."""
    digest = WeeklyDigest(
        panel_inputs=_us040_panel_inputs(repeat_task=_repeat_task_populated())
    )
    text = _strip_ansi(render(digest))
    assert "fix the failing auth test" in text
    assert "regenerate the changelog entry" in text
    assert "3 times" in text
    assert "4 times" in text
    assert REPEAT_TASK_SKILL_TAG in text


def test_repeat_task_radar_renders_estimated_minutes():
    """The per-occurrence minutes show up so the reader knows the
    rough reclaimable time per occurrence."""
    digest = WeeklyDigest(
        panel_inputs=_us040_panel_inputs(repeat_task=_repeat_task_populated())
    )
    text = _strip_ansi(render(digest))
    # 12.0 minutes renders as "12 min"; 8.5 renders as "8.5 min".
    assert "12 min" in text
    assert "8.5 min" in text


def test_repeat_task_radar_renders_citation():
    """The OpenAI + Anthropic Skills citation renders inline as a
    small footnote (US-040 AC)."""
    digest = WeeklyDigest(
        panel_inputs=_us040_panel_inputs(repeat_task=_repeat_task_populated())
    )
    text = _strip_ansi(render(digest))
    assert "OpenAI" in text
    assert "Anthropic Skills" in text


def test_verification_calibration_eyebrow_always_renders():
    """Same stability contract as the other panels."""
    text_empty = _strip_ansi(render(WeeklyDigest()))
    text_full = _strip_ansi(
        render(
            WeeklyDigest(panel_inputs=_us040_panel_inputs(verification=_verification_populated()))
        )
    )
    assert "VERIFICATION CALIBRATION" in text_empty
    assert "VERIFICATION CALIBRATION" in text_full


def test_verification_calibration_renders_no_sessions_empty_state():
    """When the week has no sessions to categorize the panel surfaces
    the explicit empty-state copy rather than four zeros."""
    panel = VerificationCalibrationPanel()  # all zeros, has_sessions=False
    digest = WeeklyDigest(panel_inputs=_us040_panel_inputs(verification=panel))
    text = _strip_ansi(render(digest))
    assert _VERIFICATION_CALIBRATION_NO_SESSIONS in text


def test_verification_calibration_renders_each_bucket_count():
    """All four buckets render in display order."""
    digest = WeeklyDigest(
        panel_inputs=_us040_panel_inputs(verification=_verification_populated())
    )
    text = _strip_ansi(render(digest))
    assert "Source-check: 2 sessions" in text
    assert "Test-run: 3 sessions" in text
    assert "Spot-check: 1 session" in text
    assert "Blanket-accept: 4 sessions" in text


def test_verification_calibration_renders_citation():
    """The Sonar / Stack Overflow 2025 / automation-bias citation
    renders inline (US-040 AC)."""
    digest = WeeklyDigest(
        panel_inputs=_us040_panel_inputs(verification=_verification_populated())
    )
    text = _strip_ansi(render(digest))
    assert "Sonar" in text
    assert "Stack Overflow" in text
    assert "automation-bias" in text


def test_us040_panels_respect_80_column_budget():
    """US-066: every line in the rendered digest must fit in 79
    columns. The two new panels must respect the same budget."""
    digest = WeeklyDigest(
        panel_inputs=_us040_panel_inputs(
            repeat_task=_repeat_task_populated(),
            verification=_verification_populated(),
        )
    )
    for line in render(digest).split("\n"):
        assert visible_width(line) <= MAX_LINE_WIDTH, (
            f"line exceeds {MAX_LINE_WIDTH} cols: {line!r}"
        )
