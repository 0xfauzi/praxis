"""Renderer-side tests for the cost-ledger disclaimer (US-051, spec section 10.2).

Acceptance criteria from the PRD:
  - The cost-ledger output emits the literal disclaimer
    ``rough estimate from token volume + tier pricing`` once at the
    bottom of the digest.
  - No moment is rendered as an invoiced cost.

Design contract enforced by these tests:
  - The literal text is fixed by spec; no rewording.
  - The disclaimer appears exactly once on both renderer surfaces
    (terminal + HTML). Repeating it would dilute the message; omitting
    it would let the rough-estimate numbers read as invoices.
  - The disclaimer renders at the bottom of each digest (after every
    other section, including the per-model spend block on HTML and the
    daily-consolidation footer line on terminal).
  - Neither surface uses 'invoice'/'invoiced'/'billed' language for any
    dollar amount -- the spec is explicit that these are estimates.
"""
from __future__ import annotations

from datetime import date

from praxis.orchestrator import RunSummary
from praxis.reports import html_report, terminal
from praxis.reports.cost_ledger_panel import (
    COST_DISCLAIMER_HTML_CLASS,
    COST_LEDGER_DISCLAIMER,
    format_cost_disclaimer,
    format_cost_disclaimer_html,
)
from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.coach import Coaching
from praxis.scoring.rubric import RUBRIC


def _make_snapshot(dim_value: float = 6.0) -> ProfileSnapshot:
    return ProfileSnapshot(
        overall=6.0,
        dimension_means={d.key: dim_value for d in RUBRIC},
        session_count=12,
        provider_breakdown={"claude": 8, "codex": 4},
        strongest_dimension="planning",
        weakest_dimension="verification",
        standout_moments=["Asked for trade-offs."],
        failure_modes=["Took the first answer."],
    )


def _make_coaching() -> Coaching:
    return Coaching(
        headline="Solid working practice.",
        focus_areas=[],
        daily_practice="Restate the goal before any tool call.",
        generated_by="test",
    )


def _make_summary() -> RunSummary:
    return RunSummary(
        sessions_seen=12,
        sessions_new=2,
        sessions_scored=2,
        elapsed_seconds=0.0,
        snapshot=_make_snapshot(),
        coaching=_make_coaching(),
        consolidated_for=date(2026, 5, 27),
    )


# ---------------------------------------------------------------------- literal


def test_disclaimer_literal_matches_spec():
    """Spec section 10.2 fixes the exact prose; no rewording allowed."""
    assert COST_LEDGER_DISCLAIMER == "rough estimate from token volume + tier pricing"


def test_format_cost_disclaimer_returns_literal_verbatim():
    """Plain-text formatter is the identity on the literal -- no prefix/suffix."""
    assert format_cost_disclaimer() == COST_LEDGER_DISCLAIMER


def test_format_cost_disclaimer_purity():
    """Same input (none), same output -- no hidden state."""
    assert format_cost_disclaimer() == format_cost_disclaimer()
    assert format_cost_disclaimer_html() == format_cost_disclaimer_html()


def test_format_cost_disclaimer_html_wraps_with_class():
    """HTML wrapper carries the muted CSS class and the literal text."""
    out = format_cost_disclaimer_html()
    assert out.startswith("<span ")
    assert out.endswith("</span>")
    assert f'class="{COST_DISCLAIMER_HTML_CLASS}"' in out
    assert COST_LEDGER_DISCLAIMER in out


def test_cost_disclaimer_html_class_is_stable_string():
    """The CSS class name is the contract between formatter and stylesheet.

    If this constant changes, the ``.cost-disclaimer`` rule in
    ``html_report.py`` must change too -- this test fails loudly if
    either drifts.
    """
    assert COST_DISCLAIMER_HTML_CLASS == "cost-disclaimer"


# ---------------------------------------------------------------------- HTML


def test_html_emits_disclaimer_once():
    """The disclaimer literal appears exactly once in the rendered HTML.

    The spec section 10.2 wording is 'once at the bottom of the digest'.
    Repeating it would dilute the message; the test counts occurrences.
    """
    out = html_report.render(_make_summary())
    assert out.count(COST_LEDGER_DISCLAIMER) == 1


def test_html_disclaimer_is_at_bottom_of_digest():
    """The disclaimer appears in the colophon, after every other section.

    'Bottom of the digest' is operationalized as: the disclaimer's
    position in the output is past the masthead, dimensions, focus
    cards, patterns, trajectory, and per-model sections.
    """
    out = html_report.render(_make_summary())
    disclaimer_idx = out.index(COST_LEDGER_DISCLAIMER)
    # Every other named section must occur before the disclaimer.
    for marker in [
        "Praxis &middot; A reading of your AI practice",  # masthead eyebrow
        "The six dimensions",
        "Where to put your attention this week",
        "Patterns from your recent sessions",
    ]:
        marker_idx = out.index(marker)
        assert marker_idx < disclaimer_idx, (
            f"{marker!r} appears at {marker_idx} but disclaimer at {disclaimer_idx}"
        )
    # And the disclaimer is inside the colophon (the bottom-most named block).
    colophon_idx = out.index('class="colophon"')
    assert colophon_idx < disclaimer_idx


