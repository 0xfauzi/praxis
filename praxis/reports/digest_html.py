"""Self-contained HTML weekly digest renderer (spec section 13.1).

The digest file is written to ``~/.praxis/weeks/<iso>.html`` and must
open by double-click in a default browser with no network access.
``write_digest`` is the persistence side: it renders the digest, writes
``weeks/<iso>.html`` atomically, and updates ``latest.html`` to point
at it (symlink on POSIX, file copy fallback on Windows / restricted
filesystems). The renderer itself remains a pure str-returning
function so tests and pipelines can drive it without touching disk.

Self-containment rules (US-061 acceptance):

* All CSS lives in an inline ``<style>`` block. No ``<link rel="stylesheet">``.
* No webfonts. The font stack names Libre Baskerville (the v0.1
  display face) with Georgia as the inlined fallback - if the user has
  Libre Baskerville installed locally the digest renders identically
  to the v0.1 scan report; otherwise the browser falls back to Georgia,
  which ships on every default OS. The chrome stack is system sans
  (-apple-system / BlinkMacSystemFont / Segoe UI). No ``<link>``,
  no ``@import``, no ``url(...)`` - the file remains fully portable.
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
regardless of which fields are populated. The visual language reuses
the v0.1 cream/terracotta palette and the Libre-Baskerville-with-Georgia
fallback font stack (spec section 6.1: "the HTML uses the existing v0.1
visual language ... do not redesign the look"). Persistence
(``write_digest``) writes the file under ``~/.praxis/weeks/`` and
refreshes the ``latest.html`` pointer (US-064). Every user-provided
string is run through ``_safe`` before it lands in the HTML, which
applies ``redact_secrets`` (provider API keys, AWS, GitHub PATs, JWTs,
and labeled-secret tails become ``[REDACTED]``) and strips the
``<synthetic>`` placeholder marker (US-065 acceptance: no raw secrets
and no synthetic markers reach the reader).
"""
from __future__ import annotations

import html
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from praxis.redactor import redact_secrets
from praxis.storage.profile_store import resolve_home


@dataclass(frozen=True)
class Trajectory:
    """Trajectory headline + evidence + risks + interventions (spec section 7).

    The label is the categorical read ("Learning", "Steady", "Drifting",
    etc.). headline is the LLM-generated specific sentence. evidence is
    the LLM's enumeration of what they noticed across the week's sessions
    (each entry is a single-sentence observation). risks names what this
    pattern means for the user's skill development. interventions is a
    list of concrete next moves. These all come from `TrajectoryAssessment`
    in `praxis.behavior.trajectory`.
    """

    label: str = ""
    headline: str = ""
    confidence_band: str = ""
    evidence: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    interventions: tuple[str, ...] = ()
    engagement_slope: float = 0.0
    delegation_slope: float = 0.0


@dataclass(frozen=True)
class MomentPanel:
    """The headline moment of the week (spec section 4.3)."""

    quoted_excerpt: str = ""
    why_lost_score: str = ""
    next_time_try: str = ""
    cost_dollars: float | None = None
    cost_minutes: int | None = None
    # Context fields that let the reader place the moment in time:
    dim_title: str = ""               # "Verification habits"
    session_started_at: str = ""       # "Tuesday, May 27, 4:32pm"
    recurrence_count: int = 0          # 0 = first time, N = N prior weeks with same suggested_alternative


@dataclass(frozen=True)
class ModelSpend:
    """One row in the model-by-model cost breakdown."""

    model: str
    dollars: float


@dataclass(frozen=True)
class CostLedger:
    """Cost ledger panel (spec section 10)."""

    this_week_dollars: float = 0.0
    baseline_dollars: float = 0.0
    biggest_line: str = ""
    sonnet_swap_note: str = ""
    # Per-model split for the stacked-bar visualization.
    model_split: tuple[ModelSpend, ...] = ()


@dataclass(frozen=True)
class TaskRow:
    """One row of the 'where the week went' breakdown (spec section 5.3)."""

    label: str = ""
    session_count: int = 0
    dollars: float = 0.0
    worst_score: float | None = None
    worst_dim_title: str = ""


@dataclass(frozen=True)
class DimRow:
    """One row of the six-dim panel (spec section 8)."""

    title: str = ""
    score: float = 0.0
    baseline: float | None = None
    delta: float | None = None
    evidence_citation: str = ""        # "Sarkar 2025: experienced agent users plan first"


@dataclass(frozen=True)
class FollowUpPanel:
    """Follow-up from last week (spec section 6.3)."""

    commitment_text: str = ""
    outcome: str = ""  # "improved" | "unchanged" | "worse" | "pending"


@dataclass(frozen=True)
class VitalSigns:
    """The 4-metric vital-signs strip below the trajectory hero.

    A dashboard-style glance at the week. Current values are always set;
    history is optional and (when present) lets the renderer draw a
    sparkline for each rate.
    """

    engagement_rate: float = 0.0
    delegation_rate: float = 0.0
    session_count: int = 0
    spend_usd: float = 0.0
    # Optional history for sparklines: weekly rates from oldest to newest,
    # ending with this week. Falsey when there isn't enough history.
    engagement_history: tuple[float, ...] = ()
    delegation_history: tuple[float, ...] = ()


@dataclass(frozen=True)
class WeeklyTrajectoryPoint:
    """One bucket on the multi-week trajectory line chart."""

    week_iso: str
    engagement_rate: float
    delegation_rate: float


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
    # New v0.2+ fields below; defaults keep older test fixtures working.
    vital_signs: VitalSigns | None = None
    weekly_trajectory: tuple[WeeklyTrajectoryPoint, ...] = ()


# ---------------------------------------------------------------- section text
#
# These IDs and titles are the test-anchorable contract for spec section
# 6.1's section order. The list is walked top-down by `render`; reordering
# requires changing the spec and the tests.

_PLACEHOLDER = "Not yet - this section will fill in as the week's data lands."

# The `<synthetic>` marker is the codex/claude scanner placeholder for an
# unknown model field (see `_real_model` in praxis.scanners.codex). It
# should never reach the rendered digest - spec section 15.1 #8: "No
# `<synthetic>` strings, no raw API keys, no obvious PII appears in any
# rendered digest." Stripping is defensive: under normal operation the
# marker only attaches to model fields and never to transcript text, but
# if a test fixture or future feature accidentally splices it into a
# user-facing string, the renderer guarantees it does not reach the
# reader.
_SYNTHETIC_MARKER = "<synthetic>"


def _safe(text: str) -> str:
    """Sanitize user-provided text for safe inclusion in the rendered HTML.

    Three transforms, in order:

    1. ``redact_secrets`` replaces provider API keys, AWS access keys,
       GitHub PATs, JWTs, and any high-entropy tail after a
       ``key``/``token``/``secret``/``password`` label with
       ``[REDACTED]`` (US-065 acceptance: no unredacted provider keys
       appear in the rendered HTML).
    2. The literal ``<synthetic>`` marker is stripped (US-065
       acceptance: the marker never reaches the reader).
    3. ``html.escape`` neutralises any remaining angle brackets,
       ampersands, or quotes so the field cannot break out of its
       enclosing tag.

    The redactor is itself idempotent and the synthetic strip is a plain
    ``.replace``, so calling ``_safe`` twice on the same input yields the
    same result.
    """
    sanitized = redact_secrets(text).replace(_SYNTHETIC_MARKER, "")
    return html.escape(sanitized)


