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
    FollowUpView,
    HeadlineMomentView,
    TaskRowView,
    WeeklyDigest,
    render,
    visible_width,
)
from praxis.reports.digest_terminal import (
    _COST_LEDGER_PLACEHOLDER,
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
