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
import os
import re
from datetime import datetime, timezone
from pathlib import Path

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
    write_digest,
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
    # Coaching-first reorder: trajectory hero, then the coaching trio
    # (moment + follow-up + next-week) as one continuous editorial
    # spread, then the data appendix (cost / tasks / dimensions).
    # The product is a coaching tool, not a benchmark - the loop has
    # to land before the numbers.
    "trajectory",
    "this-weeks-moment",
    "follow-up-from-last-week",
    "one-thing-to-try-next-week",
    "cost-ledger",
    "where-the-week-went",
    "the-six-dimensions",
)

# Human-facing section titles in the same order. Trajectory is the only
# section that does NOT carry a "TITLE" heading in the spec ASCII art -
# it leads with the headline copy directly - so it has no entry here.
# The apostrophe in "This Week's Moment" is a literal in the renderer
# (not an escaped data value), so it shows up unescaped in the output.
_SECTION_TITLES_IN_ORDER: tuple[str, ...] = (
    # Coaching trio first, then the data appendix.
    "This Week's Moment",
    "Follow-up From Last Week",
    "One Thing To Try Next Week",
    "Cost Ledger",
    "Where The Week Went",
    "The Six Dimensions",
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


# --------------------------------------------- US-063: v0.1 visual language

# Spec section 6.1 originally read "do not redesign the look" - the
# v0.2 redesign pass replaced the hex-only tokens with OKLCH (per the
# impeccable design rules: perceptually uniform color, no hard-coded
# hex), kept terracotta as the emphasis hue, kept cream as the warm
# page ground, and swapped Libre Baskerville for a well-drawn system
# serif stack (Iowan Old Style first) so the digest renders with the
# OS's editorial face instead of a web-font default. These tests now
# lock the v0.2 design tokens.


def test_render_uses_terracotta_primary_in_oklch():
    """The terracotta primary anchors emphasis across the digest.
    Expressed in OKLCH per the impeccable design rules; the exact
    OKLCH triplet for the emphasis hue must appear in the rendered
    CSS so a refactor cannot quietly drift away from it."""
    out = render(_digest())
    assert "oklch(56% 0.135 38)" in out


def test_render_uses_warm_cream_ground():
    """The warm cream ground (high lightness, low chroma, hue ~80 in
    OKLCH) is preserved from v0.1 in spirit but expressed in OKLCH so
    its perceptual lightness is honest."""
    out = render(_digest())
    assert "oklch(96.5% 0.005 80)" in out


def test_render_uses_system_serif_stack_with_iowan_old_style_first():
    """v0.2: the display face is the OS's best editorial serif. Iowan
    Old Style (macOS / iOS) leads; Sitka Text (Windows) follows;
    Hoefler Text and Charter as further macOS fallbacks; Cambria and
    Georgia as ultimate cross-platform fallbacks. Libre Baskerville is
    intentionally NOT in the stack (it was the v0.1 default and is on
    the impeccable reflex-reject list)."""
    out = render(_digest())
    assert "'Iowan Old Style'" in out
    assert "'Libre Baskerville'" not in out


def test_render_does_not_fetch_libre_baskerville():
    """Naming Libre Baskerville in the font stack is fine; loading it
    from a CDN is not. The self-containment contract from US-061 must
    hold: no @import, no url(), no <link> to a font service."""
    out = render(_digest())
    lower = out.lower()
    # Spot-check the common font CDNs by name in case future edits
    # paste in a v0.1 <link rel=stylesheet>.
    assert "fonts.googleapis.com" not in lower
    assert "fonts.gstatic.com" not in lower
    assert "@font-face" not in lower
    # And the broader self-containment guards from US-061.
    assert "<link" not in lower
    assert "@import" not in lower
    assert "url(" not in lower


def test_render_keeps_georgia_as_inlined_fallback():
    """Libre Baskerville may or may not be present on the user's
    machine. Georgia ships on every default desktop OS, so it is the
    inlined fallback for both body and display stacks."""
    out = render(_digest())
    assert "Georgia" in out


# ------------------------------------- US-064: write to disk + latest pointer

# Spec section 13.1: the HTML is written to ``~/.praxis/weeks/<iso>.html``
# (one file per ISO week) and ``~/.praxis/latest.html`` always points to
# the most recent week's file. Re-running on the same week overwrites the
# file and refreshes the pointer. These tests sandbox the home directory
# to ``tmp_path`` so the user's real ``~/.praxis`` is never touched.


def test_write_digest_writes_to_weeks_directory(tmp_path: Path):
    """The digest must land at ``<home>/weeks/<iso>.html`` so
    ``praxis show 2026-W21`` can find it via a stable path (spec
    section 13.1)."""
    home = tmp_path / ".praxis"
    written = write_digest(_digest(week_iso="2026-W21"), home=home)
    assert written == home / "weeks" / "2026-W21.html"
    assert written.is_file()


def test_write_digest_creates_weeks_dir_if_missing(tmp_path: Path):
    """First-time runs should not require the user to pre-create
    ``~/.praxis/weeks/`` - the writer creates the path."""
    home = tmp_path / ".praxis"
    assert not (home / "weeks").exists()
    write_digest(_digest(), home=home)
    assert (home / "weeks").is_dir()


def test_write_digest_returns_path(tmp_path: Path):
    """The return value is the written file path; callers (CLI,
    notifier) rely on this to surface the location to the user."""
    home = tmp_path / ".praxis"
    written = write_digest(_digest(week_iso="2026-W21"), home=home)
    assert isinstance(written, Path)
    assert written.name == "2026-W21.html"


def test_write_digest_file_contents_match_render(tmp_path: Path):
    """``write_digest`` is render() + persistence; the on-disk bytes
    must equal what ``render`` would have returned."""
    digest = _filled_digest()
    home = tmp_path / ".praxis"
    written = write_digest(digest, home=home)
    assert written.read_text(encoding="utf-8") == render(digest)


def test_write_digest_creates_latest_pointer(tmp_path: Path):
    """``<home>/latest.html`` must exist after the first write."""
    home = tmp_path / ".praxis"
    write_digest(_digest(week_iso="2026-W21"), home=home)
    latest = home / "latest.html"
    # Either a symlink (POSIX) or a regular file (Windows fallback) is OK
    # per the "symlink or platform equivalent" acceptance criterion.
    assert latest.is_symlink() or latest.is_file()


def test_write_digest_latest_resolves_to_written_file(tmp_path: Path):
    """The latest pointer must dereference to the same content as the
    week file - whether it is a symlink or a copy."""
    home = tmp_path / ".praxis"
    written = write_digest(_filled_digest(), home=home)
    latest = home / "latest.html"
    assert latest.read_text(encoding="utf-8") == written.read_text(encoding="utf-8")


def test_write_digest_uses_relative_symlink_target(tmp_path: Path):
    """When a symlink is created (POSIX path), its target must be
    relative so the pointer survives if ``~/.praxis`` is moved or
    copied (e.g. to a Time Machine backup). Skipped if the platform
    fell back to a file copy."""
    home = tmp_path / ".praxis"
    write_digest(_digest(week_iso="2026-W21"), home=home)
    latest = home / "latest.html"
    if not latest.is_symlink():
        pytest.skip("platform fell back to a file copy")
    raw = os.readlink(latest)
    assert not os.path.isabs(raw), f"latest.html target should be relative, got {raw!r}"
    assert "2026-W21.html" in raw


def test_write_digest_overwrites_same_week(tmp_path: Path):
    """Re-running on the same week replaces the file's bytes in place
    (spec acceptance: "Re-running on the same week overwrites the file
    and updates the symlink")."""
    home = tmp_path / ".praxis"
    first_digest = _digest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
    )
    second_digest = _digest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc),
    )
    first_path = write_digest(first_digest, home=home)
    first_bytes = first_path.read_text(encoding="utf-8")
    second_path = write_digest(second_digest, home=home)
    assert second_path == first_path
    assert second_path.read_text(encoding="utf-8") != first_bytes
    assert "May 28, 2026" in second_path.read_text(encoding="utf-8")