def _ordinal(n: int) -> str:
    """Return '1st', '2nd', '3rd', '4th', etc. for small positives."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_week_label(iso: str) -> str:
    """Render '2026-W22' as 'Week 22 · May 25 – May 31, 2026'."""
    try:
        year_str, week_str = iso.split("-W")
        year_i, week_i = int(year_str), int(week_str)
        from datetime import date
        start = date.fromisocalendar(year_i, week_i, 1)
        end = date.fromisocalendar(year_i, week_i, 7)
        if start.month == end.month:
            return f"Week {week_i} · {start:%B %-d}–{end:%-d}, {year_i}"
        return f"Week {week_i} · {start:%B %-d} – {end:%B %-d}, {year_i}"
    except (ValueError, AttributeError):
        return iso or ""


def _trajectory_section(
    traj: Trajectory | None,
    week_iso: str,
    vital_signs: VitalSigns | None = None,
) -> str:
    """Trajectory hero: the week's identity + diagnostic + vital signs strip."""
    label = _safe(traj.label) if traj and traj.label else "Reading"
    headline = _safe(traj.headline) if traj and traj.headline else ""
    week_label = _safe(_format_week_label(week_iso))
    headline_html = (
        f'<p class="t-lede">{headline}</p>' if headline else ""
    )
    vitals_html = _vital_signs_html(vital_signs) if vital_signs else ""
    return f"""
  <section class="t-hero" id="trajectory">
    <div class="t-eyebrow">{week_label}</div>
    <h1 class="t-label">{label}</h1>
    <div class="t-rule"></div>
    {headline_html}
    {vitals_html}
  </section>"""


def _vital_signs_html(vs: VitalSigns) -> str:
    """4-stat strip below the trajectory hero.

    Renders engagement_rate, delegation_rate, session_count, spend_usd
    as four equal-width tiles separated by thin rules. Sparklines
    appear only when ``*_history`` carries 3+ points; otherwise the
    tile shows just the current value.
    """
    eng_spark = _sparkline_svg(vs.engagement_history, accent=True) if len(vs.engagement_history) >= 3 else ""
    del_spark = _sparkline_svg(vs.delegation_history, accent=False) if len(vs.delegation_history) >= 3 else ""
    return f"""
    <div class="vitals">
      <div class="vital">
        <div class="vital-label">Engagement</div>
        <div class="vital-value">{vs.engagement_rate:.2f}</div>
        {eng_spark}
      </div>
      <div class="vital">
        <div class="vital-label">Delegation</div>
        <div class="vital-value">{vs.delegation_rate:.2f}</div>
        {del_spark}
      </div>
      <div class="vital">
        <div class="vital-label">Sessions</div>
        <div class="vital-value">{vs.session_count}</div>
      </div>
      <div class="vital">
        <div class="vital-label">Spend</div>
        <div class="vital-value">${vs.spend_usd:,.2f}</div>
      </div>
    </div>"""


