"""Tests for the v0.2 weekly digest HTML renderer.

US-061 acceptance criteria (spec section 13.1):
  - Generated HTML contains no external CSS, JS, font, or image references.
  - Double-clicking the file renders correctly in a default browser
    (smoke-checked here by asserting the output is a valid HTML document
    with inline CSS and no required network fetches).

US-062 acceptance criteria (spec section 6.1):
  - Sections appear in this order: trajectory headline + confidence band,
    This Week's Moment, Cost Ledger, Where The Week Went, The Six
    Dimensions, Follow-up From Last Week, One Thing To Try Next Week.
  - The masthead does not lead with the overall /10.

These tests build a minimal ``WeeklyDigest`` by hand rather than going
through the full weekly pipeline, so they stay fast and pure.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone

import pytest

from praxis.reports.digest_html import (
    CostLedger,
    DimRow,
    FollowUpPanel,
    MomentPanel,
    TaskRow,
    Trajectory,
    WeeklyDigest,
    render,
)


def _digest(
    week_iso: str = "2026-W21",
    generated_at: datetime | None = None,
    **overrides: object,
) -> WeeklyDigest:
    if generated_at is None:
        generated_at = datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc)
    return WeeklyDigest(
        week_iso=week_iso,
        generated_at=generated_at,
        **overrides,  # type: ignore[arg-type]
    )


def _filled_digest() -> WeeklyDigest:
    """A digest with every section populated, used by tests that need
    to assert behavior on the data-rendered (not placeholder) path."""
    return WeeklyDigest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
        trajectory=Trajectory(
            label="Drifting",
            headline=(
                "Delegation up 0.14/wk over 6 weeks while engagement held flat."
            ),
            confidence_band="high confidence",
        ),
        headline_moment=MomentPanel(
            quoted_excerpt="write the function that does the thing",
            why_lost_score="No goal, constraints, or acceptance criteria.",
            next_time_try="State the goal and acceptance criteria first.",
            cost_dollars=0.42,
            cost_minutes=6,
        ),
        cost_ledger=CostLedger(
            this_week_dollars=12.50,
            baseline_dollars=9.80,
            biggest_line="Opus on refactoring (3 sessions, $5.40)",
            sonnet_swap_note="Sonnet could have handled 2 of those, saving ~$3.00",
        ),
        task_breakdown=(
            TaskRow(label="refactoring", session_count=3, dollars=5.40, worst_score=4.2),
            TaskRow(label="debugging", session_count=2, dollars=3.10, worst_score=5.8),
        ),
        dimensions=(
            DimRow(title="Planning", score=6.8, baseline=5.4, delta=1.4),
            DimRow(title="Context", score=5.9, baseline=6.2, delta=-0.3),
        ),
        follow_up=FollowUpPanel(
            commitment_text="state the goal and constraints before prompting",
            outcome="improved",
        ),
        one_thing_to_try=(
            "Open every session with a one-sentence goal and the acceptance criteria."
        ),
    )


# ------------------------------------------------------------ structural shape


def test_render_starts_with_doctype():
    """The output must be a full HTML document so a browser can open it
    directly via the file:// protocol."""
    out = render(_digest())
    assert out.startswith("<!doctype html>")


def test_render_closes_html_tag():
    """A well-formed document - rules out accidentally truncated output."""
    out = render(_digest()).rstrip()
    assert out.endswith("</html>")


def test_render_returns_str():
    assert isinstance(render(_digest()), str)


def test_render_includes_week_iso_in_masthead():
    """The masthead echoes the week_iso so the user knows which week they
    are reading without opening DevTools."""
    out = render(_digest(week_iso="2026-W21"))
    assert "2026-W21" in out


def test_render_includes_generated_date():
    """The masthead includes the generation date as a human-readable
    timestamp."""
    out = render(
        _digest(generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc))
    )
    assert "May 27, 2026" in out


# ----------------------------------------------------------- self-containment


def test_render_has_no_link_tags():
    """No ``<link>`` element of any kind - that rules out external CSS,
    favicons, and webfont stylesheets in one assertion."""
    out = render(_digest()).lower()
    assert "<link" not in out


def test_render_has_no_script_tags():
    """No ``<script>`` tags. The digest is static reading material with
    no client-side behavior; allowing scripts would weaken the
    self-contained contract."""
    out = render(_digest()).lower()
    assert "<script" not in out


def test_render_has_no_img_tags():
    """No ``<img>`` tags. If imagery is ever required it must be inlined
    as a ``data:`` URI, so this test guards the simple-by-default path."""
    out = render(_digest()).lower()
    assert "<img" not in out


