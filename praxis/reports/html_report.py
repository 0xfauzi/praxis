"""Praxis HTML report.

Strict adherence to the Praxis design system:
- Terracotta (#C1573B) for emphasis, NEVER bold
- Cream background (#FAF7F2)
- Libre Baskerville serif for editorial warmth
- Ink scale for text (no pure black)
- Generous whitespace
- Numbers always with full context (no naked statistics)
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from praxis.behavior import TrajectoryAssessment, TrajectoryLabel
from praxis.models_advisor import ModelUsageProfile
from praxis.orchestrator import RunSummary
from praxis.reports.baseline_panel import format_last_week_annotation_html
from praxis.reports.cost_ledger_panel import (
    COST_DISCLAIMER_HTML_CLASS,
    format_cost_disclaimer_html,
)
from praxis.scoring.rubric import RUBRIC, by_key


def _grade_label(score: float) -> str:
    if score >= 8.5:
        return "Practitioner"
    if score >= 7.0:
        return "Proficient"
    if score >= 5.5:
        return "Developing"
    if score >= 4.0:
        return "Foundational"
    return "Getting Started"


def _grade_blurb(score: float) -> str:
    if score >= 8.5:
        return "You're using AI the way the strongest practitioners do. Plans first, evidence over claims, iteration as a habit."
    if score >= 7.0:
        return "Solid working practice. With sharper planning and verification, you'd be in the top tier."
    if score >= 5.5:
        return "You're getting real value from AI tools. The biggest unlocks are in planning and iteration."
    if score >= 4.0:
        return "Treating AI as a search box, not a thinking partner. Two habit changes would lift this materially."
    return "Early days. The good news: the highest-leverage changes are also the simplest."


def _dimension_bar_html(
    key: str,
    score: float,
    last_week_value: float | None = None,
) -> str:
    dim = by_key(key)
    pct = max(0.0, min(100.0, score * 10.0))
    # Truncate the evidence to keep the row tidy; only append the ellipsis when
    # we actually truncated, so short evidence doesn't get a misleading "...".
    if len(dim.evidence) > 140:
        evidence_text = dim.evidence[:140].rstrip() + "..."
    else:
        evidence_text = dim.evidence
    # Faded last-week secondary annotation (spec section 8.1). The
    # formatter returns "" when last_week_value is None so this span
    # simply disappears from the row when the precondition is not met.
    last_week_html = format_last_week_annotation_html(last_week_value)
    return f"""
    <div class="dim-row">
      <div class="dim-head">
        <div class="dim-title">{html.escape(dim.title)}</div>
        <div class="dim-score">{score:.1f}<span class="dim-of">/10</span></div>
      </div>
      <div class="dim-bar-track">
        <div class="dim-bar-fill" style="width: {pct:.1f}%"></div>
      </div>
      <div class="dim-meta">
        <span class="dim-weight">Weight {int(dim.weight * 100)}%</span>
        <span class="dim-evidence">{html.escape(evidence_text)}</span>
        {last_week_html}
      </div>
    </div>
    """


def _focus_area_html(area: dict) -> str:
    drills_html = "".join(
        f'<li>{html.escape(str(drill))}</li>' for drill in area.get("drills", [])
    )
    current = float(area.get("current_score", 0))
    target = float(area.get("target_score", 0))
    return f"""
    <article class="focus-card">
      <div class="focus-head">
        <span class="eyebrow">Focus area</span>
        <h3>{html.escape(area.get("dimension_title", ""))}</h3>
      </div>
      <div class="focus-track">
        <div class="focus-from">Now <em>{current:.1f}</em></div>
        <div class="focus-arrow">&rarr;</div>
        <div class="focus-to">Target <em>{target:.1f}</em></div>
      </div>
      <p class="focus-why">{html.escape(area.get("why_it_matters", ""))}</p>
      <div class="drills-label">Drills for this week</div>
      <ol class="drills">{drills_html}</ol>
    </article>
    """


def _provider_breakdown_html(breakdown: dict[str, int]) -> str:
    if not breakdown:
        return ""
    chips = "".join(
        f'<span class="provider-chip">{html.escape(k.title())} <em>{v}</em></span>'
        for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1])
    )
    return f'<div class="provider-row">{chips}</div>'


_TRAJECTORY_COPY: dict[TrajectoryLabel, tuple[str, str]] = {
    TrajectoryLabel.LEARNING: (
        "Learning",
        "Your engagement is rising and your delegation is falling. This is the pattern Shen & Tamkin (2026) associate with skill formation.",
    ),
    TrajectoryLabel.STABLE_ENGAGED: (
        "Engaged",
        "You consistently ask why, check your understanding, and stay in the loop. Your skill is forming, not eroding.",
    ),
    TrajectoryLabel.STABLE_PASSIVE: (
        "Passive",
        "You use AI productively but with limited engagement. Speed today, capability gap tomorrow.",
    ),
    TrajectoryLabel.ATROPHYING: (
        "Atrophying",
        "Delegation is rising and engagement is falling. This is the at-risk pattern in the skill-formation literature.",
    ),
    TrajectoryLabel.INSUFFICIENT_DATA: (
        "Reading",
        "Not enough sessions yet to call a trajectory. Keep going.",
    ),
}


def _trajectory_html(trajectory: TrajectoryAssessment | None) -> str:
    if trajectory is None:
        return ""
    label_text, label_blurb = _TRAJECTORY_COPY.get(
        trajectory.label, ("—", "")
    )
    evidence_items = "".join(
        f"<li>{html.escape(e)}</li>" for e in trajectory.evidence[:4]
    )
    risk_items = "".join(
        f"<li>{html.escape(r)}</li>" for r in trajectory.risks[:3]
    )
    intervention_items = "".join(
        f"<li>{html.escape(i)}</li>" for i in trajectory.interventions[:4]
    )

    eng_arrow = "↑" if trajectory.engagement_slope > 0 else ("↓" if trajectory.engagement_slope < 0 else "→")
    del_arrow = "↑" if trajectory.delegation_slope > 0 else ("↓" if trajectory.delegation_slope < 0 else "→")

    label_class = f"traj-label traj-{trajectory.label.value}"

    return f"""
  <div class="section-head">
    <span class="section-num">04</span>
    <h2 class="section-title">Your learning trajectory</h2>
  </div>

  <div class="traj-card">
    <div class="traj-top">
      <div>
        <div class="eyebrow">Behavioral pattern</div>
        <div class="{label_class}">{html.escape(label_text)}</div>
      </div>
      <div class="traj-slopes">
        <div class="traj-slope">
          <div class="traj-slope-label">Engagement</div>
          <div class="traj-slope-value">{eng_arrow} {trajectory.engagement_slope:+.3f}</div>
        </div>
        <div class="traj-slope">
          <div class="traj-slope-label">Delegation</div>
          <div class="traj-slope-value">{del_arrow} {trajectory.delegation_slope:+.3f}</div>
        </div>
      </div>
    </div>
    <p class="traj-headline">{html.escape(trajectory.headline or label_blurb)}</p>

    {'<div class="traj-grid">' if (evidence_items or risk_items or intervention_items) else ''}
    {f'<div><div class="traj-sub-label">Evidence</div><ul>{evidence_items}</ul></div>' if evidence_items else ''}
    {f'<div><div class="traj-sub-label">What this risks</div><ul>{risk_items}</ul></div>' if risk_items else ''}
    {f'<div><div class="traj-sub-label">What to do differently</div><ul>{intervention_items}</ul></div>' if intervention_items else ''}
    {'</div>' if (evidence_items or risk_items or intervention_items) else ''}

    <div class="traj-citation">
      Reference: Shen &amp; Tamkin (2026), <em>How AI Impacts Skill Formation</em>, arXiv 2601.20245. Anthropic study of 81,000 users (2026) found 16.3% of all respondents — and 24% of teachers — worried about cognitive atrophy from AI use.
    </div>
  </div>