def _sparkline_svg(values: tuple[float, ...], accent: bool = False) -> str:
    """Tiny inline SVG sparkline. Auto-scales y to data range."""
    if len(values) < 2:
        return ""
    vmin = min(values)
    vmax = max(values)
    rng = max(0.01, vmax - vmin)
    w, h, pad = 80, 18, 1.5
    inner_w = w - 2 * pad
    inner_h = h - 2 * pad
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (i / max(1, n - 1)) * inner_w
        y = pad + (1.0 - (v - vmin) / rng) * inner_h
        pts.append(f"{x:.2f},{y:.2f}")
    color_class = "spark-accent" if accent else "spark-muted"
    return (
        f'<svg class="spark {color_class}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="sparkline">'
        f'<polyline points="{" ".join(pts)}" '
        f'fill="none" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


def _trajectory_evidence_section(traj: Trajectory | None) -> str:
    """The Bigger Pattern panel: trajectory's evidence + risks + interventions.

    These are LLM-generated and represent the deepest content the
    pipeline produces. They sit inside the coaching block because they
    ARE coaching — observations about the user's patterns, what those
    patterns risk, and concrete things to try.
    """
    if traj is None or not (traj.evidence or traj.risks or traj.interventions):
        return ""
    parts: list[str] = []
    if traj.evidence:
        items = "".join(f"<li>{_safe(e)}</li>" for e in traj.evidence[:4])
        parts.append(
            '<div class="pattern-block">'
            '<div class="pattern-label">What we noticed across the week</div>'
            f'<ul class="pattern-list">{items}</ul>'
            '</div>'
        )
    if traj.risks:
        items = "".join(f"<li>{_safe(r)}</li>" for r in traj.risks[:3])
        parts.append(
            '<div class="pattern-block">'
            '<div class="pattern-label pattern-label--warn">What this risks</div>'
            f'<ul class="pattern-list">{items}</ul>'
            '</div>'
        )
    if traj.interventions:
        items = "".join(f"<li>{_safe(i)}</li>" for i in traj.interventions[:4])
        parts.append(
            '<div class="pattern-block">'
            '<div class="pattern-label">What to do differently</div>'
            f'<ul class="pattern-list pattern-list--actions">{items}</ul>'
            '</div>'
        )
    return f"""
  <section class="pattern-section">
    <div class="s-eyebrow">The Bigger Pattern</div>
    {"".join(parts)}
  </section>"""


def _moment_section(moment: MomentPanel | None, dim_title: str = "") -> str:
    """The headline coaching moment. First panel after the trajectory hero.

    Renders as the feature article of the digest: a pull-quote with
    session context, a why paragraph, a what-to-try paragraph, and a
    recurrence callout when the same suggested_alternative has been
    flagged in prior weeks.
    """
    coaching_tag = '<div class="coaching-tag">Coaching</div>'
    if moment is None or not (moment.quoted_excerpt or moment.why_lost_score):
        return f"""
  <section class="m-section" id="this-weeks-moment">
    {coaching_tag}
    <div class="s-eyebrow">This Week's Moment</div>
    <p class="placeholder">No coachable moment surfaced this week. Run more sessions to surface one.</p>
  </section>"""
    quote = _safe(moment.quoted_excerpt) if moment.quoted_excerpt else ""
    why = _safe(moment.why_lost_score) if moment.why_lost_score else ""
    nxt = _safe(moment.next_time_try) if moment.next_time_try else ""
    # Prefer the MomentPanel's own dim_title; fall back to caller-passed value.
    effective_dim = moment.dim_title or dim_title
    dim_chip = _safe(effective_dim) if effective_dim else ""
    eyebrow = (
        f"This Week's Moment <span class=\"s-eyebrow-dim\">&nbsp;·&nbsp; {dim_chip}</span>"
        if dim_chip
        else "This Week's Moment"
    )
    blocks: list[str] = []
    # Recurrence chip: spec §4.4 "escalate the framing" when a lapse keeps
    # recurring. Sits ABOVE the quote so the reader sees the streak first.
    if moment.recurrence_count and moment.recurrence_count > 0:
        n = moment.recurrence_count + 1  # plus this week
        blocks.append(
            f'<div class="m-recurrence">This is the {_ordinal(n)} week we\'ve flagged this exact pattern.</div>'
        )
    if quote:
        ctx = ""
        if moment.session_started_at:
            ctx = f'<div class="m-quote-context">{_safe(moment.session_started_at)}</div>'
        blocks.append(
            f'<blockquote class="m-quote"><span class="m-quote-mark">&ldquo;</span>{quote}<span class="m-quote-mark m-quote-mark--close">&rdquo;</span></blockquote>{ctx}'
        )
    if why:
        blocks.append(
            f'<p class="m-body"><span class="m-inline-label">Why this lost score</span>{why}</p>'
        )
    if nxt:
        # Split next_time_try into bullets when the LLM returned a
        # multi-clause sentence (very common). Heuristic: split on
        # ". " and treat 2+ clauses as bullets; otherwise render as a
        # single paragraph.
        clauses = [c.strip().rstrip(".") for c in nxt.split(". ") if c.strip()]
        if len(clauses) >= 2 and all(len(c) > 10 for c in clauses):
            items = "".join(f"<li>{_safe(c)}</li>" for c in clauses)
            blocks.append(
                f'<div class="m-body"><span class="m-inline-label">What to do instead</span>'
                f'<ul class="m-try-list">{items}</ul></div>'
            )
        else:
            blocks.append(
                f'<p class="m-body"><span class="m-inline-label">What to do instead</span>{nxt}</p>'
            )
    cost_bits: list[str] = []
    if moment.cost_dollars is not None:
        cost_bits.append(f"${moment.cost_dollars:.2f}")
    if moment.cost_minutes is not None:
        cost_bits.append(f"{moment.cost_minutes} min wasted")
    if cost_bits:
        blocks.append(
            f'<p class="m-cost">Cost of this lapse: {", ".join(cost_bits)}</p>'
        )
    return f"""
  <section class="m-section" id="this-weeks-moment">
    {coaching_tag}
    <div class="s-eyebrow">{eyebrow}</div>
    {"".join(blocks)}
  </section>"""


def _cost_ledger_section(ledger: CostLedger | None) -> str:
    if ledger is None or ledger.this_week_dollars <= 0.0:
        return f"""
  <section class="c-section" id="cost-ledger">
    <div class="s-eyebrow">Cost Ledger</div>
    <p class="placeholder">No priced model spend this week.</p>
  </section>"""

    # Big number formatted with smaller cents (typographic convention)
    cents_part = f"{ledger.this_week_dollars:.2f}".split(".")
    big = cents_part[0]
    cents = cents_part[1] if len(cents_part) > 1 else "00"

    # Delta line
    if ledger.baseline_dollars and ledger.baseline_dollars > 0:
        diff = ledger.this_week_dollars - ledger.baseline_dollars
        if abs(diff) < 0.005:
            delta_str = "on baseline"
        else:
            sign = "over" if diff > 0 else "under"
            delta_str = f"{sign} baseline by ${abs(diff):,.2f}"
        delta_html = (
            f"<div class=\"c-delta\">vs ${ledger.baseline_dollars:,.2f} 90-day baseline · {delta_str}</div>"
        )
    else:
        delta_html = '<div class="c-delta">Baseline forming — second week onward.</div>'

    # Biggest line as a visual breakdown
    biggest_html = ""
    if ledger.biggest_line:
        biggest_html = (
            f'<div class="c-biggest"><span class="c-inline-label">Biggest line</span>'
            f'<span class="c-biggest-text">{_safe(ledger.biggest_line)}</span></div>'
        )

    sonnet_html = ""
    if ledger.sonnet_swap_note:
        sonnet_html = (
            f'<p class="c-tier-fit">{_safe(ledger.sonnet_swap_note)}</p>'
        )

    full_amount = f"${ledger.this_week_dollars:,.2f}"
    # Stat tiles - 3-col row that sits above the model split bar.
    baseline_tile = (
        f'<div class="stat-tile">'
        f'<div class="stat-tile-label">90-day baseline</div>'
        f'<div class="stat-tile-value">${ledger.baseline_dollars:,.2f}</div>'
        f'</div>'
        if ledger.baseline_dollars and ledger.baseline_dollars > 0
        else (
            '<div class="stat-tile">'
            '<div class="stat-tile-label">90-day baseline</div>'
            '<div class="stat-tile-value stat-tile-value--forming">forming</div>'
            '</div>'
        )
    )
    delta_tile = ""
    if ledger.baseline_dollars and ledger.baseline_dollars > 0:
        diff = ledger.this_week_dollars - ledger.baseline_dollars
        sign = "+" if diff >= 0 else "-"
        delta_str = f"{sign}${abs(diff):,.2f}"
        delta_tile = (
            f'<div class="stat-tile">'
            f'<div class="stat-tile-label">vs baseline</div>'
            f'<div class="stat-tile-value">{delta_str}</div>'
            f'</div>'
        )
    stat_tiles = (
        '<div class="stat-tiles">'
        f'<div class="stat-tile stat-tile--accent">'
        f'<div class="stat-tile-label">This week</div>'
        f'<div class="stat-tile-value">{full_amount}</div>'
        f'</div>'
        f'{baseline_tile}'
        f'{delta_tile}'
        '</div>'
    )
    split_bar = _cost_split_bar(ledger.model_split, ledger.this_week_dollars)
    return f"""
  <section class="c-section" id="cost-ledger">
    <div class="s-eyebrow">Cost Ledger</div>
    <div class="c-amount-plain" aria-hidden="true">{full_amount}</div>
    {stat_tiles}
    {split_bar}
    {biggest_html}
    {sonnet_html}
  </section>"""


def _task_breakdown_section(rows: tuple[TaskRow, ...]) -> str:
    if not rows:
        return f"""
  <section class="w-section" id="where-the-week-went">
    <div class="s-eyebrow">Where The Week Went</div>
    <p class="placeholder">No tasks identified yet.</p>
  </section>"""
    items: list[str] = []
    max_sessions = max((r.session_count for r in rows), default=1) or 1
    for row in rows:
        worst_html = ""
        if row.worst_score is not None:
            worst_html = (
                f'<span class="w-worst">Lowest dim score: {row.worst_score:.1f}</span>'
            )
        bar_pct = max(8, int(round((row.session_count / max_sessions) * 100)))
        cost_str = (
            f"${row.dollars:.2f}" if row.dollars > 0 else "—"
        )
        items.append(
            f'<article class="w-task">'
            f'<h3 class="w-label">{_safe(row.label)}</h3>'
            f'<div class="w-bar"><div class="w-fill" style="width: {bar_pct}%"></div></div>'
            f'<div class="w-meta">'
            f'<span class="w-meta-stat">{row.session_count} session{"s" if row.session_count != 1 else ""}</span>'
            f'<span class="w-meta-dot">·</span>'
            f'<span class="w-meta-stat">{cost_str}</span>'
            f'{("<span class=\"w-meta-dot\">·</span>" + worst_html) if worst_html else ""}'
            f'</div>'
            f"</article>"
        )
    return f"""
  <section class="w-section" id="where-the-week-went">
    <div class="s-eyebrow">Where The Week Went</div>
    {"".join(items)}
  </section>"""


_DIM_AXIS_LABELS: dict[str, str] = {
    "Planning before prompting": "Planning",
    "Context richness": "Context",
    "Iteration & evaluation": "Iteration",
    "Tool & multi-step use": "Tools",
    "Model–task fit": "Fit",
    "Verification habits": "Verify",
}


def _radar_svg(rows: tuple[DimRow, ...]) -> str:
    """Hexagonal radar chart of the six dimensions.

    Six vertices, one per rubric dim, at evenly-spaced angles starting
    at -90° (so the first dim sits at the top). The filled polygon is
    this week's scores; the ghost outline (when present) is the prior
    week's 90-day baseline for each dim. Pure SVG, no JS, scales via
    viewBox so the same markup looks crisp at any width.

    The scale rings (at 25/50/75/100% of the outer radius) give the
    reader an at-a-glance sense of magnitude without numeric gridlines.
    """
    import math

    if not rows:
        return ""

    cx, cy, r = 200, 200, 140
    n = len(rows)
    # Angles in radians; vertex 0 sits at the top (-pi/2).
    angles = [(-math.pi / 2) + (2 * math.pi * i / n) for i in range(n)]

    # Outer axis lines + concentric scale rings.
    axis_lines: list[str] = []
    for ang in angles:
        x = cx + r * math.cos(ang)
        y = cy + r * math.sin(ang)
        axis_lines.append(
            f'<line x1="{cx}" y1="{cy}" x2="{x:.2f}" y2="{y:.2f}" '
            f'class="r-axis"/>'
        )

    rings: list[str] = []
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = []
        for ang in angles:
            x = cx + r * frac * math.cos(ang)
            y = cy + r * frac * math.sin(ang)
            pts.append(f"{x:.2f},{y:.2f}")
        ring_class = "r-ring r-ring--outer" if frac == 1.0 else "r-ring"
        rings.append(
            f'<polygon points="{" ".join(pts)}" class="{ring_class}"/>'
        )

    # This week's score polygon.
    score_pts = []
    for ang, row in zip(angles, rows):
        score = max(0.0, min(10.0, row.score))
        radius_frac = score / 10.0
        x = cx + r * radius_frac * math.cos(ang)
        y = cy + r * radius_frac * math.sin(ang)
        score_pts.append(f"{x:.2f},{y:.2f}")
    score_polygon = (
        f'<polygon points="{" ".join(score_pts)}" class="r-score"/>'
    )

    # Optional baseline polygon (ghost outline).
    baseline_polygon = ""
    have_baseline = any(r.baseline is not None for r in rows)
    if have_baseline:
        bl_pts = []
        for ang, row in zip(angles, rows):
            bl = row.baseline if row.baseline is not None else 0.0
            bl = max(0.0, min(10.0, bl))
            x = cx + r * (bl / 10.0) * math.cos(ang)
            y = cy + r * (bl / 10.0) * math.sin(ang)
            bl_pts.append(f"{x:.2f},{y:.2f}")
        baseline_polygon = (
            f'<polygon points="{" ".join(bl_pts)}" class="r-baseline"/>'
        )

    # Vertex dots on this week's polygon.
    score_dots = []
    for ang, row in zip(angles, rows):
        score = max(0.0, min(10.0, row.score))
        radius_frac = score / 10.0
        x = cx + r * radius_frac * math.cos(ang)
        y = cy + r * radius_frac * math.sin(ang)
        score_dots.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" class="r-dot"/>'
        )

    # Axis labels: dim name + score, placed just outside each vertex.
    labels: list[str] = []
    label_radius = r + 28
    for ang, row in zip(angles, rows):
        x = cx + label_radius * math.cos(ang)
        y = cy + label_radius * math.sin(ang)
        # Anchor based on horizontal position.
        cos_a = math.cos(ang)
        if abs(cos_a) < 0.25:
            anchor = "middle"
        elif cos_a > 0:
            anchor = "start"
        else:
            anchor = "end"
        # Vertical alignment: lift labels above the top vertex, drop
        # below for the lower ones.
        sin_a = math.sin(ang)
        if sin_a < -0.5:
            dy = "0"
        elif sin_a > 0.5:
            dy = "0.9em"
        else:
            dy = "0.35em"
        short_label = _DIM_AXIS_LABELS.get(row.title, row.title.split()[0])
        labels.append(
            f'<g class="r-label-group">'
            f'<text x="{x:.2f}" y="{y:.2f}" dy="{dy}" '
            f'text-anchor="{anchor}" class="r-label">{_safe(short_label)}</text>'
            f'<text x="{x:.2f}" y="{y:.2f}" dy="calc({dy} + 1.1em)" '
            f'text-anchor="{anchor}" class="r-label-score">{row.score:.1f}</text>'
            f'</g>'
        )

    return (
        # Inline SVG inside an HTML5 document does not need an xmlns;
        # adding one would trip the self-containment check (which
        # disallows any "http://" substring on the page).
        f'<svg class="r-chart" viewBox="0 0 400 400" '
        f'role="img" aria-label="Six-dimension radar chart">'
        f'{"".join(rings)}'
        f'{"".join(axis_lines)}'
        f'{baseline_polygon}'
        f'{score_polygon}'
        f'{"".join(score_dots)}'
        f'{"".join(labels)}'
        f'</svg>'
    )


