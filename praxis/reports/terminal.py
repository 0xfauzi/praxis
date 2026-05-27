"""Terminal renderer for `praxis scan` runs.

Pure ANSI, no dependencies. Designed to read as a serious editorial
document at 80 columns, matching the HTML report's voice.

Color usage is deliberately spare: terracotta for emphasis (score
numbers, section eyebrows, headlines), dim for secondary text, italic
for the coaching/trajectory voice. Nothing bold (no bold for emphasis — house rule).
"""
from __future__ import annotations

import textwrap

from praxis.behavior import TrajectoryLabel
from praxis.orchestrator import RunSummary
from praxis.reports.cost_ledger_panel import format_cost_disclaimer
from praxis.scoring.rubric import RUBRIC


# ANSI 256-color terracotta (166) is the closest practical match to
# the Praxis primary #C1573B. Greens/ambers/reds chosen to read on
# both dark and light terminals.
TERRA = "\033[38;5;166m"
INK = "\033[38;5;236m"
DIM = "\033[2m"
ITALIC = "\033[3m"
RESET = "\033[0m"

GREEN = "\033[38;5;71m"     # well-matched, learning
AMBER = "\033[38;5;172m"    # over/under, passive
DARK_RED = "\033[38;5;124m" # atrophying
BLUE = "\033[38;5;67m"      # mixed
MUTED = "\033[38;5;245m"    # unknown / insufficient data

CONTENT_WIDTH = 64
INDENT = "  "


# ---------------------------------------------------------------------- helpers


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


def _bar(score: float, width: int = 22) -> str:
    """Terracotta filled, dim empty. Score is 0-10."""
    score = max(0.0, min(10.0, score))
    filled = int(round((score / 10.0) * width))
    return f"{TERRA}{'█' * filled}{RESET}{DIM}{'░' * (width - filled)}{RESET}"


def _section(title: str) -> list[str]:
    """One-line section break: eyebrow text, then a dim rule."""
    pad = max(0, CONTENT_WIDTH - len(title) - 2)
    return [
        "",
        f"{INDENT}{TERRA}{title.upper()}{RESET}  {DIM}{'─' * pad}{RESET}",
        "",
    ]


def _wrap(text: str, width: int = CONTENT_WIDTH, indent: str = INDENT * 2) -> list[str]:
    """Indent-aware wrap that returns ready-to-join lines."""
    if not text:
        return []
    wrapped = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    return [f"{indent}{line}" for line in wrapped]


def _fit_marker(fit: str) -> tuple[str, str]:
    """Return (color, glyph) for a fit assessment."""
    if fit == "well-matched":
        return GREEN, "●"
    if fit in {"over-using", "under-using"}:
        return AMBER, "▲"
    if fit == "mixed":
        return BLUE, "◆"
    return MUTED, "○"


_TRAJ_COLOR: dict[TrajectoryLabel, str] = {
    TrajectoryLabel.LEARNING: GREEN,
    TrajectoryLabel.STABLE_ENGAGED: TERRA,
    TrajectoryLabel.STABLE_PASSIVE: AMBER,
    TrajectoryLabel.ATROPHYING: DARK_RED,
    TrajectoryLabel.INSUFFICIENT_DATA: MUTED,
}

_TRAJ_TITLE: dict[TrajectoryLabel, str] = {
    TrajectoryLabel.LEARNING: "Learning",
    TrajectoryLabel.STABLE_ENGAGED: "Engaged",
    TrajectoryLabel.STABLE_PASSIVE: "Passive",
    TrajectoryLabel.ATROPHYING: "Atrophying",
    TrajectoryLabel.INSUFFICIENT_DATA: "Reading",
}


# ---------------------------------------------------------------------- sections


