"""Cost-ledger renderer helpers.

Per spec section 10.2 every digest that exposes cost figures must carry
the literal disclaimer ``rough estimate from token volume + tier
pricing`` exactly once, at the bottom. The constant lives here -- not
in ``praxis.scoring.cost_ledger`` -- because the scoring module is
framing-agnostic (it just produces numbers); the framing is a renderer
concern. Both ``praxis/reports/terminal.py`` and
``praxis/reports/html_report.py`` import the same constant so the two
surfaces cannot drift on the exact prose.

The disclaimer is unconditional: the digest always renders some cost
context (per-model cost line in section 5, and the weekly cost panel
once US-051's downstream wiring lands), so the framing is always
relevant. Keeping it unconditional avoids a class of bug where a
digest with cost figures forgets to opt in.

The literal text is fixed by spec; do NOT reword. The accompanying
sentence ("Do not present these as invoiced costs.") drives a
companion contract enforced by the renderer tests: no dollar amount
in either surface is presented as an invoice.
"""

from __future__ import annotations

COST_LEDGER_DISCLAIMER = "rough estimate from token volume + tier pricing"

# CSS class used by the HTML colophon to style the disclaimer line
# subtly. Kept here so the formatter, the renderer, and the tests all
# reference the same string -- one audit point if the class ever moves.
COST_DISCLAIMER_HTML_CLASS = "cost-disclaimer"


def format_cost_disclaimer() -> str:
    """Return the disclaimer literal for plain-text (terminal) digests.

    Returned verbatim with no prefix or suffix so the caller controls
    placement and surrounding whitespace. The literal MUST match the
    spec section 10.2 prose, character for character.
    """
    return COST_LEDGER_DISCLAIMER


def format_cost_disclaimer_html() -> str:
    """Return the disclaimer wrapped in a span with the muted CSS class.

    The HTML renderer's stylesheet styles ``.cost-disclaimer`` as a
    faded line so the disclaimer reads as a footer caveat, not as a
    headline. The literal text inside the span is the same constant
    used by the terminal renderer.
    """
    return f'<span class="{COST_DISCLAIMER_HTML_CLASS}">{COST_LEDGER_DISCLAIMER}</span>'