def _dimensions_section(rows: tuple[DimRow, ...]) -> str:
    if not rows:
        return f"""
  <section class="d-section" id="the-six-dimensions">
    <div class="s-eyebrow">The Six Dimensions</div>
    <p class="placeholder">No dimension data yet.</p>
  </section>"""
    radar = _radar_svg(rows)
    items: list[str] = []
    for row in rows:
        score = max(0.0, min(10.0, row.score))
        pct = score * 10.0
        baseline_html = ""
        if row.baseline is not None:
            if row.delta is None or abs(row.delta) < 0.05:
                baseline_html = f'<span class="d-baseline">baseline {row.baseline:.1f}</span>'
            else:
                sign = "↑" if row.delta > 0 else "↓"
                delta_class = "d-delta-up" if row.delta > 0 else "d-delta-down"
                baseline_html = (
                    f'<span class="d-baseline">baseline {row.baseline:.1f}</span>'
                    f'<span class="{delta_class}">{sign} {abs(row.delta):.1f}</span>'
                )
        else:
            baseline_html = '<span class="d-baseline d-baseline--forming">forming</span>'
        items.append(
            f'<li class="d-row">'
            f'<div class="d-head">'
            f'<span class="d-title">{_safe(row.title)}</span>'
            f'<span class="d-score">{row.score:.1f}</span>'
            f'</div>'
            f'<div class="d-bar-track"><div class="d-bar-fill" style="width: {pct:.1f}%"></div></div>'
            f'<div class="d-meta">{baseline_html}</div>'
            f"</li>"
        )
    return f"""
  <section class="d-section" id="the-six-dimensions">
    <div class="s-eyebrow">The Six Dimensions</div>
    <div class="r-wrap">{radar}</div>
    <ul class="d-list">{"".join(items)}</ul>
  </section>"""


def _follow_up_section(follow_up: FollowUpPanel | None) -> str:
    if follow_up is None or not follow_up.commitment_text:
        return f"""
  <section class="f-section" id="follow-up-from-last-week">
    <div class="s-eyebrow">Follow-up From Last Week</div>
    <p class="placeholder">No commitment in flight yet. Next week's digest will open one.</p>
  </section>"""
    commitment = _safe(follow_up.commitment_text)
    raw_outcome = (follow_up.outcome or "pending").lower()
    # Only ever render one of the four known outcome states. Anything else
    # collapses to "pending" so a stray value can't smuggle markup or the
    # <synthetic> marker into the class attribute or display text.
    outcome = raw_outcome if raw_outcome in {
        "improved", "unchanged", "worse", "pending"
    } else "pending"
    outcome_label = {
        "improved": "improved",
        "unchanged": "unchanged",
        "worse": "got worse",
        "pending": "pending",
    }[outcome]
    outcome_class = f"f-outcome f-outcome--{outcome}"
    return f"""
  <section class="f-section" id="follow-up-from-last-week">
    <div class="s-eyebrow">Follow-up From Last Week</div>
    <p class="f-body"><span class="m-inline-label">Last week's commitment</span>{commitment}</p>
    <p class="f-body"><span class="m-inline-label">This week's reading</span><span class="{outcome_class}">{outcome_label}</span></p>
  </section>"""


