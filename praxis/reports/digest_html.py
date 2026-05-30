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
from typing import TYPE_CHECKING

from praxis.redactor import redact_secrets
from praxis.reports.panel_inputs import (
    VERIFICATION_CALIBRATION_KINDS_IN_PANEL_ORDER,
    VERIFICATION_CALIBRATION_LABELS,
    AugAutoBalancePanel,
    BehavioralPatternsPanel,
    CadencePanel,
    ContextEngineeringDepthPanel,
    KnowledgeGapDistributionPanel,
    PanelInputs,
    RefinedCostEffectivenessPanel,
    RepeatTaskRadarPanel,
    SpecificationAdoptionPanel,
    ToolAgentLadderPanel,
    VerificationCalibrationPanel,
)
from praxis.storage.profile_store import resolve_home

if TYPE_CHECKING:
    from praxis.reports.commitment_rollup import CommitmentRollup


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
class BehavioralRow:
    """One behavioral-signal card in the dimensions grid.

    Distinct from `DimRow`: behavioral signals are 0-1 rates (% of user
    turns showing the signal), not 0-10 rubric scores. They render in the
    same grid below the rubric cards but with a % glyph and a ring filled
    against 1.0 instead of 10.

    `rate` and `baseline_rate` are 0.0..1.0. `delta` is in rate-points
    (rate - baseline_rate); negative for falling, positive for rising.
    `frame` is "supportive" (terracotta accent for a rising signal that's
    good news, like Engagement or Verification) or "counter" (ink-blue
    accent for a signal whose rise is a warning, like Delegation). Used
    only for arc colour; baseline tick logic is identical for both.
    """

    title: str = ""
    rate: float = 0.0
    baseline_rate: float | None = None
    delta: float | None = None
    evidence_citation: str = ""
    frame: str = "supportive"   # "supportive" | "counter"


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
    """One bucket on the multi-week trajectory line chart.

    The four signals are the spec's behavioral triad plus the
    "delegation" atrophy axis:
      - engagement_rate: share of user turns with engagement signals
        (why-questions, comprehension checks, explanation requests)
      - delegation_rate: share with atrophy signals (pure delegation,
        outsourced debug, telegraphic prompts) — atrophy axis
      - independence_rate: share showing own attempt before asking
      - verification_marker_rate: share of user turns containing a
        verification marker word (source, verify, cite, check, etc.)
    """

    week_iso: str
    engagement_rate: float
    delegation_rate: float
    independence_rate: float = 0.0
    verification_marker_rate: float = 0.0


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
    behavioral_signals: tuple[BehavioralRow, ...] = ()
    # Spec section 2 (coaching-reposition): masthead's commitment block
    # reads this. None means no follow-up exists for the week so the
    # masthead omits the block rather than rendering placeholder copy.
    commitment_rollup: "CommitmentRollup | None" = None
    # v0.3 expansion panels (US-038..042). Optional; None preserves the
    # pre-expansion document shape so older fixtures still render.
    panel_inputs: PanelInputs | None = None


# ---------------------------------------------------------------- section text
#
# These IDs and titles are the test-anchorable contract for spec section
# 6.1's section order. The list is walked top-down by `render`; reordering
# requires changing the spec and the tests.

_PLACEHOLDER = "Not yet - this section will fill in as the week's data lands."
_DOT = '<span class="w-meta-dot">·</span>'

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


# ----------------------------------------------------- commitment masthead
#
# Spec section 2 (coaching-reposition): the masthead's commitment block
# mirrors the terminal renderer's "Your focus this week" / "How it went"
# pair. We reuse the pure helpers from ``digest_terminal`` so the field
# strings (sessions tally, self-report bucket order, data-says
# annotation, and the agree/disagree gap line) match across both surfaces
# without duplicating the noise-band threshold or the rubric lookup.

from praxis.reports.digest_terminal import (  # noqa: E402
    _GAP_AGREE_LINE as _MASTHEAD_GAP_AGREE,
    _NO_SESSIONS_LOGGED as _MASTHEAD_NO_SESSIONS,
    _format_data_says_line as _masthead_data_says,
    _format_self_report_tally as _masthead_self_report,
    _format_sessions_line as _masthead_sessions,
    _gap_summary_line as _masthead_gap_summary,
)


