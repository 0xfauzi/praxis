"""Tests for the adapter's PanelInputs construction (US-038..042).

These cover the cross-summary aggregations the adapter does before the
renderers see them. The behavioral-patterns panel (US-038) is the first
contract: counts and excerpts are aggregated across every user turn in
every session in the week.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

from praxis.models import Provider, Role, Session, Turn
from praxis.reports.adapter import (
    _behavioral_patterns_panel,
    build_panel_inputs,
)
from praxis.reports.panel_inputs import (
    EXCERPT_CHAR_LIMIT,
    MAX_EXCERPTS_PER_SIGNAL,
    SIGNAL_KINDS_IN_PANEL_ORDER,
    BehavioralPatternRow,
    BehavioralPatternsPanel,
    PanelInputs,
    clip_excerpt,
)


def _make_session(turn_texts: list[str]) -> Session:
    """Build a Session whose user turns carry the given content strings."""
    turns = [Turn(role=Role.USER, content=t) for t in turn_texts]
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"s-{len(turn_texts)}-{turn_texts[0][:6] if turn_texts else ''}",
        started_at=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
        turns=turns,
        source_path="/tmp/test",
    )


@dataclass
class _FakeSummary:
    """Minimal stand-in for the orchestrator's _SummaryView.

    The behavioral-patterns adapter only reads ``sessions``; everything
    else stays empty.
    """
    sessions: list[Session] = field(default_factory=list)
    tasks: list = field(default_factory=list)


# ----------------------------------------------------------- clip_excerpt


def test_clip_excerpt_collapses_whitespace():
    """The helper collapses internal newlines and runs of whitespace so
    every excerpt renders as one display line."""
    assert clip_excerpt("foo\nbar  baz") == "foo bar baz"


def test_clip_excerpt_strips_outer_whitespace():
    """Leading + trailing whitespace must not consume the visible budget."""
    assert clip_excerpt("   hello   ") == "hello"


def test_clip_excerpt_under_limit_unchanged():
    """A short, single-line excerpt is returned verbatim apart from the
    whitespace normalisation."""
    short = "why does this work?"
    assert clip_excerpt(short) == short


def test_clip_excerpt_truncates_with_ellipsis():
    """When the input exceeds the limit it is truncated and an ellipsis
    is appended; total length still respects the limit so the rendered
    line stays within budget."""
    long = "x" * 200
    clipped = clip_excerpt(long, limit=50)
    assert clipped.endswith("...")
    assert len(clipped) <= 50


def test_clip_excerpt_default_limit_is_panel_constant():
    """The default limit matches the panel-wide EXCERPT_CHAR_LIMIT so
    the adapter and the renderer never disagree on the budget."""
    long = "y" * (EXCERPT_CHAR_LIMIT * 2)
    clipped = clip_excerpt(long)
    assert len(clipped) <= EXCERPT_CHAR_LIMIT


# ----------------------------------------- _behavioral_patterns_panel: shape


def test_behavioral_panel_returns_row_per_signal_kind():
    """Even with zero sessions the panel emits a row per signal kind so
    the renderer can decide between empty-state and populated paths
    uniformly."""
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=[]))
    assert isinstance(panel, BehavioralPatternsPanel)
    kinds = tuple(row.signal_kind for row in panel.rows)
    assert kinds == SIGNAL_KINDS_IN_PANEL_ORDER


def test_behavioral_panel_empty_sessions_has_no_signals():
    """No sessions => has_signals==False, all counts zero, no excerpts."""
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=[]))
    assert panel.has_signals is False
    for row in panel.rows:
        assert row.count == 0
        assert row.excerpts == ()


def test_behavioral_panel_none_sessions_treated_as_empty():
    """``summary.sessions`` may be None (e.g. a freshly-built view that
    has not yet been populated); the adapter must not crash."""
    summary = SimpleNamespace(sessions=None)
    panel = _behavioral_patterns_panel(summary)
    assert panel.has_signals is False


# ----------------------------------------- _behavioral_patterns_panel: counts


def test_behavioral_panel_counts_why_questions_across_sessions():
    """A signal that fires across multiple turns and multiple sessions
    is summed correctly."""
    sessions = [
        _make_session([
            "Why does this approach work for caching?",
            "Why is this slower than the previous version?",
        ]),
        _make_session([
            "Why does this even compile?",
        ]),
    ]
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=sessions))
    why_row = next(r for r in panel.rows if r.signal_kind == "why_question")
    assert why_row.count == 3


def test_behavioral_panel_counts_pure_delegation():
    """Pure delegation pattern (imperative opener) fires on each
    qualifying turn and is summed across the week."""
    sessions = [
        _make_session([
            "write me a function that sorts",
            "make it handle errors too",
        ]),
    ]
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=sessions))
    pd_row = next(r for r in panel.rows if r.signal_kind == "pure_delegation")
    assert pd_row.count == 2


def test_behavioral_panel_has_signals_true_when_any_row_fires():
    """The has_signals property is the renderer's empty-state gate."""
    sessions = [_make_session(["write me a tiny script"])]
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=sessions))
    assert panel.has_signals is True