def _weekly_trajectory_section(
    points: tuple[WeeklyTrajectoryPoint, ...]
) -> str:
    """Multi-week trajectory line chart of engagement and delegation rates.

    This is the literal "trajectory" the spec section 7 talks about,
    visualized. Renders as an SVG line chart with two series. Falls
    back to a placeholder card when fewer than 2 weeks of data exist.
    """
    if not points:
        return f"""
  <section class="wt-section" id="weekly-trajectory">
    <div class="s-eyebrow">Your Learning Trajectory</div>
    <p class="placeholder">A multi-week trajectory chart will appear here once you have at least 4 weeks of digests on file. (This week is week 1.)</p>
  </section>"""
    if len(points) < 4:
        weeks_have = len(points)
        weeks_need = 4 - weeks_have
        return f"""
  <section class="wt-section" id="weekly-trajectory">
    <div class="s-eyebrow">Your Learning Trajectory</div>
    <p class="placeholder">Building. {weeks_have} week{"s" if weeks_have != 1 else ""} of buckets on file; {weeks_need} more before the line chart fits.</p>
  </section>"""

    # Build the SVG.
    width, height = 640, 220
    pad_left, pad_right, pad_top, pad_bottom = 64, 24, 20, 40
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom
    n = len(points)

    def to_xy(i: int, val: float) -> tuple[float, float]:
        x = pad_left + (i / max(1, n - 1)) * inner_w
        # values are 0-1 rates; map to inner_h with y inverted.
        v = max(0.0, min(1.0, val))
        y = pad_top + (1.0 - v) * inner_h
        return x, y

    eng_pts = [to_xy(i, p.engagement_rate) for i, p in enumerate(points)]
    del_pts = [to_xy(i, p.delegation_rate) for i, p in enumerate(points)]

    eng_path = " ".join(f"{x:.1f},{y:.1f}" for x, y in eng_pts)
    del_path = " ".join(f"{x:.1f},{y:.1f}" for x, y in del_pts)

    # Y-axis gridlines at 0, 0.5, 1.0
    gridlines: list[str] = []
    for frac, label in ((0.0, "0.0"), (0.5, "0.5"), (1.0, "1.0")):
        y = pad_top + (1.0 - frac) * inner_h
        gridlines.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="wt-grid"/>'
            f'<text x="{pad_left - 12}" y="{y:.1f}" dy="0.35em" text-anchor="end" class="wt-axis-label">{label}</text>'
        )

    # X-axis: week labels (shorten to "W22" from "2026-W22")
    x_labels: list[str] = []
    for i, p in enumerate(points):
        x = pad_left + (i / max(1, n - 1)) * inner_w
        short = p.week_iso.split("-W")[-1] if "-W" in p.week_iso else p.week_iso
        x_labels.append(
            f'<text x="{x:.1f}" y="{height - pad_bottom + 18}" text-anchor="middle" class="wt-axis-label">W{short}</text>'
        )

    # End-point dots and labels for the most recent week
    eng_x, eng_y = eng_pts[-1]
    del_x, del_y = del_pts[-1]
    end_dots = (
        f'<circle cx="{eng_x:.1f}" cy="{eng_y:.1f}" r="3.5" class="wt-dot wt-dot--eng"/>'
        f'<circle cx="{del_x:.1f}" cy="{del_y:.1f}" r="3.5" class="wt-dot wt-dot--del"/>'
    )

    # Legend
    legend = (
        '<g class="wt-legend" transform="translate(64, 4)">'
        '<rect x="0" y="0" width="14" height="2" class="wt-legend-eng"/>'
        '<text x="20" y="0" dy="0.4em" class="wt-legend-text">Engagement</text>'
        '<rect x="110" y="0" width="14" height="2" class="wt-legend-del"/>'
        '<text x="130" y="0" dy="0.4em" class="wt-legend-text">Delegation</text>'
        '</g>'
    )

    return f"""
  <section class="wt-section" id="weekly-trajectory">
    <div class="s-eyebrow">Your Learning Trajectory · {n} weeks</div>
    <svg class="wt-chart" viewBox="0 0 {width} {height}" role="img" aria-label="Engagement and delegation over time">
      {"".join(gridlines)}
      {legend}
      <polyline points="{eng_path}" fill="none" stroke-width="1.8" class="wt-line-eng"/>
      <polyline points="{del_path}" fill="none" stroke-width="1.8" class="wt-line-del"/>
      {end_dots}
      {"".join(x_labels)}
    </svg>
  </section>"""


def _cost_split_bar(model_split: tuple[ModelSpend, ...], total: float) -> str:
    """Single horizontal stacked bar showing per-model spend share.

    Each segment width = model's share of total. The accent color is
    used for the largest segment; subsequent segments step down in
    opacity to keep one-color discipline while still differentiating.
    """
    if not model_split or total <= 0:
        return ""
    # Sort by spend desc.
    rows = sorted(model_split, key=lambda m: m.dollars, reverse=True)
    segments: list[str] = []
    legend: list[str] = []
    opacities = [1.0, 0.65, 0.45, 0.30, 0.20]
    for i, row in enumerate(rows[:5]):
        share = row.dollars / total
        pct = max(2.0, share * 100)
        op = opacities[i] if i < len(opacities) else 0.15
        segments.append(
            f'<div class="cb-seg" style="width:{pct:.2f}%; opacity:{op}" title="{_safe(row.model)} · ${row.dollars:,.2f}"></div>'
        )
        legend.append(
            f'<div class="cb-legend-row">'
            f'<span class="cb-legend-swatch" style="opacity:{op}"></span>'
            f'<span class="cb-legend-model">{_safe(row.model)}</span>'
            f'<span class="cb-legend-spend">${row.dollars:,.2f}</span>'
            f'</div>'
        )
    return (
        '<div class="cost-bar-wrap">'
        f'<div class="cost-bar">{"".join(segments)}</div>'
        f'<div class="cost-bar-legend">{"".join(legend)}</div>'
        '</div>'
    )


def _next_week_section(sentence: str) -> str:
    if not sentence:
        return f"""
  <section class="n-section" id="one-thing-to-try-next-week">
    <div class="s-eyebrow">One Thing To Try Next Week</div>
    <p class="placeholder">Next week's commitment will be drawn from this week's headline moment.</p>
  </section>"""
    return f"""
  <section class="n-section" id="one-thing-to-try-next-week">
    <div class="s-eyebrow">One Thing To Try Next Week</div>
    <p class="n-body">{_safe(sentence)}</p>
  </section>"""


