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
BASELINE_FORMING_MESSAGE = (
    "Baseline forming. Come back in 2 more weeks for week-over-week."
)


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