def test_html_disclaimer_uses_muted_css_class():
    """The disclaimer renders with the muted CSS class for the faded look."""
    out = html_report.render(_make_summary())
    assert f'class="{COST_DISCLAIMER_HTML_CLASS}"' in out


def test_html_stylesheet_defines_disclaimer_class():
    """The CSS rule for .cost-disclaimer must be present in the stylesheet.

    Without this rule the span would inherit the default colophon
    colour and not actually read as a faded footer caveat.
    """
    out = html_report.render(_make_summary())
    assert f".{COST_DISCLAIMER_HTML_CLASS}" in out
    rule_idx = out.find(f".{COST_DISCLAIMER_HTML_CLASS} ")
    assert rule_idx != -1, "no CSS rule found for the disclaimer class"
    block = out[rule_idx : rule_idx + 200]
    assert "var(--ink-300)" in block or "var(--ink-500)" in block


def test_html_no_invoice_language():
    """Spec section 10.2: do not present cost figures as invoiced costs."""
    out = html_report.render(_make_summary()).lower()
    for forbidden in ("invoice", "invoiced", "billed"):
        assert forbidden not in out, f"render contains forbidden term {forbidden!r}"


# ---------------------------------------------------------------------- Terminal


def test_terminal_emits_disclaimer_once():
    """The disclaimer literal appears exactly once in the terminal output."""
    out = terminal.render(_make_summary())
    assert out.count(COST_LEDGER_DISCLAIMER) == 1


def test_terminal_disclaimer_is_at_bottom_of_digest():
    """Disclaimer follows every named section in the terminal output.

    Mirrors the HTML contract: the disclaimer is at the bottom, after
    the masthead, the dimensions panel, and the daily-consolidation
    footer line.
    """
    out = terminal.render(_make_summary())
    disclaimer_idx = out.index(COST_LEDGER_DISCLAIMER)
    # The terminal masthead always starts with PRAXIS in the eyebrow.
    masthead_idx = out.index("PRAXIS")
    assert masthead_idx < disclaimer_idx
    # The daily-consolidation footer line precedes the disclaimer (same _footer block).
    consolidation_idx = out.index("Daily consolidation")
    assert consolidation_idx < disclaimer_idx


def test_terminal_no_invoice_language():
    """Spec section 10.2: terminal must not present cost figures as invoiced."""
    out = terminal.render(_make_summary()).lower()
    for forbidden in ("invoice", "invoiced", "billed"):
        assert forbidden not in out, f"render contains forbidden term {forbidden!r}"


def test_terminal_disclaimer_survives_when_no_consolidation_today():
    """Even on the 'cached consolidation' code path the disclaimer renders.

    The disclaimer is unconditional -- it MUST appear regardless of
    whether daily consolidation ran today, regardless of last-week
    data, regardless of trajectory state. A surface that sometimes
    omits the disclaimer would let cost numbers read as invoices on
    those runs.
    """
    summary = RunSummary(
        sessions_seen=12,
        sessions_new=2,
        sessions_scored=2,
        elapsed_seconds=0.0,
        snapshot=_make_snapshot(),
        coaching=_make_coaching(),
        consolidated_for=None,  # cached path
    )
    out = terminal.render(summary)
    assert COST_LEDGER_DISCLAIMER in out


def test_html_disclaimer_survives_when_no_consolidation_today():
    """Same unconditional contract on the HTML surface."""
    summary = RunSummary(
        sessions_seen=12,
        sessions_new=2,
        sessions_scored=2,
        elapsed_seconds=0.0,
        snapshot=_make_snapshot(),
        coaching=_make_coaching(),
        consolidated_for=None,  # cached path
    )
    out = html_report.render(summary)
    assert COST_LEDGER_DISCLAIMER in out


# ---------------------------------------------------------------------- moments


def test_moment_dollar_impact_not_rendered_as_invoice_terminal():
    """No moment-level dollar impact is rendered as an invoiced cost.

    The current renderers do not surface ``Moment.dollar_impact_estimate``
    directly. This test locks the framing contract: even if a future
    iteration starts rendering per-moment impact figures, it must not
    introduce invoice language; the disclaimer in the footer is the
    only allowed framing.
    """
    out = terminal.render(_make_summary())
    assert COST_LEDGER_DISCLAIMER in out
    # The framing rule applies surface-wide: no 'invoiced' usage at all.
    assert "invoice" not in out.lower()


def test_moment_dollar_impact_not_rendered_as_invoice_html():
    """Same framing contract on the HTML surface (spec section 10.2)."""
    out = html_report.render(_make_summary())
    assert COST_LEDGER_DISCLAIMER in out
    assert "invoice" not in out.lower()