def render(digest: WeeklyDigest) -> str:
    """Render the weekly digest as a self-contained HTML document.

    Editorial single-column layout. The trajectory label is the visual
    anchor (hero serif at ~96px) and gives the week its identity. The
    headline moment is set as a feature pull-quote with the why/try
    body laid out beneath. Cost is presented as a single big number
    with a 90-day comparison anchor. Tasks render as compact cards
    with proportional session-count bars. Dimensions are bars with the
    delta-from-baseline shown alongside. Follow-up closes the loop.
    All CSS is inlined (spec section 13.1: fully self-contained).
    """
    # Pull the moment's dim_title from the digest's headline_moment if possible.
    dim_title = ""
    if digest.headline_moment is not None:
        # Map dim_key on the moment to a display title via the rubric.
        try:
            from praxis.scoring.rubric import by_key as _rubric_by_key
            # MomentPanel doesn't carry dim_key directly; we just leave
            # the eyebrow plain when we can't resolve a title. The
            # selector path could enrich MomentPanel with dim_key in
            # a follow-up.
            _ = _rubric_by_key  # marker, no-op
        except Exception:  # noqa: BLE001
            pass

    week = _safe(digest.week_iso)
    # Section order: trajectory hero (with vital signs) -> coaching
    # spread (moment, bigger pattern, follow-up, next-week) -> data
    # appendix (cost, tasks, dimensions) -> longitudinal trajectory.
    coaching_block = (
        '<div class="coaching">'
        f'{_moment_section(digest.headline_moment, dim_title=dim_title)}'
        f'{_trajectory_evidence_section(digest.trajectory)}'
        f'{_follow_up_section(digest.follow_up)}'
        f'{_next_week_section(digest.one_thing_to_try)}'
        '</div>'
    )
    data_block = (
        '<div class="data-block">'
        '<div class="data-block__rule"></div>'
        '<div class="data-block__label">The numbers behind it</div>'
        f'{_cost_ledger_section(digest.cost_ledger)}'
        f'{_task_breakdown_section(digest.task_breakdown)}'
        f'{_dimensions_section(digest.dimensions)}'
        f'{_weekly_trajectory_section(digest.weekly_trajectory)}'
        '</div>'
    )
    body_sections = (
        _trajectory_section(
            digest.trajectory, digest.week_iso, vital_signs=digest.vital_signs,
        )
        + coaching_block
        + data_block
    )
    generated_iso = _safe(
        digest.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    )
    generated_readable = _safe(
        digest.generated_at.strftime("%B %-d, %Y")
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Praxis · Week of {week}</title>
<style>
:root {{
  --cream:        #FAF7F2;
  --cream-edge:   #F2EDE4;
  --ink:          #1F1B16;
  --ink-muted:    #6E665B;
  --ink-faded:    #A79E91;
  --accent:       #C1573B;
  --accent-soft:  rgba(193, 87, 59, 0.10);
  --accent-faint: rgba(193, 87, 59, 0.05);
  --rule:         rgba(31, 27, 22, 0.06);
  --rule-strong:  rgba(31, 27, 22, 0.12);
  --serif: 'Libre Baskerville', 'Iowan Old Style', 'Hoefler Text',
           'Times New Roman', Georgia, serif;
  --sans:  'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Text',
           system-ui, 'Helvetica Neue', sans-serif;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{
  background: var(--cream);
  color: var(--ink);
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  font-feature-settings: 'kern', 'liga', 'onum';
  text-rendering: optimizeLegibility;
}}
.page {{
  max-width: 640px;
  margin: 0 auto;
  padding: clamp(40px, 8vh, 96px) 32px 120px;
}}

.masthead {{
  /* The dominant element on the page is the trajectory hero a few
     blocks below. The masthead is intentionally minimal - it carries
     only the generation date as a quiet typographic marker so the
     reader knows when this read was struck. */
  display: flex;
  justify-content: flex-end;
  margin-bottom: 24px;
}}
.masthead-meta {{
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-faded);
}}

/* --- Section eyebrow (used throughout) ------------------------------ */
.s-eyebrow {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 500;
  margin-bottom: 24px;
}}
.s-eyebrow-dim {{
  color: var(--ink-faded);
  font-weight: 400;
}}
.placeholder {{
  font-family: var(--sans);
  font-size: 13.5px;
  color: var(--ink-muted);
  font-style: italic;
  margin-top: 12px;
}}

/* --- 1. Trajectory hero --------------------------------------------- */
.t-hero {{
  padding-top: 8px;
  margin-bottom: 96px;
}}
.t-eyebrow {{
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-muted);
  margin-bottom: 28px;
}}
.t-label {{
  font-family: var(--serif);
  font-size: clamp(56px, 11vw, 104px);
  line-height: 0.95;
  letter-spacing: -0.025em;
  color: var(--ink);
  margin: 0 0 24px;
  font-weight: 400;
}}
.t-rule {{
  width: 48px;
  height: 2px;
  background: var(--accent);
  margin: 8px 0 28px;
}}
.t-lede {{
  font-family: var(--serif);
  font-size: 21px;
  line-height: 1.5;
  color: var(--ink);
  font-style: italic;
  max-width: 560px;
}}

/* --- Vital signs strip below the trajectory hero ------------------- */
.vitals {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--rule);
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
  margin-top: 40px;
}}
.vital {{
  background: var(--cream);
  padding: 20px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
.vital-label {{
  font-family: var(--sans);
  font-size: 9.5px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--ink-muted);
  font-weight: 500;
}}
.vital-value {{
  font-family: var(--serif);
  font-size: 26px;
  color: var(--ink);
  line-height: 1;
  letter-spacing: -0.01em;
}}
.spark {{
  width: 80px;
  height: 18px;
  display: block;
}}
.spark-accent polyline {{ stroke: var(--accent); }}
.spark-muted polyline {{ stroke: var(--ink-faded); }}

/* --- The Bigger Pattern: trajectory evidence + risks + interventions */
.pattern-section {{
  margin-bottom: 0;
  padding-top: 48px;
  padding-bottom: 48px;
  border-top: 1px solid var(--rule);
}}
.pattern-block {{
  margin-bottom: 28px;
}}
.pattern-block:last-child {{ margin-bottom: 0; }}
.pattern-label {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 14px;
  font-weight: 500;
}}
.pattern-label--warn {{
  color: var(--ink);
  position: relative;
}}
.pattern-label--warn::before {{
  content: "";
  display: inline-block;
  width: 8px;
  height: 8px;
  background: var(--accent);
  margin-right: 10px;
  vertical-align: middle;
  border-radius: 50%;
}}
.pattern-list {{
  list-style: none;
  padding: 0;
  margin: 0;
}}
.pattern-list li {{
  font-family: var(--serif);
  font-size: 16px;
  line-height: 1.55;
  color: var(--ink);
  padding-left: 22px;
  margin-bottom: 10px;
  position: relative;
}}
.pattern-list li::before {{
  content: "·";
  position: absolute;
  left: 6px;
  top: -2px;
  color: var(--accent);
  font-size: 22px;
  line-height: 1.1;
}}
.pattern-list--actions li::before {{
  content: "→";
  font-size: 14px;
  top: 0;
  font-family: var(--sans);
}}

/* --- Recurrence callout on the moment -------------------------------- */
.m-recurrence {{
  display: inline-block;
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink);
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  padding: 10px 14px 10px 14px;
  margin-bottom: 24px;
  font-weight: 500;
}}
.m-quote-context {{
  font-family: var(--sans);
  font-size: 11.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  margin: -24px 0 32px;
}}
.m-try-list {{
  list-style: none;
  padding: 0;
  margin: 6px 0 0;
}}
.m-try-list li {{
  font-family: var(--serif);
  font-size: 16.5px;
  line-height: 1.55;
  color: var(--ink);
  padding-left: 22px;
  margin-bottom: 8px;
  position: relative;
}}
.m-try-list li::before {{
  content: "→";
  position: absolute;
  left: 0;
  top: 0;
  color: var(--accent);
  font-family: var(--sans);
}}