def _commitment_section(rollup: "CommitmentRollup | None") -> str:
    """Render the masthead's commitment block (spec section 2).

    Omitted entirely when ``rollup`` is None (no active commitment for
    the rendered week). When sessions=0 the block collapses to a single
    "No sessions logged this week." line so the masthead doesn't render
    confusing zero-comparison numbers.

    Field labels and ordering mirror the terminal renderer's commitment
    block (Sessions / You said / Data says / Gap) per US-036 AC #1.
    """
    if rollup is None:
        return ""
    focus_block = (
        '<div class="cb-focus">'
        '<div class="cb-eyebrow">Your focus this week</div>'
        f'<blockquote class="cb-quote">{_safe(rollup.display_text)}</blockquote>'
        '</div>'
    )
    if rollup.sessions_this_week <= 0:
        status_block = (
            '<div class="cb-status">'
            '<div class="cb-eyebrow">How it went</div>'
            f'<p class="cb-no-sessions">{_safe(_MASTHEAD_NO_SESSIONS)}</p>'
            '</div>'
        )
    else:
        sessions_value = _masthead_sessions(
            rollup.sessions_this_week, rollup.sessions_prior_week
        )
        you_said_value = _masthead_self_report(rollup.self_report_tally)
        target_key = rollup.target_dim_key
        dim_after = float(rollup.dim_after.get(target_key, 0.0))
        dim_before_raw = rollup.dim_before.get(target_key)
        dim_before = (
            float(dim_before_raw) if dim_before_raw is not None else None
        )
        data_says_value = _masthead_data_says(target_key, dim_before, dim_after)
        gap_value = _masthead_gap_summary(
            rollup.self_report_tally,
            dim_before,
            dim_after,
            gap_prose=rollup.gap_prose,
        )
        # The four-field status block: ordering matches the terminal
        # renderer's _commitment_block exactly (Sessions, You said, Data
        # says, Gap). The gap value's class flips on agree/disagree so
        # the disagree line picks up the same warning accent the rest of
        # the digest reserves for noticed-and-named gaps. Any text other
        # than the agree line (the static disagree fallback OR judge
        # prose under US-037) counts as a disagree surface and gets the
        # accent treatment.
        gap_modifier = (
            "cb-gap--agree"
            if gap_value == _MASTHEAD_GAP_AGREE
            else "cb-gap--disagree"
        )
        status_block = (
            '<div class="cb-status">'
            '<div class="cb-eyebrow">How it went</div>'
            '<dl class="cb-fields">'
            '<dt>Sessions</dt>'
            f'<dd>{_safe(sessions_value)}</dd>'
            '<dt>You said</dt>'
            f'<dd>{_safe(you_said_value)}</dd>'
            '<dt>Data says</dt>'
            f'<dd>{_safe(data_says_value)}</dd>'
            '<dt>Gap</dt>'
            f'<dd class="{gap_modifier}">{_safe(gap_value)}</dd>'
            '</dl>'
            '</div>'
        )
    return (
        '\n  <section class="cb-section" id="commitment-block">'
        f'{focus_block}'
        f'{status_block}'
        '</section>'
    )


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

    Renders as the feature article of the digest: session context, a
    why paragraph, a what-to-try paragraph, and a recurrence callout
    when the same suggested_alternative has been flagged in prior weeks.
    Transcript quotes are intentionally not shown. The report is coaching,
    not a transcript viewer.
    """
    coaching_tag = '<div class="coaching-tag">Coaching</div>'
    if moment is None or not (moment.why_lost_score or moment.next_time_try):
        return f"""
  <section class="m-section" id="this-weeks-moment">
    {coaching_tag}
    <div class="s-eyebrow">This Week's Moment</div>
    <p class="placeholder">No coachable moment surfaced this week. Run more sessions to surface one.</p>
  </section>"""
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
    context_bits: list[str] = []
    if effective_dim:
        context_bits.append(_safe(effective_dim))
    if moment.session_started_at:
        context_bits.append(_safe(moment.session_started_at))
    if context_bits:
        blocks.append(
            f'<p class="m-quote-context">Observed: {" · ".join(context_bits)}</p>'
        )
    if why:
        blocks.append(
            f'<p class="m-body"><span class="m-inline-label">Coach\'s read</span>{why}</p>'
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
                f'<div class="m-body"><span class="m-inline-label">Practice next week</span>'
                f'<ul class="m-try-list">{items}</ul></div>'
            )
        else:
            blocks.append(
                f'<p class="m-body"><span class="m-inline-label">Practice next week</span>{nxt}</p>'
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
    """Cost rendered as narrative + bar, not as a dashboard tile grid.

    The previous version used a 3-column stat-tile row ("This week /
    Baseline / vs baseline"), which is the SaaS-dashboard hero-metric
    template the impeccable rules call out. This version puts the big
    number inline with a serif sentence and lets the stacked bar do the
    breakdown.
    """
    if ledger is None or ledger.this_week_dollars <= 0.0:
        return """
  <section class="c-section" id="cost-ledger">
    <div class="s-eyebrow">Cost Ledger</div>
    <p class="placeholder">No priced model spend this week.</p>
  </section>"""

    full_amount = f"${ledger.this_week_dollars:,.2f}"
    cents_split = f"{ledger.this_week_dollars:.2f}".split(".")
    big = cents_split[0]
    cents = cents_split[1] if len(cents_split) > 1 else "00"

    # Inline narrative that explains the week's spend in one sentence.
    # No tile chrome, no big-number/small-label template; the prose
    # carries the comparison.
    if ledger.baseline_dollars and ledger.baseline_dollars > 0:
        diff = ledger.this_week_dollars - ledger.baseline_dollars
        if abs(diff) < 0.005:
            comparison = (
                f"on the 90-day baseline of ${ledger.baseline_dollars:,.2f}."
            )
        elif diff > 0:
            comparison = (
                f"about ${diff:,.2f} above your 90-day "
                f"baseline of ${ledger.baseline_dollars:,.2f}."
            )
        else:
            comparison = (
                f"about ${abs(diff):,.2f} below your 90-day "
                f"baseline of ${ledger.baseline_dollars:,.2f}."
            )
    else:
        comparison = (
            "the first weekly digest on file; the 90-day baseline starts "
            "forming next week."
        )

    biggest_clause = ""
    if ledger.biggest_line:
        biggest_clause = (
            f' <span class="c-narrative-em">Most of it</span> went to '
            f'{_safe(ledger.biggest_line)}.'
        )

    tier_clause = ""
    if ledger.sonnet_swap_note:
        tier_clause = f' {_safe(ledger.sonnet_swap_note)}'

    split_bar = _cost_split_bar(ledger.model_split, ledger.this_week_dollars)

    return f"""
  <section class="c-section" id="cost-ledger">
    <div class="s-eyebrow">Cost Ledger</div>
    <div class="c-amount" aria-label="{full_amount}">
      <span class="c-currency">$</span><span class="c-big">{big}</span><span class="c-cents">.{cents}</span>
      <span class="c-amount-plain">{full_amount}</span>
    </div>
    <p class="c-narrative">This week's priced spend was {comparison}{biggest_clause}{tier_clause}</p>
    {split_bar}
  </section>"""


def _task_breakdown_section(rows: tuple[TaskRow, ...]) -> str:
    """Tasks as a numbered editorial list, not a card grid.

    Film-festival-lineup typography: large display numeral at left,
    task label in serif at the right, meta line below. The numeral
    is what carries the visual weight; a thin terracotta hairline
    separates rows. No card chrome.
    """
    if not rows:
        return """
  <section class="w-section" id="where-the-week-went">
    <div class="s-eyebrow">Where The Week Went</div>
    <p class="placeholder">No tasks identified yet.</p>
  </section>"""
    items: list[str] = []
    for idx, row in enumerate(rows, start=1):
        worst_html = ""
        if row.worst_score is not None:
            worst_html = (
                f'<span class="w-worst">Lowest dim score this task: {row.worst_score:.1f}</span>'
            )
        cost_str = (
            f"${row.dollars:.2f}" if row.dollars > 0 else "—"
        )
        items.append(
            f'<article class="w-task">'
            f'<div class="w-num">{idx:02d}</div>'
            f'<div class="w-body">'
            f'<h3 class="w-label">{_safe(row.label)}</h3>'
            f'<div class="w-meta">'
            f'<span class="w-meta-stat">{row.session_count} session{"s" if row.session_count != 1 else ""}</span>'
            f'<span class="w-meta-dot">·</span>'
            f'<span class="w-meta-stat">{cost_str}</span>'
            f'{_DOT + worst_html if worst_html else ""}'
            f'</div>'
            f'</div>'  # close w-body
            f"</article>"
        )
    return f"""
  <section class="w-section" id="where-the-week-went">
    <div class="s-eyebrow">Where The Week Went</div>
    {"".join(items)}
  </section>"""




def _dim_ring_svg(score: float, baseline: float | None) -> str:
    """The arc-indicator ring used inside each dim card.

    A circular score gauge (FIFA-card aesthetic) drawn as an SVG arc.
    The full circle represents a 10/10 ceiling; the filled arc spans
    (score/10) of the circumference. When a 90-day baseline is on
    file, a small tick mark sits ON the ring at the baseline's
    position so the reader can see "you are above/below baseline"
    without a separate chart.
    """
    import math

    score = max(0.0, min(10.0, score))
    size, stroke = 56, 4
    cx = cy = size / 2
    r = (size - stroke) / 2

    # Path math: angles in radians, starting from the top (-pi/2) and
    # sweeping clockwise. Arc length = score/10 of the full circumference.
    start_angle = -math.pi / 2
    sweep_angle = (score / 10.0) * 2 * math.pi
    end_angle = start_angle + sweep_angle

    # SVG arc command needs the large-arc flag if sweep > pi.
    large_arc = 1 if sweep_angle > math.pi else 0

    def polar(theta: float) -> tuple[float, float]:
        return (cx + r * math.cos(theta), cy + r * math.sin(theta))

    x0, y0 = polar(start_angle)
    x1, y1 = polar(end_angle)

    # Edge case: full circle (score = 10) — SVG arc can't draw a full
    # 360° arc, so split into two half-arcs.
    if score >= 9.99:
        fill_path = (
            f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" '
            f'class="dim-card__ring-fill" stroke-width="{stroke}"/>'
        )
    elif score <= 0.01:
        fill_path = ""
    else:
        fill_path = (
            f'<path d="M {x0:.2f} {y0:.2f} '
            f'A {r:.2f} {r:.2f} 0 {large_arc} 1 {x1:.2f} {y1:.2f}" '
            f'class="dim-card__ring-fill" stroke-width="{stroke}"/>'
        )

    # Baseline tick: a short radial tick at the baseline position.
    baseline_tick = ""
    if baseline is not None:
        bl = max(0.0, min(10.0, baseline))
        bl_angle = -math.pi / 2 + (bl / 10.0) * 2 * math.pi
        # Tick from r-2 to r+3 along the radial direction.
        t_in_x = cx + (r - 3) * math.cos(bl_angle)
        t_in_y = cy + (r - 3) * math.sin(bl_angle)
        t_out_x = cx + (r + 4) * math.cos(bl_angle)
        t_out_y = cy + (r + 4) * math.sin(bl_angle)
        baseline_tick = (
            f'<line x1="{t_in_x:.2f}" y1="{t_in_y:.2f}" '
            f'x2="{t_out_x:.2f}" y2="{t_out_y:.2f}" '
            f'class="dim-card__ring-base-tick"/>'
        )

    return (
        f'<svg class="dim-card__ring-svg" viewBox="0 0 {size} {size}" '
        f'role="img" aria-label="score gauge">'
        f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" '
        f'class="dim-card__ring-track" stroke-width="{stroke}"/>'
        f'{fill_path}'
        f'{baseline_tick}'
        f'</svg>'
    )


# A short evidence citation per dim. Drawn from the rubric's evidence
# strings; truncated to a single line so the cards keep a uniform
# baseline height.
_DIM_EVIDENCE: dict[str, str] = {
    "Planning before prompting":
        "Sarkar 2025 — experienced agent users plan first; +6% accept rate per SD of experience.",
    "Context richness":
        "OpenRouter 2025 — average prompt length grew 4× to ~6K tokens.",
    "Iteration & evaluation":
        "Sarkar 2025 — abstraction, clarity, evaluation are the core skills of effective users.",
    "Tool & multi-step use":
        "OpenRouter 2025 — agentic patterns are rising; tool-capable sequences tripled in length.",
    "Model–task fit":
        "OpenRouter — model–task fit (\"Glass Slipper\" retention) predicts long-term usage.",
    "Verification habits":
        "Anthropic safety guidance — verification is the dividing line between practitioners and casual users.",
}


# Evidence citations for the behavioral signal cards. These sit beneath
# the rubric cards in the same grid, separated by a thin divider. The
# rubric reads dimensions on a 0-10 LLM-judged scale; behavioral
# signals are 0-100% rates from the regex extractor and the marker
# pipeline, so they need their own evidence anchoring.
_BEHAVIORAL_EVIDENCE: dict[str, str] = {
    "Engagement":
        "Shen & Tamkin 2026 — high-engagement users (why-questions, "
        "comprehension checks) scored 17pp higher on comprehension.",
    "Delegation":
        "Shen & Tamkin 2026 — ~20% of users were \"pure delegators\": "
        "fastest, worst learning outcomes.",
    "Independence":
        "Anthropic 2026 — own-attempt-before-asking correlates with "
        "long-term skill retention.",
    "Verification":
        "Anthropic safety guidance — verification is the dividing line "
        "between practitioners and casual users.",
}


def _behavioral_ring_svg(rate: float, baseline_rate: float | None) -> str:
    """A ring identical in geometry to the dim ring but scaled to 0-1.

    Reuses the same `dim-card__ring-*` CSS classes so the cards look
    visually consistent in the grid. The caller decides whether to add
    a `dim-card--counter` modifier on the wrapping article to swap the
    fill colour (terracotta vs ink-blue).
    """
    import math

    rate = max(0.0, min(1.0, rate))
    size, stroke = 56, 4
    cx = cy = size / 2
    r = (size - stroke) / 2

    start_angle = -math.pi / 2
    sweep_angle = rate * 2 * math.pi
    end_angle = start_angle + sweep_angle
    large_arc = 1 if sweep_angle > math.pi else 0

    def polar(theta: float) -> tuple[float, float]:
        return (cx + r * math.cos(theta), cy + r * math.sin(theta))

    x0, y0 = polar(start_angle)
    x1, y1 = polar(end_angle)

    if rate >= 0.999:
        fill_path = (
            f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" '
            f'class="dim-card__ring-fill" stroke-width="{stroke}"/>'
        )
    elif rate <= 0.001:
        fill_path = ""
    else:
        fill_path = (
            f'<path d="M {x0:.2f} {y0:.2f} '
            f'A {r:.2f} {r:.2f} 0 {large_arc} 1 {x1:.2f} {y1:.2f}" '
            f'class="dim-card__ring-fill" stroke-width="{stroke}"/>'
        )

    baseline_tick = ""
    if baseline_rate is not None:
        bl = max(0.0, min(1.0, baseline_rate))
        bl_angle = -math.pi / 2 + bl * 2 * math.pi
        t_in_x = cx + (r - 3) * math.cos(bl_angle)
        t_in_y = cy + (r - 3) * math.sin(bl_angle)
        t_out_x = cx + (r + 4) * math.cos(bl_angle)
        t_out_y = cy + (r + 4) * math.sin(bl_angle)
        baseline_tick = (
            f'<line x1="{t_in_x:.2f}" y1="{t_in_y:.2f}" '
            f'x2="{t_out_x:.2f}" y2="{t_out_y:.2f}" '
            f'class="dim-card__ring-base-tick"/>'
        )

    return (
        f'<svg class="dim-card__ring-svg" viewBox="0 0 {size} {size}" '
        f'role="img" aria-label="rate gauge">'
        f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" '
        f'class="dim-card__ring-track" stroke-width="{stroke}"/>'
        f'{fill_path}'
        f'{baseline_tick}'
        f'</svg>'
    )


def _behavioral_card(row: BehavioralRow) -> str:
    """Render one behavioral-signal card.

    Same grid cell as a dim card but uses % glyph instead of /10 and
    shows the rate-point delta (e.g. \"+3pp vs baseline 42%\").
    """
    ring = _behavioral_ring_svg(row.rate, row.baseline_rate)
    pct = int(round(row.rate * 100))

    # Delta line. Behavioral rates move in smaller steps than rubric
    # scores, so the noise band is in rate points; 3pp = visible change.
    delta_line = ""
    if row.baseline_rate is not None and row.delta is not None:
        delta_pp = row.delta * 100
        if abs(delta_pp) >= 3:
            sign = "+" if delta_pp > 0 else "−"
            cls = (
                "dim-card__delta--up" if delta_pp > 0 else "dim-card__delta--down"
            )
            delta_line = (
                f'<div class="dim-card__delta {cls}">'
                f'{sign}{abs(delta_pp):.0f}pp vs baseline {int(round(row.baseline_rate*100))}%'
                f'</div>'
            )
        else:
            delta_line = (
                f'<div class="dim-card__delta">'
                f'on baseline ({int(round(row.baseline_rate*100))}%)'
                f'</div>'
            )
    elif row.baseline_rate is None:
        delta_line = '<div class="dim-card__delta">baseline forming</div>'

    evidence = row.evidence_citation or _BEHAVIORAL_EVIDENCE.get(row.title, "")
    evidence_html = (
        f'<div class="dim-card__evidence">{_safe(evidence)}</div>'
        if evidence else ""
    )
    modifier = (
        " dim-card--counter" if row.frame == "counter" else ""
    )
    return (
        f'<article class="dim-card{modifier}">'
        f'<div class="dim-card__head">'
        f'<div class="dim-card__title">{_safe(row.title)}</div>'
        f'<div class="dim-card__ring">'
        f'{ring}'
        f'<div class="dim-card__ring-score">{pct}<span class="dim-card__ring-unit">%</span></div>'
        f'</div>'
        f'</div>'
        f'{delta_line}'
        f'{evidence_html}'
        f'</article>'
    )


def _dimensions_section(
    rows: tuple[DimRow, ...],
    behavioral: tuple[BehavioralRow, ...] = (),
) -> str:
    if not rows and not behavioral:
        return """
  <section class="d-section" id="the-six-dimensions">
    <div class="s-eyebrow">The Six Dimensions</div>
    <p class="placeholder">No dimension data yet.</p>
  </section>"""

    cards: list[str] = []
    for row in rows:
        ring = _dim_ring_svg(row.score, row.baseline)
        # Delta line: render only when the baseline is on file AND the
        # movement clears the noise band (0.3 per spec §8.3). Below the
        # band we say "on baseline" rather than show a misleading ±0.x.
        delta_line = ""
        if row.baseline is not None and row.delta is not None:
            if abs(row.delta) >= 0.3:
                sign = "+" if row.delta > 0 else "−"
                cls = "dim-card__delta--up" if row.delta > 0 else "dim-card__delta--down"
                delta_line = (
                    f'<div class="dim-card__delta {cls}">'
                    f'{sign}{abs(row.delta):.1f} vs baseline {row.baseline:.1f}'
                    f'</div>'
                )
            else:
                delta_line = (
                    f'<div class="dim-card__delta">'
                    f'on baseline ({row.baseline:.1f})'
                    f'</div>'
                )
        elif row.baseline is None:
            delta_line = (
                '<div class="dim-card__delta">baseline forming</div>'
            )

        evidence = row.evidence_citation or _DIM_EVIDENCE.get(row.title, "")
        evidence_html = (
            f'<div class="dim-card__evidence">{_safe(evidence)}</div>'
            if evidence else ""
        )
        cards.append(
            f'<article class="dim-card">'
            f'<div class="dim-card__head">'
            f'<div class="dim-card__title">{_safe(row.title)}</div>'
            f'<div class="dim-card__ring">'
            f'{ring}'
            f'<div class="dim-card__ring-score">{row.score:.1f}</div>'
            f'</div>'
            f'</div>'
            f'{delta_line}'
            f'{evidence_html}'
            f'</article>'
        )

    behavioral_block = ""
    if behavioral:
        beh_cards = "".join(_behavioral_card(b) for b in behavioral)
        # Thin rule + eyebrow groups behavioral rates beneath the rubric
        # cards in the same dimensions section, so the reader sees them
        # as part of the same answer to "how am I doing?" but understands
        # the different unit (% rate vs /10 score).
        behavioral_block = (
            '<div class="d-divider" aria-hidden="true"></div>'
            '<div class="d-subhead">'
            '<div class="s-eyebrow s-eyebrow--inline">Behavioral signals</div>'
            '<div class="d-subhead-note">From regex extractors over user '
            'turns. Rates (% of turns showing the signal), not /10 scores.</div>'
            '</div>'
            f'<div class="dims-grid">{beh_cards}</div>'
        )

    return f"""
  <section class="d-section" id="the-six-dimensions">
    <div class="s-eyebrow">The Six Dimensions</div>
    <div class="dims-grid">{"".join(cards)}</div>
    {behavioral_block}
  </section>"""


def _follow_up_section(follow_up: FollowUpPanel | None) -> str:
    if follow_up is None or not follow_up.commitment_text:
        return """
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


def _mini_line_chart(
    points: tuple[WeeklyTrajectoryPoint, ...],
    signal: str,
    *,
    accent: str,
    title: str,
    rule_of_thumb: str,
) -> str:
    """One mini line chart (small-multiples). Compact: ~200×96 viewBox."""
    if not points:
        return ""
    width, height = 220, 110
    pad_left, pad_right, pad_top, pad_bottom = 8, 8, 22, 22
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom
    n = len(points)

    values = [getattr(p, signal) for p in points]
    if not values:
        return ""
    # Always anchor y to [0,1] so the four small multiples share a scale.
    vmin, vmax = 0.0, 1.0

    def to_xy(i: int, v: float) -> tuple[float, float]:
        x = pad_left + (i / max(1, n - 1)) * inner_w
        y = pad_top + (1.0 - max(vmin, min(vmax, v))) * inner_h
        return x, y

    xy = [to_xy(i, v) for i, v in enumerate(values)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    end_x, end_y = xy[-1]
    current = values[-1]

    # First and last labels along the x-axis.
    first_w = points[0].week_iso.split("-W")[-1] if "-W" in points[0].week_iso else points[0].week_iso
    last_w = points[-1].week_iso.split("-W")[-1] if "-W" in points[-1].week_iso else points[-1].week_iso

    # Light gridline at y=0.5 only.
    mid_y = pad_top + 0.5 * inner_h
    gridline = (
        f'<line x1="{pad_left}" y1="{mid_y:.1f}" '
        f'x2="{width - pad_right}" y2="{mid_y:.1f}" class="mc-midline"/>'
    )

    line_class = f"mc-line mc-line--{accent}"
    dot_class = f"mc-dot mc-dot--{accent}"

    return (
        f'<figure class="mc">'
        f'<figcaption class="mc-title">{title}'
        f'<span class="mc-current">{current:.2f}</span></figcaption>'
        f'<svg class="mc-chart" viewBox="0 0 {width} {height}" role="img" aria-label="{title} over time">'
        f'{gridline}'
        f'<polyline points="{path}" fill="none" class="{line_class}"/>'
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="3.5" class="{dot_class}"/>'
        f'<text x="{pad_left}" y="{height - 6}" class="mc-axis">W{first_w}</text>'
        f'<text x="{width - pad_right}" y="{height - 6}" text-anchor="end" class="mc-axis">W{last_w}</text>'
        f'</svg>'
        f'<div class="mc-note">{rule_of_thumb}</div>'
        f'</figure>'
    )


def _weekly_trajectory_section(
    points: tuple[WeeklyTrajectoryPoint, ...]
) -> str:
    """Multi-week trajectory rendered as Tufte small multiples.

    Four mini line charts side-by-side, one per behavioral signal,
    sharing a [0,1] y-axis. The reader scans across to see which
    signals are moving. The single-chart "2 overlaid lines" approach
    that preceded this is gone because it hid 2 signals (independence,
    verification) the pipeline already computes.

    Sub-4-weeks falls back to a placeholder card so the section still
    occupies its spec-section-6.1 slot.
    """
    if not points:
        return """
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
    <p class="placeholder">Building. {weeks_have} week{"s" if weeks_have != 1 else ""} of buckets on file; {weeks_need} more before the chart fits.</p>
  </section>"""

    # Four small multiples. Engagement and Independence are accented in
    # terracotta (the "good" signals that earn skill); Delegation and
    # Verification are accented in the ink-blue counterweight (one is
    # the atrophy axis, the other is a quality control habit that
    # complements them all).
    charts = (
        _mini_line_chart(
            points, "engagement_rate",
            accent="accent",
            title="Engagement",
            rule_of_thumb="why-questions, comprehension checks, follow-ups",
        ),
        _mini_line_chart(
            points, "independence_rate",
            accent="accent",
            title="Independence",
            rule_of_thumb="share showing own attempt before asking",
        ),
        _mini_line_chart(
            points, "verification_marker_rate",
            accent="counter",
            title="Verification",
            rule_of_thumb="asking for sources, traces, proof",
        ),
        _mini_line_chart(
            points, "delegation_rate",
            accent="counter",
            title="Delegation",
            rule_of_thumb="pure-delegation, outsourced debug, telegraphic",
        ),
    )
    n = len(points)
    return f"""
  <section class="wt-section" id="weekly-trajectory">
    <div class="s-eyebrow">Your Learning Trajectory · {n} weeks</div>
    <div class="mc-grid">{"".join(charts)}</div>
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


_BEHAVIORAL_PATTERNS_EMPTY = "No behavioral patterns captured this week."

# US-039 empty-state copy. Tests assert the exact strings so a future
# rewording is one audit point per renderer.
_AUG_AUTO_BALANCE_CLASSIFIER_UNAVAILABLE = "Classifier unavailable for this week."
_AUG_AUTO_BALANCE_NO_SESSIONS = "No sessions to classify this week."
_CADENCE_NO_ACTIVITY = "No substantive sessions this week."


def _aug_auto_balance_section(panel: AugAutoBalancePanel | None) -> str:
    """Render the augmentation/automation balance panel (US-039).

    The eyebrow always renders so the document shape stays stable. The
    body falls through three states (same as the terminal renderer):
    classifier-unavailable, no-sessions, populated. The populated state
    surfaces the three shares plus the Anthropic Economic Index anchor
    so the reader can compare their habit against the industry baseline.
    """
    if panel is None or panel.classifier_unavailable:
        return f"""
  <section class="bal-section" id="aug-auto-balance">
    <div class="s-eyebrow">Augmentation / Automation</div>
    <p class="placeholder">{_safe(_AUG_AUTO_BALANCE_CLASSIFIER_UNAVAILABLE)}</p>
  </section>"""
    if panel.classified_total == 0:
        return f"""
  <section class="bal-section" id="aug-auto-balance">
    <div class="s-eyebrow">Augmentation / Automation</div>
    <p class="placeholder">{_safe(_AUG_AUTO_BALANCE_NO_SESSIONS)}</p>
  </section>"""
    aug_pct = int(round(panel.augmentation_share * 100))
    auto_pct = int(round(panel.automation_share * 100))
    mixed_pct = int(round(panel.mixed_share * 100))
    industry_aug = int(round(panel.industry_augmentation_share * 100))
    industry_auto = int(round(panel.industry_automation_share * 100))
    return f"""
  <section class="bal-section" id="aug-auto-balance">
    <div class="s-eyebrow">Augmentation / Automation</div>
    <div class="bal-row">
      <span class="bal-label">Augmentation</span>
      <span class="bal-value">{aug_pct}%</span>
    </div>
    <div class="bal-row">
      <span class="bal-label">Automation</span>
      <span class="bal-value">{auto_pct}%</span>
    </div>
    <div class="bal-row">
      <span class="bal-label">Mixed</span>
      <span class="bal-value">{mixed_pct}%</span>
    </div>
    <p class="bal-anchor">Industry anchor: {industry_aug}% augmentation / {industry_auto}% automation.</p>
    <p class="bal-citation">Source: {_safe(panel.industry_anchor_citation)}</p>
  </section>"""


def _cadence_section(panel: CadencePanel | None) -> str:
    """Render the cadence panel (US-039).

    The eyebrow renders unconditionally so the section's slot in the
    document doesn't move. When ``panel`` is None or carries zero
    substantive sessions, the renderer emits the explicit "No
    substantive sessions this week." copy and OMITS the
    high-adopter label. Otherwise it emits the streak, the
    high-adopter position (when on file), and the arXiv 2509.19708
    citation as a footnote.
    """
    if panel is None or not panel.has_activity:
        return f"""
  <section class="cad-section" id="cadence">
    <div class="s-eyebrow">Cadence</div>
    <p class="placeholder">{_safe(_CADENCE_NO_ACTIVITY)}</p>
  </section>"""
    position_html = ""
    label = panel.position_label
    if label:
        position_html = (
            f'<div class="cad-row">'
            f'<span class="cad-label">Spectrum</span>'
            f'<span class="cad-value">{_safe(label)}</span>'
            f'</div>'
        )
    return f"""
  <section class="cad-section" id="cadence">
    <div class="s-eyebrow">Cadence</div>
    <div class="cad-row">
      <span class="cad-label">Weekday streak</span>
      <span class="cad-value">{panel.weekday_streak} of {panel.window_days} days</span>
    </div>
    {position_html}
    <p class="cad-citation">Source: {_safe(panel.citation)}</p>
  </section>"""


def _behavioral_patterns_section(panel: BehavioralPatternsPanel | None) -> str:
    """Render the behavioral-patterns panel (US-038).

    For each signal kind, emits a row with the signal label, the count,
    and the primary-source citation as a small footnote. The renderer
    deliberately omits raw user-turn excerpts so the digest stays a
    coaching read rather than a transcript sample. When the panel has no
    signals (every count is zero) the renderer surfaces the empty-state
    message instead of an empty table.
    """
    if panel is None or not panel.has_signals:
        return f"""
  <section class="bp-section" id="behavioral-patterns">
    <div class="s-eyebrow">Behavioral Patterns</div>
    <p class="placeholder">{_safe(_BEHAVIORAL_PATTERNS_EMPTY)}</p>
  </section>"""
    rows: list[str] = []
    for row in panel.rows:
        if row.count <= 0:
            continue
        plural = "time" if row.count == 1 else "times"
        rows.append(
            f'<article class="bp-row">'
            f'<header class="bp-row-head">'
            f'<span class="bp-label">{_safe(row.label)}</span>'
            f'<span class="bp-count">{row.count} {plural}</span>'
            f'</header>'
            f'<footer class="bp-citation">Source: {_safe(row.citation)}</footer>'
            f'</article>'
        )
    return f"""
  <section class="bp-section" id="behavioral-patterns">
    <div class="s-eyebrow">Behavioral Patterns</div>
    <div class="bp-list">{"".join(rows)}</div>
  </section>"""


def _next_week_section(sentence: str) -> str:
    if not sentence:
        return """
  <section class="n-section" id="one-thing-to-try-next-week">
    <div class="s-eyebrow">One Thing To Try Next Week</div>
    <p class="placeholder">Next week's commitment will be drawn from this week's headline moment.</p>
  </section>"""
    return f"""
  <section class="n-section" id="one-thing-to-try-next-week">
    <div class="s-eyebrow">One Thing To Try Next Week</div>
    <p class="n-body">{_safe(sentence)}</p>
  </section>"""


# US-040: empty-state copy for the repeat-task radar and the
# verification-calibration panel. Tests assert on the literal text so
# a future copy change is one audit point per renderer.
_REPEAT_TASK_EMPTY = "No repeat tasks detected this week."
_VERIFICATION_CALIBRATION_NO_SESSIONS = (
    "No sessions to calibrate verification against this week."
)


def _format_minutes_html(minutes: float) -> str:
    """Match the terminal renderer's minute-formatting policy."""
    if abs(minutes - round(minutes)) < 0.05:
        return f"{int(round(minutes))}"
    return f"{minutes:.1f}"


def _repeat_task_radar_section(panel: RepeatTaskRadarPanel | None) -> str:
    """Render the repeat-task radar panel as one HTML section (US-040).

    The eyebrow renders unconditionally so the document shape is
    stable. When ``panel`` is None or carries no detected repeats the
    body falls through to the empty-state copy; otherwise it emits
    one row per RepeatTask with a public task label, the occurrence
    count, the per-occurrence minutes, and the "Could become a skill"
    tag, plus an inline citation footnote. It never shows the raw first
    prompt that caused the repeat detector to fire.
    """
    if panel is None or not panel.has_repeats:
        return f"""
  <section class="rt-section" id="repeat-task-radar">
    <div class="s-eyebrow">Repeat-Task Radar</div>
    <p class="placeholder">{_safe(_REPEAT_TASK_EMPTY)}</p>
  </section>"""
    rows: list[str] = []
    for row in panel.rows:
        minutes = _format_minutes_html(row.estimated_minutes_per_occurrence)
        plural = "time" if row.occurrences == 1 else "times"
        rows.append(
            f'<article class="rt-row">'
            f'<header class="rt-row-head">'
            f'<h3 class="rt-label">{_safe(row.canonical_first_sentence)}</h3>'
            f'<span class="rt-skill-tag">{_safe(row.skill_tag)}</span>'
            f'</header>'
            f'<p class="rt-meta">'
            f'Detected {row.occurrences} {plural}, about {minutes} min each'
            f'</p>'
            f'</article>'
        )
    reclaimable = _format_minutes_html(panel.total_reclaimable_minutes)
    return f"""
  <section class="rt-section" id="repeat-task-radar">
    <div class="s-eyebrow">Repeat-Task Radar</div>
    <p class="rt-summary">Skills or saved prompts could reclaim about {reclaimable} min/week.</p>
    <div class="rt-list">{"".join(rows)}</div>
    <p class="rt-citation">Source: {_safe(panel.citation)}</p>
  </section>"""


# US-041: empty-state copy for the specification-adoption, context-
# engineering-depth, and knowledge-gap distribution panels. Tests
# assert verbatim so a copy change is one audit point per renderer.
_SPECIFICATION_ADOPTION_NO_SESSIONS = (
    "No sessions to measure specification adoption this week."
)
_CONTEXT_ENGINEERING_NO_ARTIFACTS = (
    "No scaffolding artifacts referenced this week."
)
_KNOWLEDGE_GAP_EMPTY = "No knowledge gaps detected this week."


def _specification_adoption_section(
    panel: SpecificationAdoptionPanel | None,
) -> str:
    """Render the specification-adoption panel (US-041).

    The eyebrow renders unconditionally so the document shape is
    stable. When ``panel`` is None or carries zero sessions, the body
    falls through to the empty-state copy; otherwise it emits the
    share of sessions that opened with a spec block plus the Woodward
    / SpecKit / Sean Grove citation as a footnote.
    """
    if panel is None or not panel.has_sessions:
        return f"""
  <section class="sa-section" id="specification-adoption">
    <div class="s-eyebrow">Specification Adoption</div>
    <p class="placeholder">{_safe(_SPECIFICATION_ADOPTION_NO_SESSIONS)}</p>
  </section>"""
    pct = int(round(panel.adoption_share * 100))
    session_word = "session" if panel.total_sessions == 1 else "sessions"
    return f"""
  <section class="sa-section" id="specification-adoption">
    <div class="s-eyebrow">Specification Adoption</div>
    <div class="sa-row">
      <span class="sa-label">Opened with a spec block</span>
      <span class="sa-value">{pct}%</span>
    </div>
    <p class="sa-meta">{panel.sessions_with_spec} of {panel.total_sessions} {session_word} this week.</p>
    <p class="sa-citation">Source: {_safe(panel.citation)}</p>
  </section>"""


def _context_engineering_section(
    panel: ContextEngineeringDepthPanel | None,
) -> str:
    """Render the context-engineering-depth panel (US-041).

    When no scaffolding kinds fired across the week, the body
    collapses to the placeholder. Otherwise the renderer emits one row
    per kind that fired (skipping zero-count rows so the reader's eye
    is drawn to what they actually engage with), followed by the DORA
    2025 + Anthropic Skills citation.
    """
    if panel is None or not panel.has_any_artifact:
        return f"""
  <section class="ce-section" id="context-engineering-depth">
    <div class="s-eyebrow">Context Engineering Depth</div>
    <p class="placeholder">{_safe(_CONTEXT_ENGINEERING_NO_ARTIFACTS)}</p>
  </section>"""
    rows: list[str] = []
    for row in panel.rows:
        if row.sessions_with_artifact <= 0:
            continue
        session_word = (
            "session" if row.sessions_with_artifact == 1 else "sessions"
        )
        rows.append(
            f'<div class="ce-row">'
            f'<span class="ce-label">{_safe(row.label)}</span>'
            f'<span class="ce-value">{row.sessions_with_artifact} {session_word}</span>'
            f'</div>'
        )
    return f"""
  <section class="ce-section" id="context-engineering-depth">
    <div class="s-eyebrow">Context Engineering Depth</div>
    {"".join(rows)}
    <p class="ce-citation">Source: {_safe(panel.citation)}</p>
  </section>"""


def _knowledge_gap_distribution_section(
    panel: KnowledgeGapDistributionPanel | None,
) -> str:
    """Render the knowledge-gap distribution panel (US-041).

    Two states:
      1. ``panel is None`` or every category has zero count: emit the
         verbatim "No knowledge gaps detected this week." copy.
      2. At least one category is positive: emit one row per category
         in display order, including explicit zeros so the reader sees
         the absence of categories that did not fire. The citation
         footnote sits beneath the rows.
    """
    if panel is None or not panel.has_gaps:
        return f"""
  <section class="kg-section" id="knowledge-gap-distribution">
    <div class="s-eyebrow">Knowledge Gaps</div>
    <p class="placeholder">{_safe(_KNOWLEDGE_GAP_EMPTY)}</p>
  </section>"""
    rows: list[str] = []
    for row in panel.rows:
        turn_word = "turn" if row.count == 1 else "turns"
        rows.append(
            f'<div class="kg-row">'
            f'<span class="kg-label">{_safe(row.label)}</span>'
            f'<span class="kg-value">{row.count} {turn_word}</span>'
            f'</div>'
        )
    return f"""
  <section class="kg-section" id="knowledge-gap-distribution">
    <div class="s-eyebrow">Knowledge Gaps</div>
    {"".join(rows)}
    <p class="kg-citation">Source: {_safe(panel.citation)}</p>
  </section>"""


# US-042: empty-state copy for the tool/agent ladder + refined cost-
# effectiveness panel. Tests assert verbatim so a copy change is one
# audit point per renderer.
_TOOL_AGENT_LADDER_NO_ACTIVITY = "No tool/agent usage observed this week."
_COST_EFFECTIVENESS_NO_COST_DATA = "No cost data this week."


def _tool_agent_ladder_section(panel: ToolAgentLadderPanel | None) -> str:
    """Render the tool/agent ladder panel (US-042).

    The eyebrow always renders so the document shape stays stable. The
    body falls through two states: no activity -> placeholder copy;
    activity -> max-rung headline plus per-rung counts plus citation.
    """
    if panel is None or not panel.has_activity:
        return f"""
  <section class="tal-section" id="tool-agent-ladder">
    <div class="s-eyebrow">Tool / Agent Ladder</div>
    <p class="placeholder">{_safe(_TOOL_AGENT_LADDER_NO_ACTIVITY)}</p>
  </section>"""
    rows: list[str] = []
    for row in panel.rows:
        session_word = "session" if row.session_count == 1 else "sessions"
        rows.append(
            f'<div class="tal-row">'
            f'<span class="tal-label">{_safe(row.label)}</span>'
            f'<span class="tal-value">{row.session_count} {session_word}</span>'
            f'</div>'
        )
    return f"""
  <section class="tal-section" id="tool-agent-ladder">
    <div class="s-eyebrow">Tool / Agent Ladder</div>
    <p class="tal-max">Max rung this week: <strong>{_safe(panel.max_rung_label)}</strong></p>
    <div class="tal-list">{"".join(rows)}</div>
    <p class="tal-citation">Source: {_safe(panel.citation)}</p>
  </section>"""


def _refined_cost_effectiveness_section(
    panel: RefinedCostEffectivenessPanel | None,
) -> str:
    """Render the refined cost-effectiveness panel (US-042).

    Three states:
      1. ``panel`` is None or ``has_cost_data`` is False: emit the
         "No cost data this week." copy. Per AC, $0 overspend must not
         render in the absence of cost data because that would falsely
         imply optimality.
      2. Cost data present but no overspend: emit a positive-signal
         sentence so the absence of waste is itself visible.
      3. Cost data present AND overspend > 0: emit the canonical AC
         sentence ("You spent $X on <higher-tier> for tasks
         <lower-tier> could have done = $Y overspend").
    """
    if panel is None or not panel.has_cost_data:
        return f"""
  <section class="rce-section" id="refined-cost-effectiveness">
    <div class="s-eyebrow">Cost-Effectiveness</div>
    <p class="placeholder">{_safe(_COST_EFFECTIVENESS_NO_COST_DATA)}</p>
  </section>"""
    if not panel.has_overspend:
        return f"""
  <section class="rce-section" id="refined-cost-effectiveness">
    <div class="s-eyebrow">Cost-Effectiveness</div>
    <p class="rce-clean">No tier-mismatch overspend detected this week.</p>
    <p class="rce-citation">Source: {_safe(panel.citation)}</p>
  </section>"""
    higher = _safe(panel.higher_tier_display)
    lower = _safe(panel.lower_tier_display)
    spent = f"${panel.spent_on_higher_tier_usd:.2f}"
    overspend = f"${panel.overspend_usd:.2f}"
    session_word = (
        "session" if panel.qualifying_session_count == 1 else "sessions"
    )
    return f"""
  <section class="rce-section" id="refined-cost-effectiveness">
    <div class="s-eyebrow">Cost-Effectiveness</div>
    <p class="rce-headline">You spent <strong>{spent}</strong> on {higher} for tasks {lower} could have done = <strong>{overspend} overspend</strong>.</p>
    <p class="rce-meta">Across {panel.qualifying_session_count} qualifying {session_word}.</p>
    <p class="rce-citation">Source: {_safe(panel.citation)}</p>
  </section>"""


def _verification_calibration_section(
    panel: VerificationCalibrationPanel | None,
) -> str:
    """Render the verification-calibration panel (US-040).

    When the week has no sessions to categorize the body collapses to
    a single placeholder. When at least one session was categorized
    the renderer emits one row per bucket in display order so the
    reader sees the absence of rigorous buckets as clearly as their
    presence. The citation footnote sits beneath the rows.
    """
    if panel is None or not panel.has_sessions:
        return f"""
  <section class="vc-section" id="verification-calibration">
    <div class="s-eyebrow">Verification Calibration</div>
    <p class="placeholder">{_safe(_VERIFICATION_CALIBRATION_NO_SESSIONS)}</p>
  </section>"""
    rows: list[str] = []
    for kind in VERIFICATION_CALIBRATION_KINDS_IN_PANEL_ORDER:
        label = VERIFICATION_CALIBRATION_LABELS.get(kind, kind.title())
        count = panel.count_for(kind)
        plural = "session" if count == 1 else "sessions"
        rows.append(
            f'<div class="vc-row">'
            f'<span class="vc-label">{_safe(label)}</span>'
            f'<span class="vc-value">{count} {plural}</span>'
            f'</div>'
        )
    return f"""
  <section class="vc-section" id="verification-calibration">
    <div class="s-eyebrow">Verification Calibration</div>
    {"".join(rows)}
    <p class="vc-citation">Source: {_safe(panel.citation)}</p>
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
    behavioral_panel = (
        digest.panel_inputs.behavioral_signals
        if digest.panel_inputs is not None
        else None
    )
    aug_auto_panel = (
        digest.panel_inputs.aug_auto_balance
        if digest.panel_inputs is not None
        else None
    )
    cadence_panel = (
        digest.panel_inputs.cadence
        if digest.panel_inputs is not None
        else None
    )
    repeat_task_panel = (
        digest.panel_inputs.repeat_task_radar
        if digest.panel_inputs is not None
        else None
    )
    verification_panel = (
        digest.panel_inputs.verification_calibration
        if digest.panel_inputs is not None
        else None
    )
    specification_panel = (
        digest.panel_inputs.specification_adoption
        if digest.panel_inputs is not None
        else None
    )
    context_engineering_panel = (
        digest.panel_inputs.context_engineering
        if digest.panel_inputs is not None
        else None
    )
    knowledge_gap_panel = (
        digest.panel_inputs.knowledge_gap_distribution
        if digest.panel_inputs is not None
        else None
    )
    ladder_panel = (
        digest.panel_inputs.tool_agent_ladder
        if digest.panel_inputs is not None
        else None
    )
    cost_effectiveness_panel = (
        digest.panel_inputs.refined_cost_effectiveness
        if digest.panel_inputs is not None
        else None
    )
    data_block = (
        '<div class="data-block">'
        '<div class="data-block__rule"></div>'
        '<div class="data-block__label">The numbers behind it</div>'
        f'{_cost_ledger_section(digest.cost_ledger)}'
        f'{_task_breakdown_section(digest.task_breakdown)}'
        f'{_dimensions_section(digest.dimensions, digest.behavioral_signals)}'
        f'{_behavioral_patterns_section(behavioral_panel)}'
        f'{_aug_auto_balance_section(aug_auto_panel)}'
        f'{_cadence_section(cadence_panel)}'
        f'{_repeat_task_radar_section(repeat_task_panel)}'
        f'{_verification_calibration_section(verification_panel)}'
        f'{_specification_adoption_section(specification_panel)}'
        f'{_context_engineering_section(context_engineering_panel)}'
        f'{_knowledge_gap_distribution_section(knowledge_gap_panel)}'
        f'{_tool_agent_ladder_section(ladder_panel)}'
        f'{_refined_cost_effectiveness_section(cost_effectiveness_panel)}'
        f'{_weekly_trajectory_section(digest.weekly_trajectory)}'
        '</div>'
    )
    # Spec section 2 (coaching-reposition): the masthead's commitment
    # block sits ABOVE the trajectory hero so the digest opens with the
    # active commitment status. When the digest carries no rollup the
    # helper returns an empty string and the block is omitted entirely.
    body_sections = (
        _commitment_section(digest.commitment_rollup)
        + _trajectory_section(
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
/* =================================================================
   Praxis weekly digest — editorial design system
   ================================================================= */
:root {{
  /* Cream ground, ink primary, two accents (terracotta = emphasis /
     warning / low score; ink-blue = citation / counterweight /
     considered detail). All in OKLCH so perceptual lightness steps
     are uniform; near-extreme lightness uses lower chroma to avoid
     the AI-design garish-on-light look. */
  --cream:           oklch(96.5% 0.005 80);     /* warm page ground */
  --cream-edge:      oklch(93.5% 0.008 75);     /* hairline cards */
  --cream-deep:      oklch(91% 0.012 75);       /* slightly stronger fill */
  --ink:             oklch(22% 0.012 60);       /* primary text, warm dark */
  --ink-muted:       oklch(46% 0.010 55);       /* secondary text */
  --ink-faded:       oklch(63% 0.008 55);       /* tertiary / chrome text */
  --ink-hint:        oklch(80% 0.006 55);       /* faintest, near-invisible */
  --accent:          oklch(56% 0.135 38);       /* terracotta */
  --accent-deep:     oklch(45% 0.130 35);       /* darker terracotta */
  --accent-soft:     oklch(56% 0.135 38 / 0.10);
  --accent-faint:    oklch(56% 0.135 38 / 0.05);
  --counter:         oklch(38% 0.070 232);      /* ink-blue counterweight */
  --counter-soft:    oklch(38% 0.070 232 / 0.10);
  --counter-faint:   oklch(38% 0.070 232 / 0.05);
  --rule:            oklch(22% 0.012 60 / 0.08);
  --rule-strong:     oklch(22% 0.012 60 / 0.15);

  /* Serif stack: prefers well-drawn system serifs on each OS before
     falling back to Georgia. Avoids the Libre Baskerville / Fraunces
     monoculture. Iowan Old Style ships on macOS / iOS by default;
     Sitka Text ships on Windows; Charter on macOS as a backup;
     Cambria as a Windows backup. */
  --serif: 'Iowan Old Style', 'Sitka Text', 'Hoefler Text',
           'Charter', 'Cambria', 'Georgia', serif;
  --sans:  -apple-system, BlinkMacSystemFont, 'SF Pro Text',
           'Segoe UI Variable', 'Segoe UI', system-ui, sans-serif;

  /* 4pt-grid spacing. Semantic names. */
  --space-2:  4px;
  --space-3:  8px;
  --space-4:  12px;
  --space-5:  16px;
  --space-6:  24px;
  --space-7:  32px;
  --space-8:  48px;
  --space-9:  64px;
  --space-10: 96px;
  --space-11: 128px;
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

/* --- Commitment block (spec section 2) ------------------------------
   The masthead's commitment block opens the digest with the active
   commitment status (focus quote + how-it-went status). Field labels
   and ordering mirror the terminal renderer's _commitment_block so
   the two surfaces read as one product. The block is omitted when
   no follow-up exists for the week, so this CSS is dormant on
   first-run digests. */
.cb-section {{
  margin: 0 0 64px;
  padding-bottom: 40px;
  border-bottom: 1px solid var(--rule);
}}
.cb-eyebrow {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 500;
  margin-bottom: 14px;
}}
.cb-focus {{
  margin-bottom: 32px;
}}
.cb-quote {{
  font-family: var(--serif);
  font-size: 22px;
  line-height: 1.45;
  color: var(--ink);
  font-style: italic;
  max-width: 560px;
  margin: 0;
  padding: 0;
}}
.cb-quote::before {{
  content: "\\201C";
  color: var(--accent);
  font-style: normal;
  margin-right: 2px;
}}
.cb-quote::after {{
  content: "\\201D";
  color: var(--accent);
  font-style: normal;
  margin-left: 2px;
}}
.cb-status {{
  margin-top: 0;
}}
.cb-no-sessions {{
  font-family: var(--serif);
  font-size: 16px;
  font-style: italic;
  color: var(--ink-muted);
  margin: 0;
}}
.cb-fields {{
  display: grid;
  grid-template-columns: minmax(96px, max-content) 1fr;
  gap: 8px 18px;
  margin: 0;
}}
.cb-fields dt {{
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-muted);
  font-weight: 500;
  margin: 0;
  align-self: baseline;
}}
.cb-fields dd {{
  font-family: var(--serif);
  font-size: 16px;
  line-height: 1.45;
  color: var(--ink);
  margin: 0;
}}
.cb-gap--agree {{
  color: var(--ink-muted);
}}
.cb-gap--disagree {{
  color: var(--accent-deep);
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

/* --- Recurrence callout on the moment.
       Notable design choice: we DO NOT use a left-border accent stripe
       (the single most overused AI design touch). Instead, the
       recurrence chip uses an inline marker glyph + small caps text
       on a tinted background. The reader feels the escalation through
       typography, not through a colored bar. */
.m-recurrence {{
  display: inline-flex;
  align-items: center;
  gap: var(--space-4);
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--accent-deep);
  background: var(--accent-soft);
  padding: 8px 14px;
  margin-bottom: var(--space-6);
  font-weight: 500;
}}
.m-recurrence::before {{
  content: "\\21BB";
  font-family: var(--serif);
  font-size: 16px;
  letter-spacing: 0;
  line-height: 1;
  color: var(--accent);
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
  margin-bottom: var(--space-10);
}}
/* Tufte small multiples: 4 mini line charts arranged in a row at wide
   viewports, 2x2 on tablet, stacked on phone. Each chart shares the
   y-axis range [0,1] so the eye can compare amplitude across signals. */
.mc-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1px;
  background: var(--rule);
  margin-top: var(--space-7);
}}
.mc {{
  background: var(--cream);
  padding: var(--space-6) var(--space-6) var(--space-5);
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}}
.mc-title {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-muted);
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-weight: 500;
}}
.mc-current {{
  font-family: var(--serif);
  font-size: 18px;
  letter-spacing: -0.01em;
  text-transform: none;
  font-weight: 400;
  color: var(--ink);
}}
.mc-chart {{
  width: 100%;
  height: auto;
  display: block;
}}
.mc-midline {{
  stroke: var(--rule);
  stroke-width: 1;
}}
.mc-line {{
  stroke-width: 1.6;
  stroke-linejoin: round;
  stroke-linecap: round;
}}
.mc-line--accent   {{ stroke: var(--accent); }}
.mc-line--counter  {{ stroke: var(--counter); }}
.mc-dot--accent   {{ fill: var(--accent); }}
.mc-dot--counter  {{ fill: var(--counter); }}
.mc-axis {{
  font-family: var(--sans);
  font-size: 9.5px;
  letter-spacing: 0.06em;
  fill: var(--ink-faded);
}}
.mc-note {{
  font-family: var(--serif);
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--ink-muted);
  font-style: italic;
  padding-top: var(--space-3);
  border-top: 1px solid var(--rule);
}}

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
/* Narrative paragraph that carries the comparison + biggest line + tier
   note in one sentence, instead of a 3-tile dashboard grid. The big
   amount sits inline above this paragraph; this prose tells the reader
   what the amount means. */
.c-narrative {{
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.55;
  color: var(--ink);
  max-width: 62ch;
  margin: 14px 0 32px 0;
}}
.c-narrative-em {{
  font-style: italic;
  color: var(--ink-muted);
}}

/* --- 4. Where the week went (tasks) --------------------------------- */
.w-section {{
  margin-bottom: var(--space-10);
}}
/* Tasks as a film-festival-lineup typography list. A large display
   serif numeral anchors the row at left; the task label is the body
   serif at right. A thin terracotta hairline separates rows. No card
   chrome; the numeral does the work. */
.w-task {{
  display: grid;
  grid-template-columns: 96px 1fr;
  align-items: baseline;
  gap: var(--space-6);
  padding: var(--space-7) 0;
  border-top: 1px solid var(--rule);
}}
.w-task:last-child {{ border-bottom: 1px solid var(--rule); }}
.w-num {{
  font-family: var(--serif);
  font-size: 56px;
  line-height: 1;
  letter-spacing: -0.03em;
  color: var(--accent);
  font-feature-settings: 'lnum';
}}
.w-body {{
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}}
.w-label {{
  font-family: var(--serif);
  font-size: 24px;
  line-height: 1.25;
  color: var(--ink);
  font-weight: 400;
  letter-spacing: -0.01em;
  margin: 0;
}}
.w-meta {{
  font-family: var(--sans);
  font-size: 12.5px;
  color: var(--ink-muted);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  letter-spacing: 0.02em;
}}
.w-meta-stat {{
  color: var(--ink);
}}
.w-meta-dot {{
  color: var(--ink-hint);
}}
.w-worst {{
  color: var(--accent-deep);
  font-style: italic;
}}

/* --- 5. Dimensions -------------------------------------------------- */
.d-section {{
  margin-bottom: 96px;
}}

/* Dimension card (FIFA-style arc-indicator + small-multiples grid).
   Six cards in a 3-by-2 grid on wide viewports, 2-by-3 on tablet,
   1-by-6 on phone. Each card carries the dim's title in serif, the
   score as a big serif number, an arc-fill ring indicating the score
   on a ten-point scale, a tick mark for the 90-day baseline (when on
   file), and the dim's evidence citation as a footnote. The arc ring
   is the FIFA-card moment: it conveys "where on the scale you are"
   without the polygon-radar's notorious illegibility. */
.dims-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 1px;
  background: var(--rule);
  margin: var(--space-7) 0 var(--space-8);
}}
.dim-card {{
  background: var(--cream);
  padding: var(--space-7) var(--space-6) var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  position: relative;
}}
.dim-card__head {{
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-5);
}}
.dim-card__title {{
  font-family: var(--serif);
  font-size: 16px;
  line-height: 1.3;
  color: var(--ink);
  letter-spacing: -0.005em;
  max-width: 12ch;
}}
.dim-card__ring {{
  width: 56px;
  height: 56px;
  flex: 0 0 56px;
  position: relative;
}}
.dim-card__ring-score {{
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--serif);
  font-size: 18px;
  color: var(--ink);
  letter-spacing: -0.01em;
  font-feature-settings: 'lnum';
}}
.dim-card__ring-track {{ stroke: var(--rule-strong); fill: none; }}
.dim-card__ring-fill {{
  stroke: var(--accent);
  fill: none;
  stroke-linecap: butt;
  transition: none;
}}
.dim-card__ring-base-tick {{
  stroke: var(--counter);
  stroke-width: 2;
}}
.dim-card__delta {{
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--ink-muted);
}}
.dim-card__delta--up    {{ color: var(--counter); }}
.dim-card__delta--down  {{ color: var(--accent); }}
.dim-card__evidence {{
  font-family: var(--serif);
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--ink-muted);
  font-style: italic;
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--rule);
}}

/* Behavioral signal card variant: swaps the ring fill colour to ink-blue
   (the secondary "considered counterweight" accent) so rising-is-bad
   signals (Delegation) read differently from rising-is-good signals
   (Engagement, Independence, Verification). Geometry is identical to
   the dim card; only the colour and the unit glyph change. */
.dim-card--counter .dim-card__ring-fill {{
  stroke: var(--counter);
}}
.dim-card--counter .dim-card__delta--up {{
  color: var(--accent);
}}
.dim-card--counter .dim-card__delta--down {{
  color: var(--counter);
}}
.dim-card__ring-unit {{
  font-size: 11px;
  font-family: var(--sans);
  color: var(--ink-muted);
  letter-spacing: 0.02em;
  margin-left: 1px;
  vertical-align: 0.6em;
}}

/* Divider between rubric cards (0-10) and behavioral cards (0-100%).
   A single hairline plus an inline eyebrow makes the unit shift legible
   without breaking the dim section into two top-level blocks. */
.d-divider {{
  height: 1px;
  background: var(--rule);
  margin: var(--space-8) 0 var(--space-6) 0;
}}
.d-subhead {{
  display: flex;
  align-items: baseline;
  gap: var(--space-5);
  margin-bottom: var(--space-5);
  flex-wrap: wrap;
}}
.d-subhead-note {{
  font-family: var(--serif);
  font-size: 13px;
  font-style: italic;
  color: var(--ink-muted);
  max-width: 56ch;
}}
.s-eyebrow--inline {{
  margin-bottom: 0;
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

/* --- 5b. Behavioral patterns (US-038) ------------------------------- */
/* Editorial list, not a card grid: a header row with the signal label
   and count, two italic excerpt rows beneath, and a small ink-faded
   citation footnote. The eye scans the labels + counts left-to-right
   first, then drops into the excerpts for what triggered them. */
.bp-section {{
  margin-bottom: 96px;
}}
.bp-list {{
  display: flex;
  flex-direction: column;
}}
.bp-row {{
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-6) 0;
  border-top: 1px solid var(--rule);
}}
.bp-row:last-child {{
  border-bottom: 1px solid var(--rule);
}}
.bp-row-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-5);
}}
.bp-label {{
  font-family: var(--serif);
  font-size: 18px;
  color: var(--ink);
  letter-spacing: -0.005em;
}}
.bp-count {{
  font-family: var(--serif);
  font-size: 20px;
  color: var(--accent);
  font-feature-settings: 'lnum';
  letter-spacing: -0.01em;
}}
.bp-excerpts {{
  list-style: none;
  padding: 0;
  margin: 0;
}}
.bp-excerpt {{
  font-family: var(--serif);
  font-size: 14.5px;
  font-style: italic;
  line-height: 1.5;
  color: var(--ink-muted);
  padding-left: var(--space-5);
  margin-bottom: var(--space-2);
  position: relative;
}}
.bp-excerpt::before {{
  content: "·";
  position: absolute;
  left: var(--space-2);
  color: var(--accent);
  font-size: 18px;
  line-height: 1.1;
}}
.bp-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-2);
}}

/* --- 5c. Augmentation / Automation balance (US-039) ----------------- */
/* Three rows with a label-and-value pair each, separated by hairlines.
   Designed to read as a typographic stat list (the numbers carry the
   weight) rather than a stacked-bar visualization, keeping editorial
   discipline with the rest of the digest. */
.bal-section {{
  margin-bottom: 96px;
}}
.bal-row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--space-5) 0;
  border-top: 1px solid var(--rule);
}}
.bal-row:last-of-type {{
  border-bottom: 1px solid var(--rule);
}}
.bal-label {{
  font-family: var(--serif);
  font-size: 16px;
  color: var(--ink);
}}
.bal-value {{
  font-family: var(--serif);
  font-size: 22px;
  color: var(--accent);
  font-feature-settings: 'lnum';
  letter-spacing: -0.01em;
}}
.bal-anchor {{
  font-family: var(--serif);
  font-size: 14px;
  font-style: italic;
  color: var(--ink-muted);
  margin-top: var(--space-5);
  max-width: 62ch;
}}
.bal-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-2);
}}

/* --- 5d. Cadence (US-039) ------------------------------------------- */
/* Mirrors the augmentation/automation balance row layout so the two
   panels read as a pair: streak + spectrum position + citation. */
.cad-section {{
  margin-bottom: 96px;
}}
.cad-row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--space-5) 0;
  border-top: 1px solid var(--rule);
}}
.cad-row:last-of-type {{
  border-bottom: 1px solid var(--rule);
}}
.cad-label {{
  font-family: var(--serif);
  font-size: 16px;
  color: var(--ink);
}}
.cad-value {{
  font-family: var(--serif);
  font-size: 22px;
  color: var(--accent);
  font-feature-settings: 'lnum';
  letter-spacing: -0.01em;
}}
.cad-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-2);
}}

/* --- 5e. Repeat-task radar (US-040) --------------------------------- */
/* Editorial list of recurring task seeds. Each row pairs the quoted
   first sentence (italic) with a small "Could become a skill" tag and
   a dim meta row (occurrences + minutes). Layout intentionally mirrors
   the behavioral-patterns rows so the editorial cadence stays uniform. */
.rt-section {{
  margin-bottom: 96px;
}}
.rt-list {{
  display: flex;
  flex-direction: column;
}}
.rt-row {{
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-6) 0;
  border-top: 1px solid var(--rule);
}}
.rt-row:last-child {{
  border-bottom: 1px solid var(--rule);
}}
.rt-row-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-5);
  flex-wrap: wrap;
}}
.rt-quote {{
  font-family: var(--serif);
  font-size: 17px;
  font-style: italic;
  color: var(--ink);
  letter-spacing: -0.005em;
  max-width: 48ch;
}}
.rt-label {{
  font-family: var(--serif);
  font-size: 18px;
  font-weight: 400;
  color: var(--ink);
  letter-spacing: -0.005em;
  margin: 0;
  max-width: 48ch;
}}
.rt-skill-tag {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  padding: 2px 8px;
  border: 1px solid var(--accent);
  border-radius: 2px;
  white-space: nowrap;
}}
.rt-meta {{
  font-family: var(--serif);
  font-size: 14px;
  font-style: italic;
  color: var(--ink-muted);
  font-feature-settings: 'lnum';
}}
.rt-summary {{
  font-family: var(--serif);
  font-size: 16px;
  line-height: 1.55;
  color: var(--ink);
  margin-bottom: var(--space-5);
  max-width: 56ch;
}}
.rt-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-5);
}}

/* --- 5f. Verification calibration (US-040) -------------------------- */
/* Histogram of how rigorously the user verified AI output this week,
   bucketed source-check / test-run / spot-check / blanket-accept. The
   row layout mirrors the augmentation/automation balance panel so the
   reader can scan both as paired stat lists. */
.vc-section {{
  margin-bottom: 96px;
}}
.vc-row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--space-5) 0;
  border-top: 1px solid var(--rule);
}}
.vc-row:last-of-type {{
  border-bottom: 1px solid var(--rule);
}}
.vc-label {{
  font-family: var(--serif);
  font-size: 16px;
  color: var(--ink);
}}
.vc-value {{
  font-family: var(--serif);
  font-size: 22px;
  color: var(--accent);
  font-feature-settings: 'lnum';
  letter-spacing: -0.01em;
}}
.vc-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-5);
}}

/* --- US-041 specification adoption ---------------------------------- */
.sa-section {{
  margin-bottom: 96px;
}}
.sa-row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--space-5) 0;
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
}}
.sa-label {{
  font-family: var(--serif);
  font-size: 16px;
  color: var(--ink);
}}
.sa-value {{
  font-family: var(--serif);
  font-size: 26px;
  color: var(--accent);
  font-feature-settings: 'lnum';
  letter-spacing: -0.01em;
}}
.sa-meta {{
  font-family: var(--serif);
  font-size: 14px;
  color: var(--ink-muted);
  margin-top: var(--space-4);
}}
.sa-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-5);
}}

/* --- US-041 context engineering ------------------------------------- */
.ce-section {{
  margin-bottom: 96px;
}}
.ce-row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--space-5) 0;
  border-top: 1px solid var(--rule);
}}
.ce-row:last-of-type {{
  border-bottom: 1px solid var(--rule);
}}
.ce-label {{
  font-family: var(--serif);
  font-size: 16px;
  color: var(--ink);
}}
.ce-value {{
  font-family: var(--serif);
  font-size: 18px;
  color: var(--accent);
  font-feature-settings: 'lnum';
  letter-spacing: -0.01em;
}}
.ce-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-5);
}}

/* --- US-041 knowledge-gap distribution ------------------------------ */
.kg-section {{
  margin-bottom: 96px;
}}
.kg-row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--space-5) 0;
  border-top: 1px solid var(--rule);
}}
.kg-row:last-of-type {{
  border-bottom: 1px solid var(--rule);
}}
.kg-label {{
  font-family: var(--serif);
  font-size: 16px;
  color: var(--ink);
}}
.kg-value {{
  font-family: var(--serif);
  font-size: 18px;
  color: var(--accent);
  font-feature-settings: 'lnum';
  letter-spacing: -0.01em;
}}
.kg-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-5);
}}

/* --- US-042 tool/agent ladder --------------------------------------- */
.tal-section {{
  margin-bottom: 96px;
}}
.tal-max {{
  font-family: var(--serif);
  font-size: 17px;
  color: var(--ink);
  margin-bottom: var(--space-5);
}}
.tal-max strong {{
  color: var(--accent);
  font-weight: 500;
}}
.tal-list {{
  margin-bottom: var(--space-5);
}}
.tal-row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: var(--space-4) 0;
  border-top: 1px solid var(--rule);
}}
.tal-row:last-of-type {{
  border-bottom: 1px solid var(--rule);
}}
.tal-label {{
  font-family: var(--serif);
  font-size: 15.5px;
  color: var(--ink);
}}
.tal-value {{
  font-family: var(--serif);
  font-size: 16px;
  color: var(--ink-muted);
  font-feature-settings: 'lnum';
}}
.tal-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-5);
}}

/* --- US-042 refined cost-effectiveness ------------------------------ */
.rce-section {{
  margin-bottom: 96px;
}}
.rce-headline {{
  font-family: var(--serif);
  font-size: 18px;
  line-height: 1.55;
  color: var(--ink);
  margin-bottom: var(--space-4);
  max-width: 560px;
}}
.rce-headline strong {{
  color: var(--accent);
  font-weight: 500;
}}
.rce-clean {{
  font-family: var(--serif);
  font-size: 17px;
  color: var(--ink);
  font-style: italic;
  margin-bottom: var(--space-4);
}}
.rce-meta {{
  font-family: var(--sans);
  font-size: 12px;
  letter-spacing: 0.05em;
  color: var(--ink-muted);
  margin-bottom: var(--space-5);
}}
.rce-citation {{
  font-family: var(--sans);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--ink-faded);
  font-style: italic;
  margin-top: var(--space-5);
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

/* =================================================================
   Dossier visual pass
   Wider editorial canvas, geometric texture, purposeful cards, and
   quiet motion. This layer intentionally overrides parts of the older
   single-column article styling above.
   ================================================================= */
:root {{
  --sage:            oklch(62% 0.055 148);
  --sage-soft:       oklch(62% 0.055 148 / 0.10);
  --ochre:           oklch(72% 0.105 82);
  --ochre-soft:      oklch(72% 0.105 82 / 0.13);
  --paper:           oklch(98% 0.007 78);
  --paper-warm:      oklch(94.5% 0.012 70);
  --shadow-paper:    oklch(38% 0.035 55 / 0.15);
  --ease-out-quint:  cubic-bezier(0.22, 1, 0.36, 1);
  --ease-out-expo:   cubic-bezier(0.16, 1, 0.3, 1);
}}

@keyframes dossier-rise {{
  from {{ opacity: 0; transform: translateY(18px); }}
  to   {{ opacity: 1; transform: translateY(0); }}
}}

@keyframes dossier-drift {{
  from {{ transform: translate3d(0, 0, 0); }}
  to   {{ transform: translate3d(-24px, 18px, 0); }}
}}

@keyframes ring-draw {{
  from {{ stroke-dashoffset: 180; }}
}}

body {{
  min-height: 100vh;
  background:
    linear-gradient(115deg, oklch(56% 0.135 38 / 0.07), transparent 34%),
    linear-gradient(15deg, transparent 58%, oklch(38% 0.070 232 / 0.08)),
    radial-gradient(circle at 1px 1px, oklch(22% 0.012 60 / 0.10) 1px, transparent 1.4px),
    var(--cream);
  background-size: auto, auto, 22px 22px, auto;
  position: relative;
  overflow-x: hidden;
}}

body::before {{
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  opacity: 0.42;
  background:
    repeating-linear-gradient(
      100deg,
      transparent 0,
      transparent 56px,
      oklch(38% 0.070 232 / 0.055) 56px,
      oklch(38% 0.070 232 / 0.055) 57px
    ),
    repeating-linear-gradient(
      0deg,
      transparent 0,
      transparent 112px,
      oklch(56% 0.135 38 / 0.055) 112px,
      oklch(56% 0.135 38 / 0.055) 113px
    );
  animation: dossier-drift 22s linear alternate infinite;
}}

.page {{
  width: min(100%, 1180px);
  max-width: none;
  position: relative;
  z-index: 1;
  isolation: isolate;
  padding: clamp(28px, 5vw, 72px) clamp(18px, 4vw, 56px) 128px;
  counter-reset: dossier-section;
}}

.page::before {{
  content: "";
  position: absolute;
  z-index: -1;
  top: 104px;
  right: clamp(10px, 4vw, 52px);
  width: min(34vw, 360px);
  aspect-ratio: 1;
  border: 1px solid oklch(22% 0.012 60 / 0.10);
  background:
    linear-gradient(135deg, transparent 0 41%, var(--accent-soft) 41% 44%, transparent 44%),
    linear-gradient(45deg, transparent 0 58%, var(--counter-faint) 58% 61%, transparent 61%),
    repeating-linear-gradient(90deg, transparent 0 18px, oklch(22% 0.012 60 / 0.045) 18px 19px);
  clip-path: polygon(8% 0, 100% 14%, 86% 100%, 0 82%);
}}

.masthead {{
  justify-content: space-between;
  align-items: end;
  gap: var(--space-6);
  margin-bottom: clamp(28px, 5vw, 64px);
  padding: var(--space-5) 0;
  border-top: 1px solid var(--rule-strong);
  border-bottom: 1px solid var(--rule-strong);
}}

.masthead-brand {{
  display: flex;
  flex-direction: column;
  gap: 2px;
}}

.masthead-kicker,
.masthead-week,
.masthead-meta {{
  font-family: var(--sans);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
}}

.masthead-kicker {{
  color: var(--accent);
  font-weight: 700;
}}

.masthead-title {{
  font-family: var(--serif);
  font-size: clamp(18px, 2vw, 25px);
  line-height: 1.1;
  letter-spacing: -0.015em;
  color: var(--ink);
}}

.masthead-week {{
  color: var(--counter);
  justify-self: center;
}}

.masthead-meta {{
  color: var(--ink-muted);
  text-align: right;
}}

.cb-section {{
  display: grid;
  grid-template-columns: minmax(0, 1.12fr) minmax(280px, 0.88fr);
  gap: clamp(28px, 5vw, 64px);
  margin-bottom: clamp(48px, 7vw, 92px);
  padding: clamp(26px, 4vw, 44px);
  border: 1px solid var(--rule-strong);
  border-radius: 8px;
  background:
    linear-gradient(90deg, var(--paper), oklch(96% 0.010 74 / 0.92)),
    repeating-linear-gradient(135deg, transparent 0 12px, oklch(56% 0.135 38 / 0.05) 12px 13px);
  box-shadow: 0 28px 70px var(--shadow-paper);
}}

.cb-focus,
.cb-status {{
  margin: 0;
}}

.cb-quote {{
  font-size: clamp(22px, 2.4vw, 34px);
  line-height: 1.25;
  max-width: 18ch;
}}

.cb-fields {{
  gap: 12px 20px;
}}

.t-hero {{
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
  gap: clamp(28px, 5vw, 72px);
  padding: clamp(34px, 6vw, 76px);
  margin-bottom: clamp(64px, 9vw, 120px);
  border: 1px solid var(--rule-strong);
  border-radius: 8px;
  overflow: hidden;
  background:
    linear-gradient(120deg, var(--paper), oklch(94.5% 0.018 74)),
    repeating-linear-gradient(0deg, transparent 0 31px, oklch(22% 0.012 60 / 0.06) 31px 32px);
  box-shadow: 0 36px 90px var(--shadow-paper);
  animation: dossier-rise 720ms var(--ease-out-quint) both;
}}

.t-hero::before {{
  content: "";
  position: absolute;
  right: -80px;
  top: -70px;
  width: 420px;
  height: 420px;
  background:
    repeating-conic-gradient(
      from 20deg,
      oklch(56% 0.135 38 / 0.13) 0deg 9deg,
      transparent 9deg 18deg
    );
  clip-path: polygon(50% 0, 100% 38%, 82% 100%, 18% 100%, 0 38%);
  opacity: 0.72;
}}

.t-hero::after {{
  content: "";
  position: absolute;
  inset: auto 0 0 auto;
  width: min(52%, 560px);
  height: 36%;
  background:
    linear-gradient(135deg, transparent 0 44%, var(--ochre-soft) 44% 68%, transparent 68%),
    linear-gradient(20deg, transparent 0 60%, var(--sage-soft) 60%);
  clip-path: polygon(18% 0, 100% 22%, 100% 100%, 0 100%);
}}

.t-hero > * {{
  position: relative;
  z-index: 1;
}}

.t-eyebrow,
.t-label,
.t-rule,
.t-lede {{
  grid-column: 1;
}}

.t-label {{
  font-size: clamp(72px, 10vw, 148px);
  max-width: 8ch;
  margin-bottom: 16px;
}}

.t-rule {{
  width: 96px;
  height: 6px;
  background:
    linear-gradient(90deg, var(--accent) 0 45%, var(--ochre) 45% 70%, var(--counter) 70%);
  margin-bottom: 26px;
}}

.t-lede {{
  max-width: 34ch;
  font-size: clamp(19px, 2vw, 27px);
  line-height: 1.42;
}}

.vitals {{
  grid-column: 2;
  grid-row: 1 / span 5;
  align-self: stretch;
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--space-4);
  margin-top: 0;
  background: transparent;
  border: 0;
}}

.vital {{
  min-height: 118px;
  border: 1px solid oklch(22% 0.012 60 / 0.13);
  border-radius: 8px;
  background: oklch(98% 0.006 78 / 0.80);
  padding: var(--space-6);
  justify-content: space-between;
  backdrop-filter: none;
  transition: transform 220ms var(--ease-out-quint), background-color 220ms var(--ease-out-quint);
}}

.vital:hover {{
  transform: translateY(-3px);
  background: var(--paper);
}}

.vital-value {{
  font-size: clamp(30px, 3vw, 44px);
  letter-spacing: -0.03em;
}}

.coaching {{
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(280px, 0.75fr);
  gap: var(--space-7);
  margin-bottom: clamp(72px, 9vw, 128px);
  padding: clamp(34px, 5vw, 64px) 0;
  border-top: 1px solid var(--rule-strong);
  border-bottom: 1px solid var(--rule-strong);
}}

.coaching::before {{
  content: "Coaching read";
  position: absolute;
  top: -12px;
  left: 0;
  font-family: var(--sans);
  font-size: 10px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--cream);
  background: var(--ink);
  padding: 4px 10px;
}}

.coaching .m-section {{
  grid-column: 1;
  grid-row: 1 / span 3;
  margin: 0;
  padding: clamp(28px, 4vw, 44px);
  border: 1px solid var(--rule-strong);
  border-radius: 8px;
  background:
    linear-gradient(180deg, var(--paper), oklch(95.5% 0.010 74)),
    radial-gradient(circle at 1px 1px, oklch(56% 0.135 38 / 0.13) 1px, transparent 1.5px);
  background-size: auto, 18px 18px;
}}

.coaching .pattern-section,
.coaching .f-section,
.coaching .n-section {{
  grid-column: 2;
  margin: 0;
  padding: var(--space-7);
  border: 1px solid var(--rule);
  border-radius: 8px;
  background: oklch(98% 0.006 78 / 0.70);
}}

.coaching .f-section,
.coaching .n-section {{
  border-top: 1px solid var(--rule);
}}

.m-body,
.f-body,
.n-body,
.pattern-list li {{
  max-width: 64ch;
}}

.data-block {{
  position: relative;
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: var(--space-6);
  margin-top: 0;
  padding-top: var(--space-9);
}}

.data-block::before {{
  content: "";
  position: absolute;
  inset: 0 calc(50% - 50vw);
  z-index: -1;
  background:
    linear-gradient(180deg, oklch(93.5% 0.010 74 / 0.68), transparent 64%),
    repeating-linear-gradient(90deg, transparent 0 88px, oklch(22% 0.012 60 / 0.045) 88px 89px);
  border-top: 1px solid var(--rule-strong);
  border-bottom: 1px solid var(--rule);
}}

.data-block__rule,
.data-block__label {{
  grid-column: 1 / -1;
}}

.data-block__rule {{
  height: 3px;
  width: min(280px, 42vw);
  background: linear-gradient(90deg, var(--accent), var(--ochre), var(--counter));
  margin: 0;
}}

.data-block__label {{
  margin-bottom: var(--space-7);
  color: var(--ink);
}}

.data-block > section {{
  counter-increment: dossier-section;
  position: relative;
  animation: dossier-rise 720ms var(--ease-out-quint) both;
  animation-delay: 80ms;
}}

.data-block > section:nth-of-type(2) {{ animation-delay: 115ms; }}
.data-block > section:nth-of-type(3) {{ animation-delay: 150ms; }}
.data-block > section:nth-of-type(4) {{ animation-delay: 185ms; }}
.data-block > section:nth-of-type(5) {{ animation-delay: 220ms; }}
.data-block > section:nth-of-type(n + 6) {{ animation-delay: 255ms; }}

.data-block > section::before {{
  content: counter(dossier-section, decimal-leading-zero);
  position: absolute;
  right: var(--space-5);
  top: var(--space-4);
  font-family: var(--serif);
  font-size: 34px;
  line-height: 1;
  letter-spacing: -0.04em;
  color: oklch(22% 0.012 60 / 0.13);
  pointer-events: none;
}}

.c-section,
.w-section {{
  grid-column: span 6;
}}

.d-section,
.bp-section,
.rt-section,
.wt-section {{
  grid-column: 1 / -1;
}}

.bal-section,
.cad-section,
.vc-section,
.sa-section,
.ce-section,
.kg-section,
.tal-section,
.rce-section {{
  grid-column: span 6;
}}

.data-block > section:not(.d-section):not(.wt-section) {{
  margin-bottom: 0;
  padding: clamp(24px, 3vw, 36px);
  border: 1px solid var(--rule-strong);
  border-radius: 8px;
  background: oklch(98% 0.006 78 / 0.78);
  box-shadow: 0 18px 42px oklch(38% 0.035 55 / 0.08);
}}

.d-section,
.wt-section {{
  margin-bottom: 0;
  padding: clamp(28px, 4vw, 48px) 0;
}}

.bp-list,
.rt-list {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-4);
}}

.bp-row,
.rt-row {{
  min-height: 148px;
  padding: var(--space-6);
  border: 1px solid var(--rule);
  border-radius: 8px;
  background:
    linear-gradient(180deg, var(--paper), oklch(95.5% 0.010 74));
  transition: transform 220ms var(--ease-out-quint), border-color 220ms var(--ease-out-quint);
}}

.bp-row:hover,
.rt-row:hover,
.dim-card:hover {{
  transform: translateY(-3px);
  border-color: oklch(56% 0.135 38 / 0.30);
}}

.bp-row:last-child,
.rt-row:last-child {{
  border-bottom: 1px solid var(--rule);
}}

.bp-count,
.rt-skill-tag,
.vc-value,
.bal-value,
.cad-value,
.sa-value,
.ce-value,
.kg-value {{
  color: var(--accent-deep);
}}

.dims-grid,
.mc-grid {{
  gap: var(--space-4);
  background: transparent;
}}

.dim-card,
.mc {{
  border: 1px solid var(--rule);
  border-radius: 8px;
  background:
    linear-gradient(180deg, var(--paper), oklch(95.5% 0.010 74));
  transition: transform 220ms var(--ease-out-quint), border-color 220ms var(--ease-out-quint);
}}

.dim-card__ring-fill {{
  animation: ring-draw 900ms var(--ease-out-expo) both;
}}

.bal-row,
.cad-row,
.vc-row,
.sa-row,
.ce-row,
.kg-row,
.tal-row {{
  background:
    linear-gradient(90deg, oklch(56% 0.135 38 / 0.06), transparent 42%);
  padding-inline: var(--space-4);
  border-color: var(--rule);
}}

.c-section .c-amount {{
  padding: var(--space-6);
  border: 1px solid var(--rule);
  border-radius: 8px;
  background:
    linear-gradient(135deg, var(--accent-soft), transparent 48%),
    var(--paper);
}}

.w-task {{
  grid-template-columns: 74px 1fr;
  padding: var(--space-6);
  border: 1px solid var(--rule);
  border-radius: 8px;
  background:
    linear-gradient(90deg, var(--paper), oklch(95.5% 0.010 74));
  margin-bottom: var(--space-4);
  transition: transform 220ms var(--ease-out-quint), border-color 220ms var(--ease-out-quint);
}}

.w-task:hover {{
  transform: translateY(-3px);
  border-color: oklch(38% 0.070 232 / 0.25);
}}

.w-task:last-child {{
  border-bottom: 1px solid var(--rule);
}}

.w-num {{
  color: var(--counter);
}}

section {{
  scroll-margin-top: var(--space-8);
}}

.colophon {{
  margin-top: var(--space-10);
  animation: dossier-rise 720ms var(--ease-out-quint) both;
}}

@media (max-width: 980px) {{
  .page {{
    width: min(100%, 820px);
  }}
  .masthead {{
    align-items: start;
    flex-wrap: wrap;
  }}
  .masthead-meta {{
    text-align: left;
  }}
  .cb-section,
  .t-hero,
  .coaching {{
    grid-template-columns: 1fr;
  }}
  .t-eyebrow,
  .t-label,
  .t-rule,
  .t-lede,
  .vitals,
  .coaching .m-section,
  .coaching .pattern-section,
  .coaching .f-section,
  .coaching .n-section {{
    grid-column: 1;
    grid-row: auto;
  }}
  .vitals {{
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }}
  .data-block {{
    grid-template-columns: 1fr;
  }}
  .data-block > section,
  .c-section,
  .w-section,
  .bal-section,
  .cad-section,
  .vc-section,
  .sa-section,
  .ce-section,
  .kg-section,
  .tal-section,
  .rce-section {{
    grid-column: 1;
  }}
}}

@media (max-width: 640px) {{
  .page {{
    padding-inline: var(--space-5);
  }}
  .t-hero,
  .cb-section,
  .coaching .m-section,
  .coaching .pattern-section,
  .coaching .f-section,
  .coaching .n-section,
  .data-block > section:not(.d-section):not(.wt-section) {{
    border-radius: 6px;
  }}
  .t-label {{
    font-size: clamp(56px, 19vw, 84px);
  }}
  .vitals,
  .bp-list,
  .rt-list {{
    grid-template-columns: 1fr;
  }}
  .w-task {{
    grid-template-columns: 52px 1fr;
  }}
  .w-num {{
    font-size: 40px;
  }}
  .cb-fields {{
    grid-template-columns: 1fr;
  }}
}}

@media (prefers-reduced-motion: reduce) {{
  *,
  *::before,
  *::after {{
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }}
}}
</style>
</head>
<body>
<main class="page">
  <header class="masthead">
    <div class="masthead-brand">
      <span class="masthead-kicker">Praxis</span>
      <span class="masthead-title">Weekly coaching dossier</span>
    </div>
    <span class="masthead-week">{week}</span>
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