def test_render_has_no_external_urls():
    """No ``http://`` or ``https://`` URLs anywhere - the file must open
    with no network access. If a future story needs to link to vendor
    documentation (e.g. an anchor in the colophon), this assertion can be
    relaxed in a targeted way."""
    out = render(_digest())
    assert "http://" not in out
    assert "https://" not in out


def test_render_has_no_protocol_relative_urls():
    """``//cdn.example.com/...`` would fetch via the page's protocol -
    still a network call, still banned."""
    out = render(_digest())
    # Avoid matching the doctype declaration or comments by scanning attribute
    # contexts where URLs commonly appear.
    for attr in ("src=", "href="):
        for match in re.finditer(rf'{attr}"([^"]*)"', out):
            value = match.group(1)
            assert not value.startswith("//"), (
                f"protocol-relative URL in {attr}{value!r}"
            )


def test_render_inlines_css_in_style_block():
    """All CSS must live in an inline ``<style>`` block."""
    out = render(_digest())
    assert "<style>" in out
    assert "</style>" in out


def test_render_has_no_css_import():
    """``@import url(...)`` would pull a remote stylesheet - banned."""
    out = render(_digest())
    assert "@import" not in out


def test_render_has_no_css_url_calls():
    """``url(...)`` in CSS commonly references external images or fonts.
    We don't need them for the digest; banning the whole construct keeps
    the file portable. If a future story needs ``url(data:...)`` it can
    relax this assertion to permit only the ``data:`` scheme."""
    out = render(_digest())
    assert "url(" not in out


def test_render_uses_system_font_stack():
    """Font stack must lead with a face that ships on every default
    browser, so the digest looks coherent without any webfont fetch."""
    out = render(_digest())
    assert "Georgia" in out


def test_render_charset_is_utf8():
    """UTF-8 is required for the digest to render the section glyphs
    and any non-ASCII content in user transcripts."""
    out = render(_digest())
    assert 'charset="utf-8"' in out


def test_render_has_viewport_meta():
    """Smoke check that the document is a regular responsive HTML page,
    not a fragment."""
    out = render(_digest())
    assert 'name="viewport"' in out


# ------------------------------------------------------------ dataclass shape


def test_weekly_digest_is_frozen():
    """``WeeklyDigest`` is immutable. Mutation would let renderers
    accidentally smuggle state and would make reuse across tests
    error-prone."""
    digest = _digest()
    with pytest.raises(dataclasses.FrozenInstanceError):
        digest.week_iso = "2026-W22"  # type: ignore[misc]


def test_weekly_digest_fields():
    """Lock the minimal shape of the input contract. Later stories add
    fields; this test will be updated when they do."""
    digest = _digest()
    assert digest.week_iso == "2026-W21"
    assert digest.generated_at == datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc)


def test_render_html_escapes_week_iso():
    """The week_iso is interpolated into the HTML; if a malformed value
    ever reaches the renderer it must not break the page."""
    out = render(_digest(week_iso="<W>"))
    # The literal angle bracket form must not appear as a tag.
    assert "<W>" not in out
    # The escaped form does.
    assert "&lt;W&gt;" in out


# ----------------------------------------------------- US-062: section order

# The exact section anchors (HTML id attributes) the renderer must emit, in
# the order fixed by spec section 6.1. Any drift here means the document
# is no longer spec-compliant.
_SECTION_IDS_IN_ORDER: tuple[str, ...] = (
    "trajectory",
    "this-weeks-moment",
    "cost-ledger",
    "where-the-week-went",
    "the-six-dimensions",
    "follow-up-from-last-week",
    "one-thing-to-try-next-week",
)

# Human-facing section titles in the same order. Trajectory is the only
# section that does NOT carry a "TITLE" heading in the spec ASCII art -
# it leads with the headline copy directly - so it has no entry here.
# The apostrophe in "This Week's Moment" is a literal in the renderer
# (not an escaped data value), so it shows up unescaped in the output.
_SECTION_TITLES_IN_ORDER: tuple[str, ...] = (
    "This Week's Moment",
    "Cost Ledger",
    "Where The Week Went",
    "The Six Dimensions",
    "Follow-up From Last Week",
    "One Thing To Try Next Week",
)


def _positions(out: str, needles: tuple[str, ...]) -> list[int]:
    return [out.find(n) for n in needles]


def test_render_emits_all_seven_sections_in_order_with_placeholders():
    """When the digest has no section data, all seven sections still
    render in the spec-mandated order. The placeholder text keeps the
    document shape stable for partial-data runs."""
    out = render(_digest())
    positions = _positions(out, tuple(f'id="{sid}"' for sid in _SECTION_IDS_IN_ORDER))
    for sid, pos in zip(_SECTION_IDS_IN_ORDER, positions):
        assert pos >= 0, f"section id={sid!r} missing from rendered HTML"
    assert positions == sorted(positions), (
        "section ids appeared out of spec section 6.1 order: "
        f"{list(zip(_SECTION_IDS_IN_ORDER, positions))}"
    )