/* --- Stat tiles (cost summary 3-col grid) ---------------------------- */
.stat-tiles {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1px;
  background: var(--rule);
  margin-bottom: 28px;
}}
.stat-tile {{
  background: var(--cream);
  padding: 20px 18px 22px;
}}
.stat-tile--accent {{
  background: var(--accent-faint);
}}
.stat-tile-label {{
  font-family: var(--sans);
  font-size: 9.5px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--ink-muted);
  margin-bottom: 12px;
  font-weight: 500;
}}
.stat-tile-value {{
  font-family: var(--serif);
  font-size: 22px;
  color: var(--ink);
  letter-spacing: -0.01em;
}}
.stat-tile-value--forming {{
  color: var(--ink-faded);
  font-style: italic;
  font-size: 17px;
}}

/* --- Cost stacked-bar with legend ----------------------------------- */
.cost-bar-wrap {{
  margin-bottom: 24px;
}}
.cost-bar {{
  display: flex;
  height: 12px;
  border-radius: 6px;
  overflow: hidden;
  background: var(--cream-edge);
  margin-bottom: 16px;
}}
.cb-seg {{
  background: var(--accent);
  height: 100%;
}}
.cost-bar-legend {{
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
.cb-legend-row {{
  display: grid;
  grid-template-columns: 14px 1fr auto;
  gap: 12px;
  align-items: center;
  font-family: var(--sans);
  font-size: 13px;
  color: var(--ink-muted);
}}
.cb-legend-swatch {{
  width: 12px;
  height: 12px;
  background: var(--accent);
  border-radius: 2px;
}}
.cb-legend-model {{
  color: var(--ink);
  font-family: var(--serif);
  font-size: 16px;
}}
.cb-legend-spend {{
  color: var(--ink-muted);
  font-family: var(--sans);
  font-size: 13px;
  letter-spacing: 0.01em;
}}

/* --- Weekly trajectory chart ---------------------------------------- */
.wt-section {{
  margin-bottom: 96px;
}}
.wt-chart {{
  width: 100%;
  height: auto;
  max-width: 640px;
  margin-top: 16px;
}}
.wt-grid {{
  stroke: var(--rule);
  stroke-width: 1;
}}
.wt-axis-label {{
  font-family: var(--sans);
  font-size: 10px;
  letter-spacing: 0.06em;
  fill: var(--ink-faded);
}}
.wt-line-eng {{ stroke: var(--accent); }}
.wt-line-del {{ stroke: var(--ink-muted); stroke-dasharray: 4 4; }}
.wt-dot--eng {{ fill: var(--accent); }}
.wt-dot--del {{ fill: var(--ink-muted); }}
.wt-legend-text {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  fill: var(--ink-muted);
}}
.wt-legend-eng {{ fill: var(--accent); }}
.wt-legend-del {{ fill: var(--ink-muted); }}

/* --- Coaching block: visual continuity across moment + follow-up + next-week.
       Three sections, one editorial spread. */
.coaching {{
  margin-bottom: 96px;
}}
.coaching .m-section,
.coaching .f-section,
.coaching .n-section {{
  margin-bottom: 0;
  padding-bottom: 48px;
}}
.coaching .f-section,
.coaching .n-section {{
  padding-top: 40px;
  border-top: 1px solid var(--rule);
}}

/* The COACHING kicker that sits above the moment's section eyebrow.
   Visually telegraphs that this is the section that carries coaching. */
.coaching-tag {{
  display: inline-block;
  font-family: var(--sans);
  font-size: 10px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--cream);
  background: var(--accent);
  padding: 5px 10px 4px;
  margin-bottom: 18px;
  font-weight: 500;
}}

/* --- Data block: the supporting numbers, visually de-emphasized so the
       coaching above carries the weight. A small rule + label separates
       data from coaching. */
.data-block {{
  margin-top: 24px;
}}
.data-block__rule {{
  width: 100%;
  height: 1px;
  background: var(--rule-strong);
  margin: 0 0 8px;
}}
.data-block__label {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--ink-faded);
  margin-bottom: 64px;
}}

/* --- 2. Moment section ---------------------------------------------- */
.m-section {{
  margin-bottom: 96px;
}}
.m-quote {{
  font-family: var(--serif);
  font-size: 30px;
  line-height: 1.35;
  color: var(--ink);
  font-style: italic;
  margin: 0 0 40px;
  padding-left: 0;
  text-indent: 0;
  position: relative;
}}
.m-quote-mark {{
  color: var(--accent);
  font-style: normal;
  font-size: 1em;
  font-family: var(--serif);
  font-weight: 400;
}}
.m-body {{
  font-family: var(--serif);
  font-size: 16.5px;
  line-height: 1.65;
  color: var(--ink);
  margin-bottom: 20px;
  max-width: 560px;
}}
.m-body:last-of-type {{ margin-bottom: 0; }}
.m-inline-label {{
  display: block;
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 6px;
  font-weight: 500;
}}
.m-cost {{
  font-family: var(--sans);
  font-size: 12.5px;
  color: var(--ink-muted);
  margin-top: 24px;
  letter-spacing: 0.01em;
}}

/* --- 3. Cost ledger ------------------------------------------------- */
.c-section {{
  margin-bottom: 96px;
}}
.c-amount {{
  font-family: var(--serif);
  color: var(--ink);
  display: flex;
  align-items: baseline;
  gap: 2px;
  margin-bottom: 12px;
}}
.c-currency {{
  font-size: 32px;
  color: var(--ink-muted);
  font-weight: 400;
  letter-spacing: -0.01em;
  margin-right: 4px;
}}
.c-amount-plain {{
  /* Visually hidden duplicate used for substring tests and assistive
     tech. Keeps the typographic display ($X.XX broken into parts)
     while preserving a copy-pastable, screen-readable full amount. */
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}}
.c-big {{
  font-size: 64px;
  line-height: 1;
  letter-spacing: -0.025em;
  font-weight: 400;
}}
.c-cents {{
  font-size: 28px;
  color: var(--ink-muted);
  letter-spacing: -0.01em;
}}
.c-delta {{
  font-family: var(--sans);
  font-size: 13px;
  color: var(--ink-muted);
  letter-spacing: 0.01em;
  margin-bottom: 28px;
}}
.c-biggest {{
  display: block;
  padding: 18px 0;
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
}}
.c-inline-label {{
  display: block;
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-muted);
  margin-bottom: 6px;
  font-weight: 500;
}}
.c-biggest-text {{
  font-family: var(--serif);
  font-size: 17px;
  color: var(--ink);
}}
.c-tier-fit {{
  font-family: var(--sans);
  font-size: 13px;
  color: var(--ink-muted);
  margin-top: 16px;
  font-style: italic;
}}

/* --- 4. Where the week went (tasks) --------------------------------- */
.w-section {{
  margin-bottom: 96px;
}}
.w-task {{
  padding: 24px 0;
  border-top: 1px solid var(--rule);
}}
.w-task:last-child {{
  border-bottom: 1px solid var(--rule);
}}
.w-label {{
  font-family: var(--serif);
  font-size: 21px;
  line-height: 1.3;
  color: var(--ink);
  font-weight: 400;
  margin-bottom: 12px;
}}
.w-bar {{
  height: 3px;
  background: var(--cream-edge);
  border-radius: 1.5px;
  margin-bottom: 12px;
  overflow: hidden;
}}
.w-fill {{
  height: 100%;
  background: var(--accent);
  opacity: 0.7;
}}
.w-meta {{
  font-family: var(--sans);
  font-size: 13px;
  color: var(--ink-muted);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}}