"""


_FIT_COPY: dict[str, tuple[str, str]] = {
    "well-matched": ("Well matched", "var(--success)"),
    "over-using": ("Over-using", "var(--warning)"),
    "under-using": ("Under-using", "var(--warning)"),
    "mixed": ("Mixed fit", "var(--info)"),
    "unknown": ("Unmapped", "var(--ink-300)"),
}


def _model_profile_html(profile: ModelUsageProfile) -> str:
    fit_label, _color = _FIT_COPY.get(profile.fit_assessment, ("—", ""))
    fit_class = f"fit-{profile.fit_assessment.replace('-', '_')}"

    display = profile.card.display_name if profile.card else profile.model_hint
    vendor = profile.card.vendor if profile.card else "Unknown vendor"
    tier = profile.card.tier if profile.card else "—"

    tasks_html = "".join(
        f'<span class="task-chip">{html.escape(t)}</span>'
        for t in profile.common_tasks[:5]
    )

    advice_html = "".join(
        f"<li>{html.escape(a)}</li>" for a in profile.advice[:5]
    )

    card_strengths = ""
    if profile.card and profile.card.best_for:
        items = "".join(
            f"<li>{html.escape(b)}</li>" for b in profile.card.best_for[:3]
        )
        card_strengths = f'<div class="model-block"><div class="traj-sub-label">Best for</div><ul>{items}</ul></div>'

    card_avoid = ""
    if profile.card and profile.card.avoid_for:
        items = "".join(
            f"<li>{html.escape(b)}</li>" for b in profile.card.avoid_for[:3]
        )
        card_avoid = f'<div class="model-block"><div class="traj-sub-label">Avoid for</div><ul>{items}</ul></div>'

    overall_html = (
        f'<div class="model-stat"><span class="stat-label">Your avg score</span>'
        f'<span class="stat-val">{profile.avg_overall_score:.1f}</span></div>'
        if profile.avg_overall_score is not None else ""
    )

    cost_html = ""
    if profile.estimated_cost_usd is not None and profile.estimated_cost_usd > 0:
        per_session = profile.estimated_cost_per_session_usd or 0.0
        cost_html = (
            f'<div class="model-stat"><span class="stat-label">Est. cost</span>'
            f'<span class="stat-val">${profile.estimated_cost_usd:.2f}'
            f'<span class="stat-unit">total</span></span>'
            f'<span class="stat-sublabel">~${per_session:.3f}/session</span></div>'
        )

    pricing_html = ""
    if profile.card and profile.card.input_per_million_usd is not None:
        verified = profile.card.pricing_last_verified or "unverified"
        source = profile.card.pricing_source or ""
        source_link = (
            f' &middot; <a href="{html.escape(source)}" target="_blank" rel="noopener">verify</a>'
            if source else ""
        )
        notes_html = (
            f'<div class="pricing-notes">{html.escape(profile.card.pricing_notes)}</div>'
            if profile.card.pricing_notes else ""
        )
        pricing_html = f"""
      <div class="pricing-block">
        <div class="traj-sub-label">Pricing</div>
        <div class="pricing-row">
          <span class="pricing-num">${profile.card.input_per_million_usd:g}</span>
          <span class="pricing-unit">per million input tokens</span>
        </div>
        <div class="pricing-row">
          <span class="pricing-num">${profile.card.output_per_million_usd:g}</span>
          <span class="pricing-unit">per million output tokens</span>
        </div>
        <div class="pricing-meta">verified {html.escape(verified)}{source_link}</div>
        {notes_html}
      </div>"""

    return f"""
    <article class="model-card">
      <div class="model-head">
        <div>
          <div class="eyebrow">{html.escape(vendor)} · {html.escape(tier)}</div>
          <h3>{html.escape(display)}</h3>
        </div>
        <div class="model-fit {fit_class}">{html.escape(fit_label)}</div>
      </div>

      <div class="model-stats">
        <div class="model-stat">
          <span class="stat-label">Sessions</span>
          <span class="stat-val">{profile.session_count}</span>
        </div>
        <div class="model-stat">
          <span class="stat-label">Avg prompt</span>
          <span class="stat-val">{int(profile.avg_prompt_chars)}<span class="stat-unit">chars</span></span>
        </div>
        <div class="model-stat">
          <span class="stat-label">Engagement</span>
          <span class="stat-val">{profile.avg_engagement_rate:.2f}</span>
        </div>
        {overall_html}
        {cost_html}
      </div>

      {f'<div class="model-tasks"><div class="traj-sub-label">You use it for</div>{tasks_html}</div>' if tasks_html else ''}

      <div class="model-advice">
        <div class="drills-label">Advice for this model</div>
        <ul>{advice_html}</ul>
      </div>

      {pricing_html}
      {card_strengths}
      {card_avoid}
    </article>
    """


def _models_section_html(model_profiles: list[ModelUsageProfile] | None) -> str:
    if not model_profiles:
        return ""
    cards_html = "".join(_model_profile_html(p) for p in model_profiles)

    total_cost = sum(
        p.estimated_cost_usd or 0.0 for p in model_profiles if p.estimated_cost_usd
    )
    priced_count = sum(1 for p in model_profiles if p.estimated_cost_usd)
    unpriced_count = sum(
        1 for p in model_profiles
        if p.card is not None and p.card.input_per_million_usd is None
    )

    cost_summary_html = ""
    if total_cost > 0:
        caveat_bits = [
            "rough estimate assuming ~4 chars per token, output ≈ 1.5× input, no caching or batch discounts"
        ]
        if unpriced_count:
            caveat_bits.append(
                f"{unpriced_count} subscription-only model{'s' if unpriced_count != 1 else ''} excluded"
            )
        caveat = "; ".join(caveat_bits)
        cost_summary_html = f"""
  <div class="cost-summary">
    <div class="eyebrow">Estimated spend in this window</div>
    <div style="margin-top: 12px;">
      <span class="cost-amount">~${total_cost:.2f}</span>
      <span style="color: var(--ink-500);">across {priced_count} priced model{'s' if priced_count != 1 else ''}</span>
    </div>
    <span class="cost-note">{html.escape(caveat)}. Verify vendor pricing pages before using for budget planning.</span>
  </div>"""

    return f"""
  <div class="section-head">
    <span class="section-num">05</span>
    <h2 class="section-title">Your models — one read per tool</h2>
  </div>
  <p class="muted-intro">You're using {len(model_profiles)} model{'s' if len(model_profiles) != 1 else ''}. Each one wants different things. Here's how you're doing per tool, what to change, and what it costs.</p>
  {cost_summary_html}
  <div class="models-grid">{cards_html}</div>
