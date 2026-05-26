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
    format_baseline_value,
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
            assert format_baseline_value(value, forming) == format_baseline_value(
                value, forming
            )


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