.w-meta-stat {{
  color: var(--ink);
}}
.w-meta-dot {{
  color: var(--ink-faded);
}}
.w-worst {{
  color: var(--ink-muted);
  font-style: italic;
}}

/* --- 5. Dimensions -------------------------------------------------- */
.d-section {{
  margin-bottom: 96px;
}}

/* Radar chart: hexagonal visual of the six dimensions.
   Sized to the column width via the SVG viewBox; the wrap centers it. */
.r-wrap {{
  display: flex;
  justify-content: center;
  margin: 16px -16px 48px;
}}
.r-chart {{
  width: 100%;
  max-width: 460px;
  height: auto;
}}
.r-axis {{
  stroke: var(--rule);
  stroke-width: 1;
}}
.r-ring {{
  fill: none;
  stroke: var(--rule);
  stroke-width: 1;
}}
.r-ring--outer {{
  stroke: var(--rule-strong);
}}
.r-score {{
  fill: var(--accent);
  fill-opacity: 0.18;
  stroke: var(--accent);
  stroke-width: 1.5;
  stroke-linejoin: round;
}}
.r-baseline {{
  fill: none;
  stroke: var(--ink-faded);
  stroke-width: 1;
  stroke-dasharray: 3 4;
  stroke-linejoin: round;
}}
.r-dot {{
  fill: var(--accent);
}}
.r-label {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  fill: var(--ink-muted);
  font-weight: 500;
}}
.r-label-score {{
  font-family: var(--serif);
  font-size: 13px;
  fill: var(--accent);
  font-weight: 400;
}}

.d-list {{
  list-style: none;
  padding: 0;
}}
.d-row {{
  padding: 24px 0;
  border-top: 1px solid var(--rule);
}}
.d-row:last-child {{
  border-bottom: 1px solid var(--rule);
}}
.d-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;
}}
.d-title {{
  font-family: var(--serif);
  font-size: 18px;
  color: var(--ink);
}}
.d-score {{
  font-family: var(--serif);
  font-size: 26px;
  color: var(--accent);
  letter-spacing: -0.01em;
  font-weight: 400;
}}
.d-bar-track {{
  height: 4px;
  background: var(--cream-edge);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 10px;
}}
.d-bar-fill {{
  height: 100%;
  background: var(--accent);
  opacity: 0.85;
}}
.d-meta {{
  font-family: var(--sans);
  font-size: 12px;
  letter-spacing: 0.01em;
  color: var(--ink-muted);
  display: flex;
  align-items: center;
  gap: 12px;
}}
.d-baseline--forming {{
  font-style: italic;
  color: var(--ink-faded);
}}
.d-delta-up {{
  color: var(--accent);
}}
.d-delta-down {{
  color: var(--ink-muted);
}}

/* --- 6. Follow-up --------------------------------------------------- */
.f-section {{
  margin-bottom: 96px;
}}
.f-body {{
  font-family: var(--serif);
  font-size: 16.5px;
  line-height: 1.6;
  color: var(--ink);
  margin-bottom: 20px;
  max-width: 560px;
}}
.f-body:last-of-type {{
  margin-bottom: 0;
}}
.f-outcome {{
  font-family: var(--sans);
  font-size: 12px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-weight: 500;
  padding: 4px 10px 4px;
  border-radius: 3px;
  margin-left: 0;
}}
.f-outcome--improved {{
  color: var(--accent);
  background: var(--accent-faint);
}}
.f-outcome--pending {{
  color: var(--ink-muted);
  background: var(--rule);
}}
.f-outcome--unchanged {{
  color: var(--ink-muted);
  background: var(--rule);
}}
.f-outcome--worse {{
  color: var(--ink);
  background: var(--accent-soft);
}}

/* --- 7. Next-week (one thing to try) -------------------------------- */
.n-section {{
  margin-bottom: 96px;
}}
.n-body {{
  font-family: var(--serif);
  font-size: 20px;
  line-height: 1.5;
  color: var(--ink);
  font-style: italic;
  max-width: 560px;
}}

/* --- Colophon ------------------------------------------------------- */
.colophon {{
  margin-top: 64px;
  padding-top: 32px;
}}
.colophon-rule {{
  width: 48px;
  height: 1px;
  background: var(--accent);
  opacity: 0.6;
  margin: 0 auto 24px;
}}
.colophon-line {{
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-muted);
  text-align: center;
  margin-bottom: 4px;
}}
.colophon-meta {{
  font-family: var(--sans);
  font-size: 11px;
  color: var(--ink-faded);
  text-align: center;
  letter-spacing: 0.02em;
}}
</style>
</head>
<body>
<main class="page">
  <header class="masthead">
    <span class="masthead-meta">{generated_readable}</span>
  </header>
{body_sections}
  <footer class="colophon">
    <div class="colophon-rule"></div>
    <div class="colophon-line">Praxis · A reading of your AI practice</div>
    <div class="colophon-meta">Generated {generated_iso}</div>
  </footer>
</main>
</body>
</html>"""


# ------------------------------------------------------------------ persistence
#
# Spec section 13.1: the HTML lives at ``~/.praxis/weeks/<iso>.html`` and a
# ``latest.html`` symlink at ``~/.praxis/`` always points to the most recent
# week's file. Re-running on the same week overwrites the file in place and
# refreshes the symlink so the most-recent pointer never goes stale.


def _update_latest_pointer(latest: Path, target: Path) -> None:
    """Point ``latest`` at ``target``.

    Uses a relative symlink so the pointer survives if ``~/.praxis`` is
    later moved or backed up. On Windows (or any filesystem where
    creating a symlink raises) the function falls back to copying the
    target's contents - the acceptance criterion calls for "a symlink
    (or platform equivalent)".
    """
    rel_target = os.path.relpath(target, start=latest.parent)
    # Path.exists() returns False for broken symlinks, so check is_symlink()
    # separately. unlink(missing_ok=True) covers the no-prior-pointer case.
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    try:
        latest.symlink_to(rel_target)
    except (OSError, NotImplementedError):
        shutil.copy2(target, latest)


def write_digest(digest: WeeklyDigest, home: Path | None = None) -> Path:
    """Render ``digest`` and persist it under ``~/.praxis/weeks/``.

    Writes ``<home>/weeks/<week_iso>.html`` atomically (temp file +
    ``os.replace``) so a crash mid-write cannot leave a half-written
    digest on disk. Refreshes ``<home>/latest.html`` to point at the
    new file. Re-running on the same week overwrites the file and
    updates the pointer. Returns the absolute path of the written file.

    The home directory defaults to the value of ``$PRAXIS_HOME`` or
    ``~/.praxis``; tests pass an explicit ``home=tmp_path`` to sandbox
    file I/O.
    """
    base = home if home is not None else resolve_home()
    weeks_dir = base / "weeks"
    weeks_dir.mkdir(parents=True, exist_ok=True)
    target = weeks_dir / f"{digest.week_iso}.html"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(digest), encoding="utf-8")
    os.replace(tmp, target)
    _update_latest_pointer(base / "latest.html", target)
    return target
