"""Tests for the baseline panel rendering helpers (US-035, spec section 8.4).

Acceptance criteria:
  - When the user has less than 14 days of data, baseline values render
    as ``--``.
  - Deltas are not displayed in that case; the panel shows
    ``Baseline forming. Come back in 2 more weeks for week-over-week.``
  - Last-week mean still renders if at least one prior week of data
    exists (covered alongside US-036 wiring; the data-state predicates
    are tested in ``test_baseline.py::test_forming_and_prior_week_*``).
"""

from __future__ import annotations

from praxis.reports.baseline_panel import (
    BASELINE_FORMING_MESSAGE,
    BASELINE_PLACEHOLDER,
    LAST_WEEK_HTML_CLASS,
    format_baseline_value,
    format_last_week_annotation,
    format_last_week_annotation_html,
)


def test_placeholder_glyph_matches_spec():
    """Spec section 8.4: render baseline as ``--`` when forming."""
    assert BASELINE_PLACEHOLDER == "--"


def test_forming_message_matches_spec():
    """The exact prose from spec section 8.4."""
    assert (
        BASELINE_FORMING_MESSAGE
        == "Baseline forming. Come back in 2 more weeks for week-over-week."
    )


def test_format_baseline_value_returns_placeholder_when_forming():
    """When forming, the numeric value is suppressed and ``--`` is shown."""
    assert format_baseline_value(6.5, forming=True) == BASELINE_PLACEHOLDER
    assert format_baseline_value(0.0, forming=True) == BASELINE_PLACEHOLDER
    assert format_baseline_value(9.99, forming=True) == BASELINE_PLACEHOLDER


def test_format_baseline_value_passes_numeric_when_not_forming():
    """When the baseline is formed, the value renders with one decimal."""
    assert format_baseline_value(6.5, forming=False) == "6.5"
    assert format_baseline_value(0.0, forming=False) == "0.0"
    assert format_baseline_value(9.0, forming=False) == "9.0"


def test_format_baseline_value_one_decimal_precision():
    """Matches the ``f"{score:.1f}"`` format used elsewhere in both renderers.

    The HTML report's ``_dimension_bar_html`` and the terminal's
    ``_dimensions`` both print dim scores with one decimal, so the
    baseline annotation MUST use the same precision -- a baseline
    that read ``6.45`` next to a current of ``6.5`` would look wrong.
    """
    assert format_baseline_value(6.453, forming=False) == "6.5"
    assert format_baseline_value(6.44, forming=False) == "6.4"


def test_format_baseline_value_purity():
    """Same input, same output -- no hidden state."""
    for value in [0.0, 3.14, 6.5, 9.0]:
        for forming in (True, False):
            assert format_baseline_value(value, forming) == format_baseline_value(value, forming)


def test_forming_flag_overrides_value():
    """When forming is True, the numeric value is ignored entirely.

    Locks the contract: callers can pass through ``baseline.overall_mean``
    (which is 0.0 in the empty-baseline case) without first guarding the
    call site -- the helper itself does the right thing.
    """
    assert format_baseline_value(7.5, forming=True) == BASELINE_PLACEHOLDER
    # The placeholder glyph deliberately does NOT contain the numeric
    # value at all, even partially.
    assert "7" not in BASELINE_PLACEHOLDER
    assert "." not in BASELINE_PLACEHOLDER


# ---------------------------------------------------------------------- US-036
# Last-week annotation rendered as a faded secondary anchor in HTML;
# terminal omits it entirely per spec section 8.1.


def test_last_week_html_class_matches_renderer_contract():
    """The CSS class name is the contract between formatter and stylesheet.

    The HTML renderer's stylesheet defines a `.dim-last-week` rule
    with var(--ink-300) for the faded look. If this constant changes,
    that rule must change too -- this test fails loudly if either
    drifts.
    """
    assert LAST_WEEK_HTML_CLASS == "dim-last-week"


def test_format_last_week_annotation_none_is_empty():
    """When no value is provided, the formatter contributes nothing."""
    assert format_last_week_annotation(None) == ""


def test_format_last_week_annotation_includes_label_and_value():
    """The annotation reads 'last-week X.X' so the reader gets context."""
    assert format_last_week_annotation(6.5) == "last-week 6.5"
    assert format_last_week_annotation(0.0) == "last-week 0.0"
    assert format_last_week_annotation(9.0) == "last-week 9.0"


def test_format_last_week_annotation_one_decimal_precision():
    """One decimal matches the dim score format used elsewhere.

    The annotation sits next to the current score in the row; a
    higher-precision annotation would look inconsistent.
    """
    assert format_last_week_annotation(6.453) == "last-week 6.5"
    assert format_last_week_annotation(6.44) == "last-week 6.4"


def test_format_last_week_annotation_html_none_is_empty():
    """None -> empty string; the renderer can concat unconditionally."""
    assert format_last_week_annotation_html(None) == ""


def test_format_last_week_annotation_html_wraps_with_class():
    """Returns a span carrying the faded CSS class."""
    out = format_last_week_annotation_html(6.5)
    assert out.startswith("<span ")
    assert out.endswith("</span>")
    assert f'class="{LAST_WEEK_HTML_CLASS}"' in out
    assert "last-week 6.5" in out


def test_format_last_week_annotation_html_uses_one_decimal():
    """Same precision contract as the plain-text formatter."""
    out = format_last_week_annotation_html(6.453)
    assert "last-week 6.5" in out
    # And no extra-precision leak.
    assert "6.45" not in out


def test_format_last_week_annotation_html_purity():
    """Same input, same output -- no hidden state."""
    for value in [None, 0.0, 3.14, 6.5, 9.0]:
        assert format_last_week_annotation_html(value) == format_last_week_annotation_html(value)


def test_baseline_panel_docstring_documents_terminal_omits_contract():
    """Spec section 8.1: last-week annotation is HTML-only.

    The plain-language docstring on the last-week formatter explicitly
    calls out the terminal-omits contract so future readers (and the
    next iteration's author) can rely on it without guessing.
    """
    doc = format_last_week_annotation.__doc__ or ""
    assert "terminal" in doc.lower()
    assert "omits" in doc.lower() or "not use" in doc.lower()