def _masthead(summary: RunSummary) -> list[str]:
    snap = summary.snapshot
    grade = _grade_label(snap.overall)
    lines: list[str] = []
    lines.append("")
    lines.append(f"{INDENT}{TERRA}PRAXIS{RESET}   {DIM}A reading of your AI practice{RESET}")
    lines.append(f"{INDENT}{DIM}{'─' * CONTENT_WIDTH}{RESET}")
    lines.append("")
    # Hero: score · grade
    score_str = f"{TERRA}{snap.overall:.1f}{RESET}{DIM}/10{RESET}"
    lines.append(f"{INDENT}{score_str}   {grade}")
    # Meta line. Avoid nested DIM/RESET pairs; the line stays dim throughout.
    new = summary.sessions_new
    seen = summary.sessions_seen
    scored = summary.sessions_scored
    meta = f"{snap.session_count} sessions in window  ·  scanned {seen}, new {new}, scored {scored}"
    lines.append(f"{INDENT}{DIM}{meta}{RESET}")

    if snap.provider_breakdown:
        chips = f"  {DIM}·{RESET}  ".join(
            f"{TERRA}{k}{RESET} {v}"
            for k, v in sorted(snap.provider_breakdown.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"{INDENT}{chips}")
    # Spec §9.6 (US-031): when the rolling 4-week telemetry tripped the
    # high>90% threshold, surface a one-line banner so the user knows the
    # pass-1 prompt was auto-tuned on this run.
    if summary.calibration_notice:
        lines.append("")
        lines.append(f"{INDENT}{TERRA}note{RESET}  {DIM}{summary.calibration_notice}{RESET}")
    return lines


def _dimensions(summary: RunSummary) -> list[str]:
    lines: list[str] = []
    lines.extend(_section("The six dimensions"))
    for dim in RUBRIC:
        score = summary.snapshot.dimension_means.get(dim.key, 0.0)
        title = dim.title.ljust(30)
        lines.append(f"{INDENT}{INDENT}{title} {_bar(score)} {TERRA}{score:>4.1f}{RESET}")
    return lines


def _coaching(summary: RunSummary) -> list[str]:
    coaching = summary.coaching
    lines: list[str] = []
    if not coaching or (not coaching.headline and not coaching.focus_areas):
        return lines

    lines.extend(_section("Coaching"))
    if coaching.headline:
        lines.extend(
            [f"{INDENT}{INDENT}{ITALIC}{line}{RESET}"
             for line in textwrap.wrap(coaching.headline, width=CONTENT_WIDTH)]
        )
        lines.append("")

    if coaching.focus_areas:
        lines.append(f"{INDENT}{INDENT}{TERRA}Focus this week{RESET}")
        for i, area in enumerate(coaching.focus_areas[:2], start=1):
            title = area.get("dimension_title", "")
            current = float(area.get("current_score", 0))
            target = float(area.get("target_score", 0))
            track = f"{current:.1f}  {TERRA}→{RESET}  {target:.1f}"
            lines.append("")
            lines.append(f"{INDENT}{INDENT}{TERRA}{i}.{RESET}  {title}   {DIM}({track}){RESET}")
            for drill in area.get("drills", [])[:2]:
                # Wrap drills so long sentences read nicely in 80 cols
                drill_lines = textwrap.wrap(
                    drill,
                    width=CONTENT_WIDTH - 4,
                    initial_indent="",
                    subsequent_indent="    ",
                )
                if drill_lines:
                    lines.append(f"{INDENT}{INDENT}    {TERRA}·{RESET} {drill_lines[0]}")
                    for cont in drill_lines[1:]:
                        lines.append(f"{INDENT}{INDENT}    {cont}")

    if coaching.daily_practice:
        lines.append("")
        lines.append(f"{INDENT}{INDENT}{TERRA}Daily practice{RESET}")
        wrapped = textwrap.wrap(coaching.daily_practice, width=CONTENT_WIDTH - 4)
        for line in wrapped:
            lines.append(f"{INDENT}{INDENT}{ITALIC}{line}{RESET}")
    return lines


def _trajectory(summary: RunSummary) -> list[str]:
    traj = summary.trajectory
    if traj is None:
        return []
    lines: list[str] = []
    lines.extend(_section("Learning trajectory"))

    color = _TRAJ_COLOR.get(traj.label, MUTED)
    title = _TRAJ_TITLE.get(traj.label, traj.label.value.title())

    eng = traj.engagement_slope
    deleg = traj.delegation_slope
    eng_glyph = "↑" if eng > 0.001 else ("↓" if eng < -0.001 else "→")
    del_glyph = "↑" if deleg > 0.001 else ("↓" if deleg < -0.001 else "→")

    slopes = (
        f"{eng_glyph} {eng:+.3f} engagement"
        f"   {DIM}·{RESET}   "
        f"{del_glyph} {deleg:+.3f} delegation"
    )
    lines.append(f"{INDENT}{INDENT}{color}{title}{RESET}   {DIM}{slopes}{RESET}")

    if traj.headline:
        lines.append("")
        for line in textwrap.wrap(traj.headline, width=CONTENT_WIDTH - 2):
            lines.append(f"{INDENT}{INDENT}{ITALIC}{line}{RESET}")
    return lines


def _per_model(summary: RunSummary) -> list[str]:
    profiles = summary.model_profiles or []
    if not profiles:
        return []
    lines: list[str] = []
    lines.extend(_section("Per-model read"))

    total_known_cost = sum(
        p.estimated_cost_usd or 0.0 for p in profiles if p.estimated_cost_usd
    )
    if total_known_cost > 0:
        lines.append(
            f"{INDENT}{INDENT}{DIM}Window cost estimate across priced models: "
            f"~${total_known_cost:.2f}{RESET}"
        )
        lines.append("")

    for profile in profiles[:5]:
        display = profile.card.display_name if profile.card else profile.model_hint
        display_short = (display[:30] + "…") if len(display) > 31 else display
        fit = profile.fit_assessment or "unknown"
        color, glyph = _fit_marker(fit)
        sessions = profile.session_count

        # Name column padded to a stable width so glyphs line up
        name_col = display_short.ljust(32)
        fit_col = f"{color}{glyph}{RESET} {fit:<13}"
        meta = f"{sessions} sessions"
        if profile.avg_overall_score is not None:
            meta += f"  {DIM}·{RESET}  {TERRA}{profile.avg_overall_score:.1f}{RESET}/10"
        if profile.estimated_cost_usd is not None and profile.estimated_cost_usd > 0:
            meta += f"  {DIM}·{RESET}  ~${profile.estimated_cost_usd:.2f}"
        lines.append(f"{INDENT}{INDENT}{name_col} {fit_col} {DIM}{meta}{RESET}")

        # Pricing line directly under the model name (dim)
        if profile.card and profile.card.input_per_million_usd is not None:
            price = (
                f"${profile.card.input_per_million_usd:g}/M in · "
                f"${profile.card.output_per_million_usd:g}/M out"
            )
            lines.append(f"{INDENT}{INDENT}    {DIM}{price}{RESET}")

        # Top advice item, wrapped under the row
        if profile.advice:
            advice = profile.advice[0]
            for w in textwrap.wrap(advice, width=CONTENT_WIDTH - 4):
                lines.append(f"{INDENT}{INDENT}    {DIM}{w}{RESET}")
    return lines


def _footer(summary: RunSummary) -> list[str]:
    if summary.consolidated_for is not None:
        note = f"Daily consolidation: completed for {summary.consolidated_for}"
    else:
        note = "Daily consolidation: cached (already run today)"
    return [
        "",
        f"{INDENT}{DIM}{note}  ·  {summary.elapsed_seconds}s elapsed{RESET}",
        f"{INDENT}{DIM}{format_cost_disclaimer()}{RESET}",
        "",
    ]


# ----------------------------------------------------------------------- render


def render(summary: RunSummary) -> str:
    parts: list[str] = []
    parts.extend(_masthead(summary))
    parts.extend(_dimensions(summary))
    parts.extend(_coaching(summary))
    parts.extend(_trajectory(summary))
    parts.extend(_per_model(summary))
    parts.extend(_footer(summary))
    return "\n".join(parts)