# ---------------------------------------- _behavioral_patterns_panel: excerpts


def test_behavioral_panel_captures_excerpts_for_fired_signals():
    """Each fired signal carries up to MAX_EXCERPTS_PER_SIGNAL example
    excerpts so the reader can see what triggered the count."""
    sessions = [
        _make_session([
            "Why does this approach work for caching?",
            "Why is this slower than the previous version?",
            "Why does this even compile?",  # third match - should be dropped
        ]),
    ]
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=sessions))
    why_row = next(r for r in panel.rows if r.signal_kind == "why_question")
    assert len(why_row.excerpts) == MAX_EXCERPTS_PER_SIGNAL
    assert "caching" in why_row.excerpts[0]
    assert "slower" in why_row.excerpts[1]


def test_behavioral_panel_clips_long_excerpts():
    """Long user turns get clipped to EXCERPT_CHAR_LIMIT so a sprawling
    transcript paragraph does not blow up the panel layout."""
    long_why = "Why " + ("does this work as expected " * 20) + "?"
    sessions = [_make_session([long_why])]
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=sessions))
    why_row = next(r for r in panel.rows if r.signal_kind == "why_question")
    assert len(why_row.excerpts) == 1
    assert len(why_row.excerpts[0]) <= EXCERPT_CHAR_LIMIT


def test_behavioral_panel_carries_citation_per_row():
    """Each row carries the primary source string the renderer surfaces
    as a footnote (US-038 acceptance)."""
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=[]))
    for row in panel.rows:
        assert row.citation  # non-empty string
        assert "arXiv" in row.citation


def test_behavioral_panel_carries_display_label_per_row():
    """Each row carries a user-facing label so the renderer never has
    to map signal kinds to display strings itself."""
    panel = _behavioral_patterns_panel(_FakeSummary(sessions=[]))
    labels = {row.signal_kind: row.label for row in panel.rows}
    assert labels["why_question"] == "Why-questions"
    assert labels["pure_delegation"] == "Pure delegation"


# ----------------------------------------------- build_panel_inputs wrapper


def test_build_panel_inputs_returns_panel_inputs():
    """The top-level adapter returns the bag of expansion-panel inputs;
    today that means behavioral_signals is populated."""
    pi = build_panel_inputs(_FakeSummary(sessions=[]))
    assert isinstance(pi, PanelInputs)
    assert pi.behavioral_signals is not None


def test_build_panel_inputs_behavioral_signals_uses_sessions():
    """The PanelInputs.behavioral_signals field carries the same
    aggregation _behavioral_patterns_panel produces."""
    sessions = [_make_session(["why does this break?"])]
    pi = build_panel_inputs(_FakeSummary(sessions=sessions))
    assert pi.behavioral_signals is not None
    assert pi.behavioral_signals.has_signals is True


# --------------------------------------------- dataclass shape contracts


def test_behavioral_pattern_row_is_frozen():
    """Rows are immutable so the renderer cannot accidentally mutate
    aggregation state mid-render."""
    import dataclasses

    row = BehavioralPatternRow(
        signal_kind="why_question",
        label="Why-questions",
        count=1,
        citation="Shen & Tamkin 2026 (arXiv 2601.20245)",
    )
    try:
        row.count = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("BehavioralPatternRow must be frozen")
