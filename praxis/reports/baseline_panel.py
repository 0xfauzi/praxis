"""Baseline panel rendering helpers.

Per spec section 8.4, when the user has less than 14 days of data the
baseline does not yet exist. The display rules are:

  - Baseline values render as ``--`` (not a number).
  - Deltas are not displayed at all.
  - A "Baseline forming. Come back in 2 more weeks for week-over-week."
    note replaces the per-dim baseline annotation.
  - Last-week mean still renders if at least one prior week of data
    exists (see ``praxis.scoring.baseline.has_prior_week_sessions``).

Like ``gating.py``, this is purely a display concern (spec section 0
is explicit that display gating is UX, not coaching). The helpers live
under ``praxis/reports/`` rather than ``praxis/scoring/`` so the HTML
and terminal renderers both call into the same formatters: one source
of truth keeps the panel state identical on both surfaces, which is the
"applied uniformly" property required by the spec.
"""

from __future__ import annotations

BASELINE_PLACEHOLDER = "--"
BASELINE_FORMING_MESSAGE = "Baseline forming. Come back in 2 more weeks for week-over-week."

# CSS class name used by the HTML report to style the last-week
# secondary annotation as a faded anchor (spec section 8.1). Kept as
# a module-level constant so the formatter, the renderer, and the
# tests all reference the same string -- a rename here is a single
# audit point.
LAST_WEEK_HTML_CLASS = "dim-last-week"


def format_baseline_value(value: float, forming: bool) -> str:
    """Render a baseline /10-scale value with forming-state gating.

    Returns:
      - ``--`` when the baseline is forming (spec section 8.4: less
        than 14 days of data).
      - ``"X.X"`` (one decimal, matching the format used elsewhere by
        ``_dimension_bar_html`` and ``_dimensions``) otherwise.

    The caller decides whether the baseline is forming by passing the
    result of ``is_baseline_forming(sessions, as_of)``. This helper
    deliberately does NOT take the session list directly: the forming
    decision can also be threaded in from cached state (e.g., a
    pre-computed Baseline panel state object), so coupling it to the
    raw session list would force unrelated callers to carry it around.
    """
    if forming:
        return BASELINE_PLACEHOLDER
    return f"{value:.1f}"


def format_last_week_annotation(value: float | None) -> str:
    """Plain-text last-week annotation, or empty when no data exists.

    Returns ``"last-week X.X"`` (one decimal, matching the dim-score
    format) when ``value`` is provided, or ``""`` when ``value`` is
    ``None``. The empty string lets callers concatenate the annotation
    unconditionally: a missing last-week mean simply contributes
    nothing to the rendered row, no per-call-site guard needed.

    The HTML renderer wraps this in a faded span via
    ``format_last_week_annotation_html``. The terminal renderer does
    NOT use either helper -- spec section 8.1 omits the last-week
    annotation from terminal output, and the absence of a terminal
    call site is part of the contract enforced by the renderer tests.
    """
    if value is None:
        return ""
    return f"last-week {value:.1f}"


def format_last_week_annotation_html(value: float | None) -> str:
    """HTML span carrying the last-week annotation with the muted class.

    Returns an empty string when ``value`` is ``None`` so callers can
    concatenate unconditionally. When a value is provided, returns a
    ``<span class="dim-last-week">last-week X.X</span>`` that the HTML
    report styles as a faded secondary anchor (spec section 8.1).

    The value is a float formatted via ``:.1f``, so no HTML escaping
    is required on the numeric content. The span class name is taken
    from ``LAST_WEEK_HTML_CLASS`` so the formatter, the CSS rule in
    ``html_report.py``, and the tests all agree on a single string.
    """
    text = format_last_week_annotation(value)
    if not text:
        return ""
    return f'<span class="{LAST_WEEK_HTML_CLASS}">{text}</span>'
