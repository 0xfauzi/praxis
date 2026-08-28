"""Display significance gating for /10-scale deltas.

Per spec section 8.3, deltas smaller than 0.3 on the /10 scale render
as `~` (unchanged). Only deltas >= 0.3 are rendered with up/down arrows
plus the signed numeric value. The rationale, from the spec: this
avoids selling noise as signal -- a 0.2-point swing on a /10 score is
within the noise band of week-to-week sampling and dressing it up with
a green arrow would mislead the reader.

Gating is purely a display concern (section 0 of the spec is explicit
that this is UX, not coaching), so the helper lives under
`praxis/reports/` rather than `praxis/scoring/`. It is renderer-agnostic:
both `html_report.py` and `terminal.py` import and call this function
when rendering a delta, which is how the "applied uniformly" property
required by US-034 is enforced -- there is one source of truth, not two.
"""

from __future__ import annotations

SIGNIFICANCE_THRESHOLD = 0.3
INSIGNIFICANT_GLYPH = "~"
UP_GLYPH = "↑"  # ↑
DOWN_GLYPH = "↓"  # ↓


def is_significant(delta: float) -> bool:
    """True iff |delta| >= 0.3 on the /10 scale."""
    return abs(delta) >= SIGNIFICANCE_THRESHOLD


def format_delta(delta: float) -> str:
    """Render a /10-scale delta with significance gating.

    Returns:
      - "~" when |delta| < 0.3 (insignificant; rendered the same in
        HTML and terminal so the noise band looks identical on both
        surfaces).
      - "↑ +X.X" for significant positive deltas.
      - "↓ -X.X" for significant negative deltas.

    The numeric value is formatted to one decimal place to match the
    /10 score precision used elsewhere in both renderers (e.g.,
    ``f"{score:.1f}"`` in `_dimension_bar_html` and `_dimensions`).
    """
    if not is_significant(delta):
        return INSIGNIFICANT_GLYPH
    arrow = UP_GLYPH if delta > 0 else DOWN_GLYPH
    return f"{arrow} {delta:+.1f}"
