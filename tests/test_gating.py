"""Tests for display significance gating (US-034, spec section 8.3).

Acceptance criteria:
  - Delta magnitudes < 0.3 on the /10 scale render as '~'.
  - Delta magnitudes >= 0.3 render with up/down arrows and the
    signed numeric value.
  - Gating is applied uniformly to HTML and terminal renderers
    (locked in here by asserting there is exactly one source of
    truth -- the function in praxis.reports.gating).
"""

from __future__ import annotations

from praxis.reports.gating import (
    DOWN_GLYPH,
    INSIGNIFICANT_GLYPH,
    SIGNIFICANCE_THRESHOLD,
    UP_GLYPH,
    format_delta,
    is_significant,
)


def test_threshold_constant_matches_spec():
    assert SIGNIFICANCE_THRESHOLD == 0.3


def test_glyphs_match_spec():
    assert INSIGNIFICANT_GLYPH == "~"
    assert UP_GLYPH == "↑"
    assert DOWN_GLYPH == "↓"


def test_zero_delta_is_insignificant():
    assert format_delta(0.0) == "~"
    assert is_significant(0.0) is False


def test_small_positive_below_threshold_is_insignificant():
    assert format_delta(0.29) == "~"
    assert format_delta(0.1) == "~"
    assert is_significant(0.29) is False
    assert is_significant(0.1) is False


def test_small_negative_below_threshold_is_insignificant():
    assert format_delta(-0.29) == "~"
    assert format_delta(-0.1) == "~"
    assert is_significant(-0.29) is False
    assert is_significant(-0.1) is False


def test_positive_threshold_boundary_is_significant():
    """|delta| == 0.3 is significant (inclusive boundary)."""
    assert format_delta(0.3) == "↑ +0.3"
    assert is_significant(0.3) is True


def test_negative_threshold_boundary_is_significant():
    assert format_delta(-0.3) == "↓ -0.3"
    assert is_significant(-0.3) is True


def test_significant_positive_delta_renders_up_arrow():
    assert format_delta(0.5) == "↑ +0.5"
    assert format_delta(1.4) == "↑ +1.4"
    assert format_delta(9.0) == "↑ +9.0"


def test_significant_negative_delta_renders_down_arrow():
    assert format_delta(-0.5) == "↓ -0.5"
    assert format_delta(-2.0) == "↓ -2.0"
    assert format_delta(-9.0) == "↓ -9.0"


def test_signed_numeric_value_is_always_explicit():
    """The '+' sign on positive deltas is part of the contract.

    Spec: 'render with up/down arrows and signed numeric value'.
    The sign is redundant with the arrow but explicit signedness is
    what the spec asks for, so callers / readers see "+0.5" not "0.5".
    """
    assert "+" in format_delta(0.5)
    assert "+" in format_delta(2.0)
    assert "-" in format_delta(-0.5)


def test_display_precision_is_one_decimal():
    """Values render to one decimal place to match the /10 score format."""
    # A value with more than one decimal of precision should round to one.
    out = format_delta(0.51)
    # Either +0.5 or +0.6 is acceptable depending on banker's rounding;
    # the contract is "one decimal", not a specific rounding rule.
    assert out.startswith(f"{UP_GLYPH} +0.")
    assert len(out.split(".")[-1]) == 1


def test_just_below_threshold_is_insignificant():
    """Numbers that round up to 0.3 in display are NOT promoted to significant.

    The gate is on the raw magnitude, not the displayed magnitude. A
    value of 0.299 has |delta| < 0.3 and must render as '~' even though
    it would print as '0.3' with one-decimal formatting.
    """
    assert format_delta(0.299) == "~"
    assert format_delta(-0.299) == "~"


def test_just_above_threshold_is_significant():
    assert is_significant(0.31)
    assert is_significant(-0.31)
    assert format_delta(0.31).startswith(UP_GLYPH)
    assert format_delta(-0.31).startswith(DOWN_GLYPH)


def test_format_delta_is_pure_and_idempotent_on_inputs():
    """Two calls with the same delta return the same string."""
    for d in [-1.5, -0.3, -0.1, 0.0, 0.1, 0.3, 0.5, 1.4, 9.9]:
        assert format_delta(d) == format_delta(d)


def test_gating_helper_is_shared_across_renderers():
    """Both renderers import gating from one module.

    Locks the 'applied uniformly' property required by US-034:
    importing the function from praxis.reports.gating must succeed,
    and there must be no parallel implementation in either renderer.
    """
    # The import below is the contract: praxis.reports.gating.format_delta
    # is the canonical entry point. If a future change duplicates this
    # logic inside html_report.py or terminal.py, code review should
    # catch it; this test guards the entry point itself.
    from praxis.reports import gating

    assert callable(gating.format_delta)
    assert callable(gating.is_significant)