def test_render_emits_all_seven_sections_in_order_when_populated():
    """The order is the spec contract; it must hold whether the data is
    populated or not."""
    out = render(_filled_digest())
    positions = _positions(out, tuple(f'id="{sid}"' for sid in _SECTION_IDS_IN_ORDER))
    for sid, pos in zip(_SECTION_IDS_IN_ORDER, positions):
        assert pos >= 0, f"section id={sid!r} missing from rendered HTML"
    assert positions == sorted(positions)


def test_render_emits_visible_section_titles_in_order():
    """The visible section titles (the words a reader sees) appear in
    the order spec section 6.1 prescribes. This complements the id-based
    check by guarding against renaming an id without updating the head."""
    out = render(_filled_digest())
    positions = _positions(out, _SECTION_TITLES_IN_ORDER)
    for title, pos in zip(_SECTION_TITLES_IN_ORDER, positions):
        assert pos >= 0, f"section title {title!r} missing from rendered HTML"
    assert positions == sorted(positions)


def test_trajectory_section_is_first_after_masthead():
    """The eye should land on the trajectory headline, not on a /10
    score (spec section 6 'priority order')."""
    out = render(_filled_digest())
    masthead_end = out.find("</header>")
    trajectory_start = out.find('id="trajectory"')
    moment_start = out.find('id="this-weeks-moment"')
    assert masthead_end > 0
    assert trajectory_start > masthead_end
    assert trajectory_start < moment_start


def test_masthead_does_not_lead_with_overall_score():
    """The masthead carries the week title and generation date only.
    No /10 hero scoreline (spec section 6.1: the eye lands on trajectory,
    not on the score; this is the explicit US-062 acceptance criterion)."""
    out = render(_filled_digest())
    masthead_start = out.find('<header class="masthead">')
    masthead_end = out.find("</header>")
    assert masthead_start >= 0 and masthead_end > masthead_start
    masthead_html = out[masthead_start:masthead_end]
    assert "/10" not in masthead_html, (
        f"masthead unexpectedly leads with /10: {masthead_html!r}"
    )


def test_render_has_no_hero_score_before_trajectory():
    """Belt-and-braces: nothing between the doctype and the trajectory
    section should display the overall /10. This rules out the v0.1
    pattern of putting a hero number above the fold."""
    out = render(_filled_digest())
    trajectory_start = out.find('id="trajectory"')
    assert trajectory_start > 0
    above_the_fold = out[:trajectory_start]
    assert "/10" not in above_the_fold


def test_trajectory_section_renders_headline_when_provided():
    """The trajectory section emits the LLM-generated headline so the
    user sees the specific behavioral observation, not just a label."""
    out = render(_filled_digest())
    assert (
        "Delegation up 0.14/wk over 6 weeks while engagement held flat." in out
    )


def test_moment_section_renders_quoted_excerpt_when_provided():
    """The headline moment quote should be visible in the moment
    section so the reader can ground the coaching in real text."""
    out = render(_filled_digest())
    assert "write the function that does the thing" in out


def test_cost_ledger_renders_this_week_total_when_provided():
    """The cost ledger calls out this-week-vs-baseline as the first
    visible row (spec section 6.1 ASCII art)."""
    out = render(_filled_digest())
    assert "$12.50" in out
    assert "$9.80" in out


def test_task_breakdown_renders_rows_when_provided():
    """The 'where the week went' panel lists tasks with session count
    and dollars (spec section 6.1)."""
    out = render(_filled_digest())
    assert "refactoring" in out
    assert "3 sessions" in out
    assert "$5.40" in out


def test_dimensions_section_renders_rows_when_provided():
    """The six-dim panel renders dim title and score per row."""
    out = render(_filled_digest())
    assert "Planning" in out
    assert "6.8" in out


def test_follow_up_section_renders_commitment_and_outcome_when_provided():
    """The follow-up panel closes the loop with the prior commitment
    and this week's outcome (spec section 6.3)."""
    out = render(_filled_digest())
    assert "state the goal and constraints before prompting" in out
    assert "improved" in out


def test_next_week_section_renders_sentence_when_provided():
    out = render(_filled_digest())
    assert (
        "Open every session with a one-sentence goal and the acceptance criteria."
        in out
    )


def test_render_filled_digest_keeps_self_containment_contract():
    """The self-containment contract from US-061 must continue to hold
    once new sections are populated; this is the regression seatbelt."""
    out = render(_filled_digest())
    lower = out.lower()
    assert "<link" not in lower
    assert "<script" not in lower
    assert "<img" not in lower
    assert "http://" not in out
    assert "https://" not in out
    assert "@import" not in out
    assert "url(" not in out