"""


def render(summary: RunSummary) -> str:
    snapshot = summary.snapshot
    coaching = summary.coaching
    score = snapshot.overall

    last_week_means = summary.last_week_means or {}
    dim_rows = "".join(
        _dimension_bar_html(
            d.key,
            snapshot.dimension_means.get(d.key, 0.0),
            last_week_value=last_week_means.get(d.key),
        )
        for d in RUBRIC
    )

    focus_html = "".join(_focus_area_html(a) for a in coaching.focus_areas)

    standouts = "".join(
        f"<li>{html.escape(s)}</li>" for s in snapshot.standout_moments[:3]
    ) or "<li class='muted'>Run more sessions to surface patterns.</li>"
    failures = "".join(
        f"<li>{html.escape(f)}</li>" for f in snapshot.failure_modes[:3]
    ) or "<li class='muted'>Run more sessions to surface patterns.</li>"

    provider_html = _provider_breakdown_html(snapshot.provider_breakdown)
    trajectory_html = _trajectory_html(summary.trajectory)
    models_html = _models_section_html(summary.model_profiles)

    # Spec §9.6 (US-031): when the rolling 4-week telemetry tripped the
    # high>90% threshold, the orchestrator sets calibration_notice; surface
    # it as a small banner so the reader knows the pass-1 prompt was
    # auto-tuned this run. Hidden when notice is None.
    calibration_html = (
        f'<div class="calibration-note">{html.escape(summary.calibration_notice)}</div>'
        if summary.calibration_notice else ""
    )

    generated_at = datetime.now(timezone.utc).strftime("%d %B %Y")
    consolidated_note = (
        f"Consolidated today · {summary.sessions_scored} new sessions analyzed"
        if summary.consolidated_for is not None
        else "Cached consolidation · next refresh tomorrow"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Praxis</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Libre+Baskerville:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
:root {{
  --primary: #C1573B;
  --primary-hover: #A84832;
  --primary-soft: rgba(193, 87, 59, 0.08);
  --primary-line: rgba(193, 87, 59, 0.15);
  --cream-100: #FAF7F2;
  --cream-300: #F0EBE3;
  --cream-400: #E8E3DB;
  --ink-900: #1a1a1a;
  --ink-700: #404040;
  --ink-500: #6b6b6b;
  --ink-300: #a3a3a3;
  --ink-100: #e5e5e5;
  --border-light: rgba(26, 26, 26, 0.08);
  --border-medium: rgba(26, 26, 26, 0.15);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{
  background: var(--cream-100);
  color: var(--ink-900);
  font-family: Georgia, 'Libre Baskerville', serif;
  font-size: 16px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
.page {{ max-width: 880px; margin: 0 auto; padding: 80px 32px 120px; }}

/* Masthead */
.masthead {{ border-bottom: 1px solid var(--border-light); padding-bottom: 32px; margin-bottom: 64px; }}
.eyebrow {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--primary);
  font-weight: 500;
}}
.masthead-title {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 44px;
  line-height: 1.15;
  margin-top: 16px;
  letter-spacing: -0.01em;
}}
.masthead-title em {{ color: var(--primary); font-style: normal; }}
.masthead-meta {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: var(--ink-500);
  margin-top: 24px;
  display: flex; gap: 16px; flex-wrap: wrap;
}}
.masthead-meta span {{ position: relative; padding-right: 16px; }}
.masthead-meta span:not(:last-child)::after {{
  content: ""; position: absolute; right: 0; top: 50%;
  width: 3px; height: 3px; background: var(--ink-300); border-radius: 50%;
  transform: translateY(-50%);
}}

/* Hero score */
.hero-score {{
  display: grid; grid-template-columns: auto 1fr; gap: 48px;
  align-items: center;
  padding: 56px 48px;
  background: var(--cream-300);
  border: 1px solid var(--border-light);
  margin-bottom: 64px;
}}
.hero-number {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 96px;
  line-height: 1;
  color: var(--primary);
  font-weight: 400;
  letter-spacing: -0.02em;
}}
.hero-number .denom {{
  color: var(--ink-300);
  font-size: 36px;
  margin-left: 4px;
}}
.hero-grade {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 28px;
  margin-bottom: 12px;
}}
.hero-blurb {{ font-size: 17px; color: var(--ink-700); max-width: 460px; }}

/* Provider chips */
.provider-row {{ display: flex; gap: 12px; margin-bottom: 56px; flex-wrap: wrap; }}
.provider-chip {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 8px 16px;
  border: 1px solid var(--border-medium);
  color: var(--ink-700);
  background: var(--cream-100);
}}
.provider-chip em {{
  font-style: normal;
  color: var(--primary);
  margin-left: 6px;
}}

/* Section heads */
.section-head {{ margin: 80px 0 32px; }}
.section-num {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-style: italic;
  font-size: 14px;
  color: var(--primary);
  margin-right: 16px;
}}
.section-title {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 28px;
  display: inline;
}}

/* Dimension bars */
.dim-row {{ padding: 28px 0; border-bottom: 1px solid var(--border-light); }}
.dim-row:last-child {{ border-bottom: none; }}
.dim-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }}
.dim-title {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 18px;
}}
.dim-score {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 22px;
  color: var(--primary);
}}
.dim-of {{ color: var(--ink-300); font-size: 14px; }}
.dim-bar-track {{
  height: 6px;
  background: var(--cream-400);
  position: relative;
  overflow: hidden;
}}
.dim-bar-fill {{
  height: 100%;
  background: var(--primary);
  transition: width 800ms cubic-bezier(0.4, 0, 0.2, 1);
}}
.dim-meta {{
  margin-top: 10px;
  font-size: 12px;
  color: var(--ink-500);
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  display: flex; gap: 16px; align-items: baseline;
}}
.dim-weight {{ color: var(--primary); letter-spacing: 0.05em; flex-shrink: 0; }}
.dim-evidence {{ font-style: italic; }}
.dim-last-week {{
  color: var(--ink-300);
  letter-spacing: 0.05em;
  flex-shrink: 0;
  font-style: italic;
}}

/* Focus cards */
.focus-grid {{ display: grid; gap: 32px; }}
@media (min-width: 720px) {{
  .focus-grid {{ grid-template-columns: 1fr 1fr; }}
}}
.focus-card {{
  background: var(--cream-100);
  border: 1px solid var(--border-light);
  padding: 32px;
}}
.focus-head h3 {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 22px;
  margin-top: 8px;
  margin-bottom: 24px;
}}
.focus-track {{
  display: flex; align-items: center; gap: 16px;
  padding: 16px;
  background: var(--cream-300);
  margin-bottom: 20px;
}}
.focus-from, .focus-to {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-500);
}}
.focus-from em, .focus-to em {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-style: normal;
  font-size: 22px;
  color: var(--primary);
  margin-left: 8px;
}}
.focus-arrow {{ color: var(--primary); font-size: 20px; }}
.focus-why {{ color: var(--ink-700); margin-bottom: 24px; font-size: 15px; }}
.drills-label {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--primary);
  margin-bottom: 12px;
}}
.drills {{ padding-left: 20px; }}
.drills li {{
  margin-bottom: 12px;
  color: var(--ink-700);
  font-size: 15px;
}}
.drills li::marker {{ color: var(--primary); }}

/* Daily practice callout */
.practice-callout {{
  margin: 64px 0;
  padding: 48px;
  background: var(--primary);
  color: var(--cream-100);
}}
.practice-callout .eyebrow {{
  color: rgba(250, 247, 242, 0.7);
}}
.practice-callout p {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-style: italic;
  font-size: 24px;
  line-height: 1.4;
  margin-top: 16px;
  max-width: 640px;
}}

/* Patterns columns */
.patterns {{ display: grid; gap: 32px; margin-top: 24px; }}
@media (min-width: 720px) {{ .patterns {{ grid-template-columns: 1fr 1fr; }} }}
.patterns h4 {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 18px;
  margin-bottom: 16px;
  font-weight: 400;
}}
.patterns h4 em {{ color: var(--primary); font-style: normal; }}
.patterns ul {{ list-style: none; padding: 0; }}
.patterns li {{
  padding: 16px 0;
  border-top: 1px solid var(--border-light);
  font-size: 15px;
  color: var(--ink-700);
}}
.patterns li.muted {{ color: var(--ink-300); font-style: italic; }}

/* Headline */
.headline {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-style: italic;
  font-size: 22px;
  line-height: 1.5;
  color: var(--ink-700);
  padding: 32px 0;
  border-top: 1px solid var(--border-light);
  border-bottom: 1px solid var(--border-light);
  margin: 32px 0;
}}

/* Footer */
.colophon {{
  margin-top: 96px;
  padding-top: 32px;
  border-top: 1px solid var(--border-light);
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 11px;
  color: var(--ink-500);
  letter-spacing: 0.05em;
}}
.colophon strong {{ color: var(--primary); font-weight: 500; }}
.colophon a {{ color: var(--primary); text-decoration: none; }}
/* Spec 10.2: the disclaimer reads as a footer caveat, not a headline. */
.{COST_DISCLAIMER_HTML_CLASS} {{
  color: var(--ink-300);
  font-style: italic;
  letter-spacing: 0.05em;
}}

/* Semantic colors for fit and trajectory */
.muted-intro {{ color: var(--ink-500); font-size: 15px; margin-bottom: 32px; max-width: 640px; }}

/* Trajectory */
.traj-card {{
  background: var(--cream-100);
  border: 1px solid var(--border-light);
  padding: 40px;
  margin-bottom: 32px;
}}
.traj-top {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 32px;
  margin-bottom: 24px;
  padding-bottom: 24px;
  border-bottom: 1px solid var(--border-light);
}}
.traj-label {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 36px;
  line-height: 1.1;
  margin-top: 12px;
  letter-spacing: -0.01em;
}}
.traj-learning {{ color: #2D7D46; }}
.traj-stable_engaged {{ color: var(--primary); }}
.traj-stable_passive {{ color: #B8860B; }}
.traj-atrophying {{ color: #8F3D2A; }}
.traj-insufficient_data {{ color: var(--ink-500); }}

.traj-slopes {{ display: flex; gap: 32px; }}
.traj-slope {{ text-align: right; }}
.traj-slope-label {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--ink-500);
}}
.traj-slope-value {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 18px;
  color: var(--primary);
  margin-top: 4px;
}}

.traj-headline {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-style: italic;
  font-size: 19px;
  line-height: 1.5;
  color: var(--ink-700);
  margin-bottom: 24px;
  max-width: 720px;
}}

.traj-grid {{
  display: grid;
  gap: 32px;
  margin-bottom: 24px;
}}
@media (min-width: 720px) {{
  .traj-grid {{ grid-template-columns: 1fr 1fr 1fr; }}
}}

.traj-sub-label {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--primary);
  margin-bottom: 12px;
}}
.traj-grid ul, .model-block ul {{
  list-style: none;
  padding: 0;
}}
.traj-grid li, .model-block li {{
  padding: 10px 0;
  border-top: 1px solid var(--border-light);
  font-size: 14px;
  color: var(--ink-700);
  line-height: 1.5;
}}
.traj-citation {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 11px;
  color: var(--ink-500);
  padding-top: 24px;
  margin-top: 24px;
  border-top: 1px solid var(--border-light);
  line-height: 1.5;
}}
.traj-citation em {{ color: var(--primary); font-style: italic; }}

/* Model cards */
.models-grid {{
  display: grid;
  gap: 32px;
}}
@media (min-width: 720px) {{
  .models-grid {{ grid-template-columns: 1fr 1fr; }}
}}
.model-card {{
  background: var(--cream-100);
  border: 1px solid var(--border-light);
  padding: 32px;
}}
.model-head {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 24px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border-light);
}}
.model-head h3 {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 22px;
  margin-top: 8px;
}}
.model-fit {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 10px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  padding: 6px 12px;
  border: 1px solid currentColor;
  white-space: nowrap;
  flex-shrink: 0;
  margin-top: 8px;
}}
.fit-well_matched {{ color: #2D7D46; }}
.fit-over_using {{ color: #B8860B; }}
.fit-under_using {{ color: #B8860B; }}
.fit-mixed {{ color: #4A6FA5; }}
.fit-unknown {{ color: var(--ink-500); }}

.model-stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
  padding: 20px;
  background: var(--cream-300);
}}
.model-stat {{ display: flex; flex-direction: column; gap: 4px; }}
.stat-label {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 10px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--ink-500);
}}
.stat-val {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 24px;
  color: var(--primary);
}}
.stat-unit {{ font-size: 11px; color: var(--ink-500); margin-left: 4px; }}

.model-tasks {{ margin-bottom: 24px; }}
.task-chip {{
  display: inline-block;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 11px;
  padding: 6px 12px;
  margin: 4px 8px 4px 0;
  border: 1px solid var(--border-medium);
  color: var(--ink-700);
  letter-spacing: 0.03em;
}}

.model-advice {{ margin-bottom: 20px; }}
.model-advice ul {{
  list-style: none;
  padding: 0;
}}
.model-advice li {{
  padding: 12px 0;
  border-top: 1px solid var(--border-light);
  font-size: 14px;
  color: var(--ink-700);
  line-height: 1.5;
  position: relative;
  padding-left: 20px;
}}
.model-advice li::before {{
  content: "·";
  color: var(--primary);
  position: absolute;
  left: 0;
  font-weight: bold;
  font-size: 20px;
  line-height: 1;
  top: 14px;
}}

.model-block {{ margin-top: 20px; }}

/* Pricing block within model cards */
.stat-sublabel {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 10px;
  color: var(--ink-500);
  margin-top: 2px;
}}
.pricing-block {{
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--border-light);
}}
.pricing-row {{
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 6px 0;
}}
.pricing-num {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 20px;
  color: var(--primary);
  min-width: 80px;
}}
.pricing-unit {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 12px;
  color: var(--ink-500);
}}
.pricing-meta {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 10px;
  letter-spacing: 0.05em;
  color: var(--ink-500);
  margin-top: 8px;
}}
.pricing-meta a {{
  color: var(--primary);
  text-decoration: none;
  border-bottom: 1px solid var(--primary-line);
}}
.pricing-notes {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 12px;
  color: var(--ink-700);
  font-style: italic;
  margin-top: 10px;
  line-height: 1.5;
}}

/* Calibration auto-tune banner (US-031): only rendered when the rolling
   4-week pass-1 telemetry tripped the high>90% threshold and the prompt
   was sharpened for this run. */
.calibration-note {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--primary);
  padding: 12px 16px;
  margin-bottom: 32px;
  border-left: 3px solid var(--primary);
  background: var(--primary-soft);
}}

/* Cost summary callout (above per-model grid) */
.cost-summary {{
  margin-top: 16px;
  margin-bottom: 32px;
  padding: 20px 24px;
  background: var(--cream-300);
  border-left: 3px solid var(--primary);
}}
.cost-summary .cost-amount {{
  font-family: 'Libre Baskerville', Georgia, serif;
  font-size: 28px;
  color: var(--primary);
  margin-right: 12px;
}}
.cost-summary .cost-note {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 12px;
  color: var(--ink-500);
  display: block;
  margin-top: 6px;
  line-height: 1.5;
}}
</style>
</head>
<body>
<main class="page">

  <header class="masthead">
    <div class="eyebrow">Praxis &middot; A reading of your AI practice</div>
    <h1 class="masthead-title">Your AI practice, <em>scored against the evidence.</em></h1>
    <div class="masthead-meta">
      <span>{html.escape(generated_at)}</span>
      <span>{snapshot.session_count} sessions in window</span>
      <span>{html.escape(consolidated_note)}</span>
    </div>
  </header>

  <section class="hero-score">
    <div class="hero-number">{score:.1f}<span class="denom">/10</span></div>
    <div>
      <div class="hero-grade">{html.escape(_grade_label(score))}</div>
      <div class="hero-blurb">{html.escape(_grade_blurb(score))}</div>
    </div>
  </section>

  {provider_html}

  {calibration_html}

  <div class="headline">{html.escape(coaching.headline)}</div>

  <div class="section-head">
    <span class="section-num">01</span>
    <h2 class="section-title">The six dimensions</h2>
  </div>
  <div class="dimensions">{dim_rows}</div>

  <div class="section-head">
    <span class="section-num">02</span>
    <h2 class="section-title">Where to put your attention this week</h2>
  </div>
  <div class="focus-grid">{focus_html}</div>

  <div class="practice-callout">
    <div class="eyebrow">Daily practice</div>
    <p>&ldquo;{html.escape(coaching.daily_practice)}&rdquo;</p>
  </div>

  <div class="section-head">
    <span class="section-num">03</span>
    <h2 class="section-title">Patterns from your recent sessions</h2>
  </div>
  <div class="patterns">
    <div>
      <h4>What you're <em>doing well</em></h4>
      <ul>{standouts}</ul>
    </div>
    <div>
      <h4>What's <em>holding you back</em></h4>
      <ul>{failures}</ul>
    </div>
  </div>

  {trajectory_html}

  {models_html}

  <footer class="colophon">
    <p>Generated by <strong>Praxis</strong>. Scores blend heuristic features with LLM-as-judge analysis on a sample of your sessions. Coaching is personalized from your weakest dimensions, grounded in research from Anthropic, OpenAI, Microsoft, Google, and academic work on AI productivity.</p>
    <p style="margin-top: 12px;">Behavioral trajectory grounded in Shen &amp; Tamkin (2026), <em>How AI Impacts Skill Formation</em>, and Anthropic's 2026 qualitative study of 81,000 users. Model-specific advice is generated from public model cards and your actual usage of each model.</p>
    <p style="margin-top: 12px;">Coaching generated by: {html.escape(coaching.generated_by)} &middot; All processing local. Your chat history never leaves your machine, except as a sampled transcript sent to the LLM judge you authorized.</p>
    <p style="margin-top: 12px;">Cost figures: {format_cost_disclaimer_html()}.</p>
  </footer>

</main>
</body>
</html>"""
