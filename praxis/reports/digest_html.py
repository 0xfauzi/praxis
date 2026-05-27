"""Self-contained HTML weekly digest renderer (spec section 13.1).

The digest file is written to ``~/.praxis/weeks/<iso>.html`` and must
open by double-click in a default browser with no network access.
This module is the renderer; persistence (writing to disk, updating the
``latest.html`` symlink) is handled by a separate story.

Self-containment rules (US-061 acceptance):

* All CSS lives in an inline ``<style>`` block. No ``<link rel="stylesheet">``.
* No webfonts. The font stack lists system faces only (Georgia primary,
  with a sans-serif fallback for chrome). A later story (US-063)
  re-introduces Libre Baskerville via the v0.1 inlined-fallback path.
* No ``<script>`` tags - the digest is static reading material.
* No ``<img>`` tags. If imagery is ever required, it must be inlined as
  a ``data:`` URI so the file remains portable.
* No ``@import`` URLs in CSS.

Section order is fixed by spec section 6.1. The masthead carries the week
title and generation date but never the overall /10 - the eye lands on
the trajectory headline first, the headline moment second, and the
six-dim panel only after the cost ledger. See ``_SECTIONS`` below for
the contract; ``render`` walks that list in order so the document
structure cannot drift from the spec.

The renderer is a pure function from a ``WeeklyDigest`` dataclass to an
HTML string. The dataclass carries optional panels for trajectory, the
headline moment, the cost ledger, the task breakdown, dimension rows,
the follow-up, and the "one thing to try" sentence. Each panel renders
as a section even when the data is missing, so the order is locked
regardless of which fields are populated; later stories fill in the
content side and the visual language (US-063), the file output path
(US-064), and the no-secrets / no-synthetic-markers guarantee (US-065).
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Trajectory:
    """Trajectory headline + confidence band (spec section 7).

    The label is the categorical read ("Learning", "Steady", "Drifting",
    etc.); the headline is the LLM-generated specific sentence; the
    confidence_band is the short qualifier shown alongside ("high
    confidence", "low confidence - 4 weekly buckets", etc.).
    """

    label: str = ""
    headline: str = ""
    confidence_band: str = ""


@dataclass(frozen=True)
class MomentPanel:
    """The headline moment of the week (spec section 4.3)."""

    quoted_excerpt: str = ""
    why_lost_score: str = ""
    next_time_try: str = ""
    cost_dollars: float | None = None
    cost_minutes: int | None = None


@dataclass(frozen=True)
class CostLedger:
    """Cost ledger panel (spec section 10)."""

    this_week_dollars: float = 0.0
    baseline_dollars: float = 0.0
    biggest_line: str = ""
    sonnet_swap_note: str = ""


@dataclass(frozen=True)
class TaskRow:
    """One row of the 'where the week went' breakdown (spec section 5.3)."""

    label: str = ""
    session_count: int = 0
    dollars: float = 0.0
    worst_score: float | None = None


@dataclass(frozen=True)
class DimRow:
    """One row of the six-dim panel (spec section 8)."""

    title: str = ""
    score: float = 0.0
    baseline: float | None = None
    delta: float | None = None


@dataclass(frozen=True)
class FollowUpPanel:
    """Follow-up from last week (spec section 6.3)."""

    commitment_text: str = ""
    outcome: str = ""  # "improved" | "unchanged" | "worse" | "pending"


@dataclass(frozen=True)
class WeeklyDigest:
    """Input contract for one weekly digest render.

    Carries the seven section panels per spec section 6.1, all optional
    so the renderer can produce a structurally valid document at any
    point in the pipeline (e.g. when an early-week run has no follow-up
    yet, or no moments were emitted). The section order is enforced by
    ``render``; missing data renders as a "not yet" placeholder rather
    than dropping the section, so the document shape is stable.
    """

    week_iso: str
    generated_at: datetime
    trajectory: Trajectory | None = None
    headline_moment: MomentPanel | None = None
    cost_ledger: CostLedger | None = None
    task_breakdown: tuple[TaskRow, ...] = ()
    dimensions: tuple[DimRow, ...] = ()
    follow_up: FollowUpPanel | None = None
    one_thing_to_try: str = ""


# ---------------------------------------------------------------- section text
#
# These IDs and titles are the test-anchorable contract for spec section
# 6.1's section order. The list is walked top-down by `render`; reordering
# requires changing the spec and the tests.

_PLACEHOLDER = "Not yet - this section will fill in as the week's data lands."


def _trajectory_section(traj: Trajectory | None) -> str:
    if traj is None or not (traj.label or traj.headline):
        body = f'<p class="placeholder">{_PLACEHOLDER}</p>'
    else:
        label = html.escape(traj.label) if traj.label else ""
        headline = html.escape(traj.headline) if traj.headline else ""
        band = html.escape(traj.confidence_band) if traj.confidence_band else ""
        label_html = f'<div class="traj-label">{label}</div>' if label else ""
        headline_html = (
            f'<p class="traj-headline">{headline}</p>' if headline else ""
        )
        band_html = f'<div class="traj-band">{band}</div>' if band else ""
        body = f"{label_html}{headline_html}{band_html}"
    return f"""
  <section class="section trajectory" id="trajectory">
    <div class="eyebrow">Trajectory</div>
    {body}
  </section>"""


def _moment_section(moment: MomentPanel | None) -> str:
    if moment is None or not (moment.quoted_excerpt or moment.why_lost_score):
        body = f'<p class="placeholder">{_PLACEHOLDER}</p>'
    else:
        quote = html.escape(moment.quoted_excerpt) if moment.quoted_excerpt else ""
        why = html.escape(moment.why_lost_score) if moment.why_lost_score else ""
        nxt = html.escape(moment.next_time_try) if moment.next_time_try else ""
        cost_bits: list[str] = []
        if moment.cost_dollars is not None:
            cost_bits.append(f"${moment.cost_dollars:.2f}")
        if moment.cost_minutes is not None:
            cost_bits.append(f"{moment.cost_minutes} min")
        cost_text = " / ".join(cost_bits)
        quote_html = (
            f'<blockquote class="moment-quote">{quote}</blockquote>' if quote else ""
        )
        why_html = (
            f'<p class="moment-line"><span class="moment-label">Why this lost score:</span> {why}</p>'
            if why
            else ""
        )
        nxt_html = (
            f'<p class="moment-line"><span class="moment-label">Next time, try:</span> {nxt}</p>'
            if nxt
            else ""
        )
        cost_html = (
            f'<p class="moment-line"><span class="moment-label">Estimated cost of this lapse:</span> {cost_text}</p>'
            if cost_text
            else ""
        )
        body = f"{quote_html}{why_html}{nxt_html}{cost_html}"
    return f"""
  <section class="section moment" id="this-weeks-moment">
    <h2 class="section-title">This Week's Moment</h2>
    {body}
  </section>"""


def _cost_ledger_section(ledger: CostLedger | None) -> str:
    if ledger is None or not (ledger.this_week_dollars or ledger.biggest_line):
        body = f'<p class="placeholder">{_PLACEHOLDER}</p>'
    else:
        diff = ledger.this_week_dollars - ledger.baseline_dollars
        if diff > 0.005:
            tag = f"over by ${diff:.2f}"
        elif diff < -0.005:
            tag = f"under by ${-diff:.2f}"
        else:
            tag = "on baseline"
        rows: list[str] = []
        rows.append(
            f'<p class="ledger-row"><span class="ledger-label">This week:</span> '
            f"${ledger.this_week_dollars:.2f} "
            f'<span class="ledger-aside">(vs ${ledger.baseline_dollars:.2f} baseline, {tag})</span></p>'
        )
        if ledger.biggest_line:
            rows.append(
                f'<p class="ledger-row"><span class="ledger-label">Biggest line:</span> '
                f"{html.escape(ledger.biggest_line)}</p>"
            )
        if ledger.sonnet_swap_note:
            rows.append(
                f'<p class="ledger-row">{html.escape(ledger.sonnet_swap_note)}</p>'
            )
        body = "".join(rows)
    return f"""
  <section class="section cost-ledger" id="cost-ledger">
    <h2 class="section-title">Cost Ledger</h2>
    {body}
  </section>"""


def _task_breakdown_section(rows: tuple[TaskRow, ...]) -> str:
    if not rows:
        body = f'<p class="placeholder">{_PLACEHOLDER}</p>'
    else:
        items: list[str] = []
        for idx, row in enumerate(rows, start=1):
            worst = (
                f' &middot; worst: {row.worst_score:.1f}'
                if row.worst_score is not None
                else ""
            )
            items.append(
                f'<li class="task-row">'
                f'<span class="task-rank">{idx}.</span> '
                f'<span class="task-label">{html.escape(row.label)}</span> '
                f'&middot; {row.session_count} session'
                f'{"s" if row.session_count != 1 else ""} '
                f'&middot; ${row.dollars:.2f}{worst}'
                f"</li>"
            )
        body = f'<ol class="task-list">{"".join(items)}</ol>'
    return f"""
  <section class="section task-breakdown" id="where-the-week-went">
    <h2 class="section-title">Where The Week Went</h2>
    {body}
  </section>"""


def _dimensions_section(rows: tuple[DimRow, ...]) -> str:
    if not rows:
        body = f'<p class="placeholder">{_PLACEHOLDER}</p>'
    else:
        items: list[str] = []
        for row in rows:
            baseline_bit = (
                f' (baseline {row.baseline:.1f}'
                if row.baseline is not None
                else ""
            )
            delta_bit = ""
            if row.delta is not None and row.baseline is not None:
                sign = "+" if row.delta >= 0 else ""
                delta_bit = f", {sign}{row.delta:.1f})"
            elif row.baseline is not None:
                delta_bit = ")"
            items.append(
                f'<li class="dim-row">'
                f'<span class="dim-title">{html.escape(row.title)}</span> '
                f'<span class="dim-score">{row.score:.1f}</span>'
                f'<span class="dim-aside">{baseline_bit}{delta_bit}</span>'
                f"</li>"
            )
        body = f'<ul class="dim-list">{"".join(items)}</ul>'
    return f"""
  <section class="section dimensions" id="the-six-dimensions">
    <h2 class="section-title">The Six Dimensions</h2>
    {body}
  </section>"""


def _follow_up_section(follow_up: FollowUpPanel | None) -> str:
    if follow_up is None or not follow_up.commitment_text:
        body = f'<p class="placeholder">{_PLACEHOLDER}</p>'
    else:
        commitment = html.escape(follow_up.commitment_text)
        outcome = (
            html.escape(follow_up.outcome) if follow_up.outcome else "pending"
        )
        body = (
            f'<p class="follow-up-line">Last week we asked you to {commitment}.</p>'
            f'<p class="follow-up-line">This week\'s data shows: '
            f'<span class="follow-up-outcome">{outcome}</span>.</p>'
        )
    return f"""
  <section class="section follow-up" id="follow-up-from-last-week">
    <h2 class="section-title">Follow-up From Last Week</h2>
    {body}
  </section>"""


def _next_week_section(sentence: str) -> str:
    if not sentence:
        body = f'<p class="placeholder">{_PLACEHOLDER}</p>'
    else:
        body = f'<p class="next-week-line">{html.escape(sentence)}</p>'
    return f"""
  <section class="section next-week" id="one-thing-to-try-next-week">
    <h2 class="section-title">One Thing To Try Next Week</h2>
    {body}
  </section>"""


def render(digest: WeeklyDigest) -> str:
    """Render the weekly digest as a self-contained HTML document.

    The output meets spec section 13.1's "fully self-contained, opens by
    double-click" requirement: all CSS is inlined, and no external CSS,
    JS, fonts, or images are referenced. Sections appear in the order
    fixed by spec section 6.1: trajectory headline + confidence band,
    This Week's Moment, Cost Ledger, Where The Week Went, The Six
    Dimensions, Follow-up From Last Week, One Thing To Try Next Week.
    The masthead carries the week title and generation date only - the
    overall /10 is intentionally not the lead.
    """
    week = html.escape(digest.week_iso)
    generated = html.escape(digest.generated_at.strftime("%B %d, %Y"))
    body_sections = "".join(
        [
            _trajectory_section(digest.trajectory),
            _moment_section(digest.headline_moment),
            _cost_ledger_section(digest.cost_ledger),
            _task_breakdown_section(digest.task_breakdown),
            _dimensions_section(digest.dimensions),
            _follow_up_section(digest.follow_up),
            _next_week_section(digest.one_thing_to_try),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Praxis - Week of {week}</title>
<style>
:root {{
  --primary: #C1573B;
  --cream-100: #FAF7F2;
  --ink-900: #1a1a1a;
  --ink-500: #6b6b6b;
  --border-light: rgba(26, 26, 26, 0.08);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{
  background: var(--cream-100);
  color: var(--ink-900);
  font-family: Georgia, serif;
  font-size: 16px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
.page {{ max-width: 880px; margin: 0 auto; padding: 80px 32px 120px; }}
.masthead {{
  border-bottom: 1px solid var(--border-light);
  padding-bottom: 32px;
  margin-bottom: 48px;
}}
.eyebrow {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--primary);
  font-weight: 500;
}}
.masthead-title {{
  font-family: Georgia, serif;
  font-size: 44px;
  line-height: 1.15;
  margin-top: 16px;
  letter-spacing: -0.01em;
}}
.masthead-meta {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: var(--ink-500);
  margin-top: 24px;
}}
.section {{
  padding: 32px 0;
  border-top: 1px solid var(--border-light);
}}
.section:first-of-type {{ border-top: none; padding-top: 8px; }}
.section-title {{
  font-family: Georgia, serif;
  font-size: 22px;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  margin-bottom: 16px;
  color: var(--ink-900);
}}
.placeholder {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 13px;
  color: var(--ink-500);
  font-style: italic;
}}
.traj-label {{
  font-family: Georgia, serif;
  font-size: 28px;
  color: var(--primary);
  letter-spacing: -0.01em;
  margin-bottom: 12px;
}}
.traj-headline {{
  font-family: Georgia, serif;
  font-size: 20px;
  line-height: 1.5;
  color: var(--ink-900);
}}
.traj-band {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink-500);
  margin-top: 12px;
}}
.moment-quote {{
  font-family: Georgia, serif;
  font-size: 18px;
  line-height: 1.55;
  color: var(--ink-900);
  border-left: 3px solid var(--primary);
  padding: 8px 0 8px 20px;
  margin-bottom: 16px;
  font-style: italic;
}}
.moment-line, .ledger-row, .follow-up-line, .next-week-line {{
  font-size: 15px;
  line-height: 1.6;
  color: var(--ink-900);
  margin-bottom: 6px;
}}
.moment-label, .ledger-label {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink-500);
  margin-right: 4px;
}}
.ledger-aside, .dim-aside {{
  color: var(--ink-500);
  font-size: 13px;
}}
.task-list, .dim-list {{
  list-style: none;
  padding-left: 0;
}}
.task-row, .dim-row {{
  padding: 8px 0;
  border-bottom: 1px solid var(--border-light);
  font-size: 15px;
}}
.task-row:last-child, .dim-row:last-child {{ border-bottom: none; }}
.task-rank, .dim-score {{
  font-family: Georgia, serif;
  color: var(--primary);
  font-weight: 400;
  margin-right: 6px;
}}
.dim-title {{
  display: inline-block;
  min-width: 180px;
}}
.follow-up-outcome {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--primary);
}}
.next-week-line {{
  font-family: Georgia, serif;
  font-size: 18px;
  line-height: 1.55;
  font-style: italic;
  color: var(--ink-900);
}}
</style>
</head>
<body>
<main class="page">
  <header class="masthead">
    <div class="eyebrow">Praxis &middot; Weekly Read</div>
    <h1 class="masthead-title">Week of {week}</h1>
    <div class="masthead-meta">Generated {generated}</div>
  </header>
{body_sections}
</main>
</body>
</html>"""
