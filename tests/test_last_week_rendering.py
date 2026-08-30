"""Renderer-side tests for the last-week secondary annotation (US-036).

Acceptance criteria (spec section 8.1):
  - When 2+ weeks of data are available, last-week's mean is exposed to
    the renderer alongside the 90-day baseline (the orchestrator-side
    wiring is a separate later concern; here we verify the renderer
    contract: given a RunSummary with ``last_week_means`` populated,
    the HTML output emits the faded annotation; with ``None`` it does
    not).
  - HTML digest renders last-week as a faded secondary annotation
    (faded == styled with the ``dim-last-week`` CSS class, which the
    stylesheet colours with ``var(--ink-300)``).
  - Terminal digest omits the last-week annotation in all cases.

These tests build a minimal RunSummary by hand rather than going
through the full orchestrator pipeline, so they stay fast and pure.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from praxis.orchestrator import RunSummary
from praxis.reports import html_report, terminal
from praxis.reports.baseline_panel import LAST_WEEK_HTML_CLASS
from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.coach import Coaching
from praxis.scoring.rubric import RUBRIC


def _make_snapshot(dim_value: float = 6.0) -> ProfileSnapshot:
    return ProfileSnapshot(
        overall=6.0,
        dimension_means={d.key: dim_value for d in RUBRIC},
        session_count=12,
        provider_breakdown={"claude": 8, "codex": 4},
        strongest_dimension="planning",
        weakest_dimension="verification",
        standout_moments=["Asked for trade-offs."],
        failure_modes=["Took the first answer."],
    )


def _make_coaching() -> Coaching:
    return Coaching(
        headline="Solid working practice.",
        focus_areas=[],
        daily_practice="Restate the goal before any tool call.",
        generated_by="test",
    )


def _make_summary(
    last_week_means: dict[str, float] | None = None,
) -> RunSummary:
    return RunSummary(
        sessions_seen=12,
        sessions_new=2,
        sessions_scored=2,
        elapsed_seconds=0.0,
        snapshot=_make_snapshot(),
        coaching=_make_coaching(),
        consolidated_for=date(2026, 5, 27),
        last_week_means=last_week_means,
    )


# ---------------------------------------------------------------------- HTML


def test_html_renders_faded_last_week_annotation_when_provided():
    """HTML digest renders last-week as a faded secondary annotation."""
    last_week = {d.key: 5.5 for d in RUBRIC}
    out = html_report.render(_make_summary(last_week_means=last_week))
    # The span is present, with the muted CSS class.
    assert f'class="{LAST_WEEK_HTML_CLASS}"' in out
    # And carries the annotation text for at least the first dim.
    assert "last-week 5.5" in out


def test_html_renders_one_annotation_per_dimension_row():
    """Each of the 6 dim rows gets its own faded annotation when provided."""
    # Unique values per dim so we can assert each one shows up exactly once.
    last_week = {d.key: 4.0 + i * 0.5 for i, d in enumerate(RUBRIC)}
    out = html_report.render(_make_summary(last_week_means=last_week))
    for i, d in enumerate(RUBRIC):
        expected = f"last-week {4.0 + i * 0.5:.1f}"
        assert expected in out, f"missing annotation for dim {d.key}"


def test_html_omits_last_week_annotation_when_none():
    """No last_week_means -> no rendered annotation in the output.

    The stylesheet rule for ``.dim-last-week`` is always present
    (CSS is static), but no ``<span class="dim-last-week">...</span>``
    should be emitted when no data was provided.
    """
    out = html_report.render(_make_summary(last_week_means=None))
    assert f'class="{LAST_WEEK_HTML_CLASS}"' not in out
    assert (
        ">last-week " not in out
    )  # span body for the annotation  # the "last-week 6.5" annotation text


def test_html_omits_last_week_annotation_when_empty_dict():
    """Empty dict is also a 'no last-week data' signal."""
    out = html_report.render(_make_summary(last_week_means={}))
    assert f'class="{LAST_WEEK_HTML_CLASS}"' not in out
    assert ">last-week " not in out  # span body for the annotation


def test_html_partial_last_week_means_only_renders_provided_dims():
    """If only some dims have last-week data, only those rows show the annotation."""
    # Only planning has a last-week mean; the other five rows skip the annotation.
    last_week = {"planning": 6.5}
    out = html_report.render(_make_summary(last_week_means=last_week))
    # The one that's present:
    assert "last-week 6.5" in out
    # And there is exactly ONE span with the faded class (one row, not six).
    assert out.count(f'<span class="{LAST_WEEK_HTML_CLASS}">') == 1


def test_html_stylesheet_defines_faded_last_week_class():
    """The renderer's CSS must define a rule for the faded annotation class.

    Locks the 'faded' part of the acceptance criterion: the span class
    used by the formatter must have a matching style block that uses
    a muted ink colour. Without this rule the span would inherit the
    default text colour and not actually look faded.
    """
    out = html_report.render(_make_summary(last_week_means={"planning": 5.5}))
    assert f".{LAST_WEEK_HTML_CLASS}" in out
    # The faded look uses one of the muted ink tokens.
    # Check the substring of the stylesheet that defines the rule.
    rule_idx = out.find(f".{LAST_WEEK_HTML_CLASS}")
    block = out[rule_idx : rule_idx + 200]
    assert "var(--ink-300)" in block or "var(--ink-500)" in block


# ---------------------------------------------------------------------- Terminal


def test_terminal_never_renders_last_week_annotation():
    """Spec section 8.1: terminal digest omits the last-week annotation."""
    last_week = {d.key: 5.5 for d in RUBRIC}
    out = terminal.render(_make_summary(last_week_means=last_week))
    # The annotation text is absent.
    assert ">last-week " not in out  # span body for the annotation
    # And the HTML-only CSS class string doesn't sneak in either.
    assert LAST_WEEK_HTML_CLASS not in out


def test_terminal_unaffected_by_none_last_week_means():
    """No last_week_means is the also-no-op case for terminal."""
    out = terminal.render(_make_summary(last_week_means=None))
    assert ">last-week " not in out  # span body for the annotation


def test_terminal_module_does_not_import_last_week_helpers():
    """Lock the design: terminal must not reach for the HTML-only helpers.

    Spec section 8.1 makes terminal a deliberate omission, not an
    'oh we forgot to wire it up' omission. If a future change starts
    importing the last-week formatters into terminal, this test
    catches it -- author would then need to either re-spec or split
    the formatter into a renderer-agnostic core plus an HTML wrapper.
    """
    import praxis.reports.terminal as term_mod

    src = Path(term_mod.__file__).read_text()
    assert "format_last_week_annotation" not in src
    assert "last_week_means" not in src


# ---------------------------------------------------------------------- RunSummary


def test_run_summary_last_week_means_defaults_to_none():
    """RunSummary's last_week_means field is optional, default None.

    Callers that have not yet wired up the baseline computation pass
    no value; the renderer then omits the annotation. This keeps the
    field backwards-compatible with all existing RunSummary
    construction sites (orchestrator + tests).
    """
    summary = RunSummary(
        sessions_seen=0,
        sessions_new=0,
        sessions_scored=0,
        elapsed_seconds=0.0,
        snapshot=_make_snapshot(),
        coaching=_make_coaching(),
        consolidated_for=None,
    )
    assert summary.last_week_means is None