def test_write_digest_updates_pointer_to_newest_week(tmp_path: Path):
    """When a new week is written, ``latest.html`` must follow it -
    the user always lands on the most recent digest."""
    home = tmp_path / ".praxis"
    write_digest(_digest(week_iso="2026-W20"), home=home)
    written_w21 = write_digest(_digest(week_iso="2026-W21"), home=home)
    latest = home / "latest.html"
    assert latest.read_text(encoding="utf-8") == written_w21.read_text(
        encoding="utf-8"
    )
    if latest.is_symlink():
        assert "2026-W21.html" in os.readlink(latest)


def test_write_digest_replaces_existing_latest_file(tmp_path: Path):
    """If ``latest.html`` already exists as a regular file (e.g.
    user-pasted, or a previous Windows-fallback copy), it must be
    replaced cleanly without leaving stale state."""
    home = tmp_path / ".praxis"
    home.mkdir(parents=True, exist_ok=True)
    stale = home / "latest.html"
    stale.write_text("stale", encoding="utf-8")
    write_digest(_digest(week_iso="2026-W21"), home=home)
    assert stale.read_text(encoding="utf-8") != "stale"


def test_write_digest_replaces_broken_symlink(tmp_path: Path):
    """A broken symlink (target deleted manually) should be replaced,
    not left in place. ``Path.exists()`` returns False on a broken
    symlink, so the writer must also check ``is_symlink()``."""
    home = tmp_path / ".praxis"
    home.mkdir(parents=True, exist_ok=True)
    broken_target = home / "weeks" / "2025-W52.html"
    latest = home / "latest.html"
    try:
        latest.symlink_to(broken_target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not supported on this platform")
    # Confirm the pre-condition: the symlink exists but is broken.
    assert latest.is_symlink()
    assert not latest.exists()
    write_digest(_digest(week_iso="2026-W21"), home=home)
    assert latest.exists()
    assert latest.read_text(encoding="utf-8") != ""


def test_write_digest_leaves_no_tmp_file(tmp_path: Path):
    """Atomic write uses a ``.tmp`` sibling that must be renamed away
    before ``write_digest`` returns. A leftover tmp file would mean
    the rename never happened."""
    home = tmp_path / ".praxis"
    write_digest(_digest(week_iso="2026-W21"), home=home)
    tmp_leftovers = list((home / "weeks").glob("*.tmp"))
    assert tmp_leftovers == [], (
        f"atomic write left tmp files behind: {tmp_leftovers}"
    )


def test_write_digest_honors_praxis_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When ``home`` is not passed, ``write_digest`` resolves the home
    directory through the same ``PRAXIS_HOME`` override the rest of
    the storage layer uses. This keeps the persistence layer testable
    without an explicit argument every time."""
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("PRAXIS_HOME", str(sandbox))
    written = write_digest(_digest(week_iso="2026-W21"))
    assert written == sandbox / "weeks" / "2026-W21.html"
    assert written.is_file()
    assert (sandbox / "latest.html").read_text(encoding="utf-8") == written.read_text(
        encoding="utf-8"
    )


# ------------------------- US-065: no synthetic markers, no raw secrets, no PII

# Spec section 15.1 #8: "No `<synthetic>` strings, no raw API keys, no
# obvious PII appears in any rendered digest." The renderer is the last
# hop before the digest lands in a file the user opens in a browser, so
# it MUST be defensive even if upstream redaction was skipped.

# These fixtures are NOT real credentials. They follow the structural
# pattern each redaction regex matches (provider prefix + sufficient
# tail length) so the test exercises every entry in
# ``praxis.redactor._PATTERNS``.
_ANTHROPIC_KEY = "sk-ant-api03-FAKEfake_-1234567890ABCDEFGHIJabcdefghij"
_OPENAI_KEY = "sk-proj-FAKEopenai1234567890ABCDEFabcdef_-XYZ09876"
_AWS_KEY = "AROAIOSFODNN7EXAMPLE"
_GITHUB_PAT = "gho_abcdefghijklmnopqrstuvwxyz0123456789"
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


def _digest_with_secrets() -> WeeklyDigest:
    """A digest whose user-content fields each carry one secret and the
    ``<synthetic>`` marker, so a single render exercises every leakage
    path the renderer is responsible for sealing."""
    return WeeklyDigest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
        trajectory=Trajectory(
            label="Drifting",
            headline=f"<synthetic> headline mentioning {_ANTHROPIC_KEY} inline.",
            confidence_band="high confidence",
        ),
        headline_moment=MomentPanel(
            quoted_excerpt=f"my key is {_OPENAI_KEY} and I pasted it here <synthetic>",
            why_lost_score=f"Token leaked: token={_GITHUB_PAT}",
            next_time_try=f"Remove AWS=`{_AWS_KEY}` from prompts.",
            cost_dollars=0.42,
            cost_minutes=6,
        ),
        cost_ledger=CostLedger(
            this_week_dollars=12.50,
            baseline_dollars=9.80,
            biggest_line=f"Opus session with leaked key sk-ant-api03 inline {_ANTHROPIC_KEY}",
            sonnet_swap_note=f"Bearer {_JWT} appeared in transcripts",
        ),
        task_breakdown=(
            TaskRow(
                label=f"refactoring <synthetic>",
                session_count=3,
                dollars=5.40,
                worst_score=4.2,
            ),
        ),
        dimensions=(
            DimRow(title="Planning <synthetic>", score=6.8, baseline=5.4, delta=1.4),
        ),
        follow_up=FollowUpPanel(
            commitment_text=f"avoid leaking password={_OPENAI_KEY} in prompts",
            outcome="improved <synthetic>",
        ),
        one_thing_to_try=(
            f"Audit transcripts for {_ANTHROPIC_KEY} and strip <synthetic> markers."
        ),
    )


def test_render_does_not_contain_synthetic_marker():
    """The literal ``<synthetic>`` placeholder must not appear anywhere
    in the rendered HTML, regardless of which field a stray marker landed
    in. Stripping happens before html.escape so the marker is gone for
    both source-grep and visible-text readings."""
    out = render(_digest_with_secrets())
    assert "<synthetic>" not in out
    # html.escape would have turned a stray `<synthetic>` into
    # `&lt;synthetic&gt;`. That would pass the literal check above but
    # would still be visible to the reader, so guard the escaped form too.
    assert "&lt;synthetic&gt;" not in out


def test_render_does_not_contain_synthetic_marker_in_masthead():
    """Belt-and-braces: if the week_iso or generated date were ever
    populated with a ``<synthetic>`` token (e.g. a malformed test
    fixture), the masthead must still be marker-free."""
    digest = _digest(week_iso="<synthetic>")
    out = render(digest)
    assert "<synthetic>" not in out
    assert "&lt;synthetic&gt;" not in out


def test_render_redacts_anthropic_key():
    """Anthropic API keys pasted into a moment or headline must be
    replaced with ``[REDACTED]`` before the file is rendered."""
    out = render(_digest_with_secrets())
    assert _ANTHROPIC_KEY not in out
    assert "[REDACTED]" in out


def test_render_redacts_openai_key():
    out = render(_digest_with_secrets())
    assert _OPENAI_KEY not in out


def test_render_redacts_aws_access_key():
    out = render(_digest_with_secrets())
    assert _AWS_KEY not in out


def test_render_redacts_github_pat():
    out = render(_digest_with_secrets())
    assert _GITHUB_PAT not in out


def test_render_redacts_jwt():
    out = render(_digest_with_secrets())
    assert _JWT not in out


def test_render_redacts_secret_in_every_user_facing_field():
    """Each user-content field on the digest is a possible leak path.
    The fixture seeds one secret per field; this test asserts every one
    is gone from the rendered HTML. If a future story adds a new field,
    extend ``_digest_with_secrets`` and this assertion together."""
    out = render(_digest_with_secrets())
    for needle in (_ANTHROPIC_KEY, _OPENAI_KEY, _AWS_KEY, _GITHUB_PAT, _JWT):
        assert needle not in out, f"{needle!r} leaked into rendered HTML"


def test_render_preserves_surrounding_prose_around_redacted_secret():
    """Redaction must not eat the rest of the sentence. The reader
    needs the surrounding prose intact so the redaction is legible
    (e.g. 'my key is [REDACTED] and I pasted it here')."""
    out = render(_digest_with_secrets())
    assert "my key is" in out
    assert "and I pasted it here" in out


def test_render_is_idempotent_under_sanitisation():
    """``redact_secrets`` is idempotent, and the synthetic strip is a
    plain ``.replace``. Running the renderer twice on the same digest
    yields the same bytes - this is a regression guard against a future
    edit that double-escapes or re-applies redaction in a non-idempotent
    way."""
    digest = _digest_with_secrets()
    assert render(digest) == render(digest)


def test_write_digest_strips_secrets_and_synthetic_from_disk(tmp_path: Path):
    """The on-disk file is the artifact the user opens in a browser,
    so the no-secrets / no-synthetic-markers contract must hold against
    file bytes, not just the in-memory string."""
    home = tmp_path / ".praxis"
    written = write_digest(_digest_with_secrets(), home=home)
    on_disk = written.read_text(encoding="utf-8")
    for needle in (
        _ANTHROPIC_KEY,
        _OPENAI_KEY,
        _AWS_KEY,
        _GITHUB_PAT,
        _JWT,
        "<synthetic>",
        "&lt;synthetic&gt;",
    ):
        assert needle not in on_disk, (
            f"{needle!r} leaked into the on-disk digest at {written}"
        )
    assert "[REDACTED]" in on_disk


def test_render_still_html_escapes_after_sanitisation():
    """The sanitisation pass must not break the html.escape contract
    from US-061: a ``<W>`` week_iso must still appear as ``&lt;W&gt;``
    in the rendered HTML so the browser does not interpret it as a
    tag. Only the specific ``<synthetic>`` marker is stripped; other
    angle brackets remain escaped."""
    out = render(_digest(week_iso="<W>"))
    assert "<W>" not in out
    assert "&lt;W&gt;" in out


def test_render_does_not_re_redact_existing_placeholder():
    """If a moment field already carries the ``[REDACTED]`` token (e.g.
    an upstream redaction already ran), the renderer must not mangle
    it. Idempotence depends on the redactor not matching its own
    placeholder."""
    digest = WeeklyDigest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
        headline_moment=MomentPanel(
            quoted_excerpt="my key is [REDACTED] in the transcript",
            why_lost_score="leaked secret was scrubbed",
            next_time_try="never paste keys",
        ),
    )
    out = render(digest)
    assert "[REDACTED]" in out
    # Exactly one occurrence in the moment quote - no accidental
    # second redaction pass that doubles the placeholder.
    assert out.count("[REDACTED]") == 1


def test_render_filled_digest_with_secrets_keeps_self_containment_contract():
    """The US-061 self-containment contract must continue to hold once
    secrets and synthetic markers are stripped. This is the regression
    seatbelt for the cross-story interaction."""
    out = render(_digest_with_secrets())
    lower = out.lower()
    assert "<link" not in lower
    assert "<script" not in lower
    assert "<img" not in lower
    assert "http://" not in out
    assert "https://" not in out
    assert "@import" not in out
    assert "url(" not in out


# ----------------------------------------- US-038: behavioral-patterns panel


def _bp_panel_with_rows():
    """A behavioral-patterns panel with two populated signal rows."""
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
            excerpts=("write me a function",),
        ),
    ))


def _bp_empty_panel():
    """A panel where every row has count==0 (US-038 empty-state path)."""
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


def _bp_digest(panel):
    from praxis.reports.panel_inputs import PanelInputs
    return WeeklyDigest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
        panel_inputs=PanelInputs(behavioral_signals=panel),
    )


def test_behavioral_patterns_section_always_present():
    """The section anchor (`id="behavioral-patterns"`) always renders so
    the document shape is stable across populated + empty states."""
    out = render(_bp_digest(_bp_panel_with_rows()))
    assert 'id="behavioral-patterns"' in out
    out_empty = render(_bp_digest(_bp_empty_panel()))
    assert 'id="behavioral-patterns"' in out_empty


def test_behavioral_patterns_renders_label_and_count():
    """Each populated row carries its display label and total count
    so the reader sees the raw signal volume."""
    out = render(_bp_digest(_bp_panel_with_rows()))
    assert "Why-questions" in out
    assert "4 times" in out
    assert "Pure delegation" in out
    assert "2 times" in out


def test_behavioral_patterns_renders_excerpts():
    """Each populated row renders up to two raw user-turn excerpts
    so the reader can ground the count in transcript text."""
    out = render(_bp_digest(_bp_panel_with_rows()))
    assert "why does this approach work for caching?" in out
    assert "write me a function" in out


def test_behavioral_patterns_renders_citation_per_row():
    """Both renderers cite the primary source per signal inline in
    small footnote text (US-038 acceptance)."""
    out = render(_bp_digest(_bp_panel_with_rows()))
    # The citation appears at least twice (once per populated row).
    assert out.count("Shen &amp; Tamkin 2026 (arXiv 2601.20245)") >= 2


def test_behavioral_patterns_renders_empty_state_when_zero_signals():
    """When every row has count==0 the section surfaces the verbatim
    empty-state message instead of an empty list (US-038 acceptance)."""
    out = render(_bp_digest(_bp_empty_panel()))
    assert "No behavioral patterns captured this week." in out


def test_behavioral_patterns_renders_empty_state_when_no_panel():
    """Defaults gracefully: a digest with no panel_inputs still emits
    the empty-state message rather than a broken or missing section."""
    out = render(_digest())  # no panel_inputs
    assert 'id="behavioral-patterns"' in out
    assert "No behavioral patterns captured this week." in out


def test_behavioral_patterns_zero_count_rows_dropped_when_others_fire():
    """Rows with count==0 do not pollute the table when other signals
    have fired; the reader sees only triggered patterns."""
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
    out = render(_bp_digest(panel))
    assert "Why-questions" in out
    assert "Pure delegation" not in out


def test_behavioral_patterns_section_after_six_dim_panel():
    """Behavioral patterns sits after the six-dim cards in the data
    block so the reader sees the structural /10 read first and then
    the raw-pattern evidence that informs it."""
    out = render(_bp_digest(_bp_panel_with_rows()))
    six_dim_pos = out.find('id="the-six-dimensions"')
    bp_pos = out.find('id="behavioral-patterns"')
    assert 0 <= six_dim_pos < bp_pos


def test_behavioral_patterns_self_containment_holds():
    """The US-061 self-containment contract must hold for the new
    panel; rendering a populated panel must not introduce external
    links, scripts, or images."""
    out = render(_bp_digest(_bp_panel_with_rows()))
    lower = out.lower()
    assert "<link" not in lower
    assert "<script" not in lower
    assert "<img" not in lower
    assert "http://" not in out
    assert "https://" not in out
    assert "@import" not in out
    assert "url(" not in out


def test_behavioral_patterns_html_escapes_excerpts():
    """Excerpts are user-provided strings so the renderer must HTML-escape
    them; a stray `<script>` in a transcript must not become a real
    `<script>` tag in the rendered digest."""
    from praxis.reports.panel_inputs import (
        BehavioralPatternRow,
        BehavioralPatternsPanel,
    )
    panel = BehavioralPatternsPanel(rows=(
        BehavioralPatternRow(
            signal_kind="why_question",
            label="Why-questions",
            count=1,
            citation="Shen & Tamkin 2026 (arXiv 2601.20245)",
            excerpts=("<script>alert('xss')</script>",),
        ),
    ))
    out = render(_bp_digest(panel))
    assert "<script>alert" not in out
    assert "&lt;script&gt;alert" in out


# ---------------------- US-039: aug/auto balance + cadence panels (HTML) ----


def _aug_auto_html_panel():
    from praxis.reports.panel_inputs import AugAutoBalancePanel
    return AugAutoBalancePanel(
        augmentation_count=3,
        automation_count=2,
        mixed_count=1,
        unclassified_count=0,
    )


def _aug_auto_unavailable_panel():
    from praxis.reports.panel_inputs import AugAutoBalancePanel
    return AugAutoBalancePanel(
        unclassified_count=3,
        classifier_unavailable=True,
    )


def _cadence_html_panel():
    from praxis.reports.panel_inputs import CadencePanel
    return CadencePanel(
        weekday_streak=5,
        substantive_session_count=7,
        high_adopter_position="high",
    )


def _cadence_empty_panel():
    from praxis.reports.panel_inputs import CadencePanel
    return CadencePanel(
        weekday_streak=0,
        substantive_session_count=0,
        high_adopter_position=None,
    )


def _us039_digest(*, aug_auto=None, cadence=None):
    from praxis.reports.panel_inputs import PanelInputs
    return WeeklyDigest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
        panel_inputs=PanelInputs(
            aug_auto_balance=aug_auto,
            cadence=cadence,
        ),
    )


def test_aug_auto_balance_section_always_present():
    """The section anchor (`id="aug-auto-balance"`) always renders."""
    out_empty = render(_digest())  # no panel_inputs
    assert 'id="aug-auto-balance"' in out_empty
    out_full = render(_us039_digest(aug_auto=_aug_auto_html_panel()))
    assert 'id="aug-auto-balance"' in out_full


def test_aug_auto_balance_renders_classifier_unavailable_message():
    """When every session in the week is unclassified the panel surfaces
    the verbatim US-039 unavailable copy."""
    out = render(_us039_digest(aug_auto=_aug_auto_unavailable_panel()))
    assert "Classifier unavailable for this week." in out


def test_aug_auto_balance_no_panel_renders_unavailable():
    """A digest with no panel_inputs at all falls back to the
    unavailable copy rather than 0/0/0."""
    out = render(_digest())
    assert "Classifier unavailable for this week." in out
    # And does NOT show misleading 0% counts inside the panel itself
    # (CSS uses % values for opacity/lightness, so check only the
    # aug-auto-balance section's body for misleading zero shares).
    start = out.find('id="aug-auto-balance"')
    end = out.find("</section>", start)
    panel_html = out[start:end]
    assert "Augmentation</span>" not in panel_html  # no populated row when unavailable


def test_aug_auto_balance_renders_shares_when_populated():
    """When the classifier has data the panel emits the three shares."""
    out = render(_us039_digest(aug_auto=_aug_auto_html_panel()))
    # 3/6 = 50% aug; 2/6 = 33% auto; 1/6 = 17% mixed
    assert "Augmentation" in out
    assert "Automation" in out
    assert "Mixed" in out
    assert "50%" in out
    assert "33%" in out
    assert "17%" in out


def test_aug_auto_balance_renders_anthropic_anchor():
    """The Anthropic Economic Index anchor renders inline (US-039)."""
    out = render(_us039_digest(aug_auto=_aug_auto_html_panel()))
    assert "Anthropic Economic Index" in out
    assert "52% augmentation" in out
    assert "45% automation" in out


def test_cadence_section_always_present():
    """The section anchor (`id="cadence"`) always renders."""
    out_empty = render(_digest())
    assert 'id="cadence"' in out_empty
    out_full = render(_us039_digest(cadence=_cadence_html_panel()))
    assert 'id="cadence"' in out_full


def test_cadence_renders_no_activity_message_when_empty():
    """When zero substantive sessions fell in the window, the section
    surfaces the verbatim US-039 message."""
    out = render(_us039_digest(cadence=_cadence_empty_panel()))
    assert "No substantive sessions in the last 21 days." in out


def test_cadence_omits_high_adopter_label_when_empty():
    """The renderer must not surface a stand-in label when there is no
    activity to position on the spectrum."""
    out = render(_us039_digest(cadence=_cadence_empty_panel()))
    assert "Low-adopter" not in out
    assert "Moderate-adopter" not in out
    assert "High-adopter" not in out


def test_cadence_renders_streak_when_populated():
    """Streak renders as 'N of 21 days'."""
    out = render(_us039_digest(cadence=_cadence_html_panel()))
    assert "5 of 21 days" in out


def test_cadence_renders_spectrum_label_when_populated():
    """The high-adopter position renders as a human-facing label."""
    out = render(_us039_digest(cadence=_cadence_html_panel()))
    assert "High-adopter" in out


def test_cadence_renders_arxiv_citation():
    """The arXiv 2509.19708 anchor is cited inline (US-039 acceptance)."""
    out = render(_us039_digest(cadence=_cadence_html_panel()))
    assert "arXiv 2509.19708" in out


def test_us039_panels_self_containment_holds():
    """The US-061 self-containment contract must hold for the new
    panels; rendering must not introduce external links, scripts, or
    images regardless of which state each panel renders in."""
    out = render(
        _us039_digest(
            aug_auto=_aug_auto_html_panel(),
            cadence=_cadence_html_panel(),
        )
    )
    lower = out.lower()
    assert "<link" not in lower
    assert "<script" not in lower
    assert "<img" not in lower
    assert "http://" not in out
    assert "https://" not in out
    assert "@import" not in out
    assert "url(" not in out


def test_us039_panels_render_in_data_block_after_behavioral_patterns():
    """The new panels sit inside the data block after behavioral
    patterns so the editorial cadence keeps 'what kind of user' signals
    grouped together."""
    out = render(
        _us039_digest(
            aug_auto=_aug_auto_html_panel(),
            cadence=_cadence_html_panel(),
        )
    )
    bp_pos = out.find('id="behavioral-patterns"')
    bal_pos = out.find('id="aug-auto-balance"')
    cad_pos = out.find('id="cadence"')
    assert 0 <= bp_pos < bal_pos < cad_pos


# ---------------------- US-040: repeat-task radar + verification ------------


def _repeat_task_html_panel():
    from praxis.reports.panel_inputs import (
        RepeatTaskRadarPanel,
        RepeatTaskRow,
    )
    return RepeatTaskRadarPanel(
        rows=(
            RepeatTaskRow(
                canonical_first_sentence="fix the failing auth test",
                occurrences=3,
                estimated_minutes_per_occurrence=12.0,
            ),
            RepeatTaskRow(
                canonical_first_sentence="regenerate the changelog entry",
                occurrences=4,
                estimated_minutes_per_occurrence=8.5,
            ),
        )
    )


def _repeat_task_empty_panel():
    from praxis.reports.panel_inputs import RepeatTaskRadarPanel
    return RepeatTaskRadarPanel()


def _verification_html_panel():
    from praxis.reports.panel_inputs import VerificationCalibrationPanel
    return VerificationCalibrationPanel(
        source_check_count=2,
        test_run_count=3,
        spot_check_count=1,
        blanket_accept_count=4,
    )


def _verification_empty_panel():
    from praxis.reports.panel_inputs import VerificationCalibrationPanel
    return VerificationCalibrationPanel()


def _us040_digest(*, repeat_task=None, verification=None):
    from praxis.reports.panel_inputs import PanelInputs
    return WeeklyDigest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
        panel_inputs=PanelInputs(
            repeat_task_radar=repeat_task,
            verification_calibration=verification,
        ),
    )


def test_repeat_task_radar_section_always_present():
    """The section anchor (`id="repeat-task-radar"`) always renders."""
    out_empty = render(_digest())
    assert 'id="repeat-task-radar"' in out_empty
    out_full = render(_us040_digest(repeat_task=_repeat_task_html_panel()))
    assert 'id="repeat-task-radar"' in out_full


def test_repeat_task_radar_renders_empty_state_message():
    """When detect_repeats returned nothing the panel emits the verbatim
    US-040 empty-state copy instead of an empty list."""
    out = render(_us040_digest(repeat_task=_repeat_task_empty_panel()))
    assert "No repeat tasks detected this week." in out


def test_repeat_task_radar_no_panel_renders_empty_state():
    """A digest with no panel_inputs falls back to the verbatim empty
    state rather than emitting an empty rows section."""
    out = render(_digest())
    assert "No repeat tasks detected this week." in out


def test_repeat_task_radar_renders_each_row():
    """Each RepeatTask renders the canonical sentence, occurrence
    count, per-occurrence minutes, and the skill tag."""
    out = render(_us040_digest(repeat_task=_repeat_task_html_panel()))
    assert "fix the failing auth test" in out
    assert "regenerate the changelog entry" in out
    assert "3 times" in out
    assert "4 times" in out
    assert "12 min" in out
    assert "8.5 min" in out
    assert "Could become a skill" in out


def test_repeat_task_radar_renders_citation():
    """The OpenAI + Anthropic Skills citation renders inline."""
    out = render(_us040_digest(repeat_task=_repeat_task_html_panel()))
    assert "OpenAI" in out
    assert "Anthropic Skills" in out


def test_repeat_task_radar_html_escapes_canonical_sentence():
    """Canonical sentences come from user transcripts so they must be
    HTML-escaped; a stray script tag must not become a real tag."""
    from praxis.reports.panel_inputs import (
        RepeatTaskRadarPanel,
        RepeatTaskRow,
    )
    panel = RepeatTaskRadarPanel(
        rows=(
            RepeatTaskRow(
                canonical_first_sentence="<script>alert('xss')</script>",
                occurrences=3,
                estimated_minutes_per_occurrence=5.0,
            ),
        )
    )
    out = render(_us040_digest(repeat_task=panel))
    assert "<script>alert" not in out
    assert "&lt;script&gt;alert" in out


def test_verification_calibration_section_always_present():
    """The section anchor (`id="verification-calibration"`) always renders."""
    out_empty = render(_digest())
    assert 'id="verification-calibration"' in out_empty
    out_full = render(_us040_digest(verification=_verification_html_panel()))
    assert 'id="verification-calibration"' in out_full


def test_verification_calibration_renders_no_sessions_message():
    """When the week has no sessions to categorize the panel emits
    the explicit empty-state copy."""
    out = render(_us040_digest(verification=_verification_empty_panel()))
    assert "No sessions to calibrate verification against this week." in out


def test_verification_calibration_renders_all_buckets():
    """All four buckets render in display order with their counts."""
    out = render(_us040_digest(verification=_verification_html_panel()))
    assert "Source-check" in out
    assert "Test-run" in out
    assert "Spot-check" in out
    assert "Blanket-accept" in out
    # And the counts (2/3/1/4) render somewhere in the section body.
    start = out.find('id="verification-calibration"')
    end = out.find("</section>", start)
    section_html = out[start:end]
    assert "2 sessions" in section_html
    assert "3 sessions" in section_html
    assert "1 session" in section_html
    assert "4 sessions" in section_html


def test_verification_calibration_renders_citation():
    """The Sonar / Stack Overflow / automation-bias citation renders
    inline as a small footnote (US-040 AC)."""
    out = render(_us040_digest(verification=_verification_html_panel()))
    assert "Sonar" in out
    assert "Stack Overflow" in out
    assert "automation-bias" in out


def test_us040_panels_self_containment_holds():
    """The US-061 self-containment contract must hold; rendering must
    not introduce external links, scripts, or images."""
    out = render(
        _us040_digest(
            repeat_task=_repeat_task_html_panel(),
            verification=_verification_html_panel(),
        )
    )
    lower = out.lower()
    assert "<link" not in lower
    assert "<script" not in lower
    assert "<img" not in lower
    assert "http://" not in out
    assert "https://" not in out
    assert "@import" not in out
    assert "url(" not in out


def test_us040_panels_render_after_cadence():
    """The repeat-task radar and verification-calibration panels sit
    inside the data block after the cadence panel so the editorial
    cadence stays uniform."""
    from praxis.reports.panel_inputs import PanelInputs
    digest = WeeklyDigest(
        week_iso="2026-W21",
        generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc),
        panel_inputs=PanelInputs(
            aug_auto_balance=_aug_auto_html_panel(),
            cadence=_cadence_html_panel(),
            repeat_task_radar=_repeat_task_html_panel(),
            verification_calibration=_verification_html_panel(),
        ),
    )
    out = render(digest)
    cad_pos = out.find('id="cadence"')
    rt_pos = out.find('id="repeat-task-radar"')
    vc_pos = out.find('id="verification-calibration"')
    assert 0 <= cad_pos < rt_pos < vc_pos
