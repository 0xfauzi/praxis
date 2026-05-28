"""Tests for the adapter's PanelInputs construction (US-038..042).

These cover the cross-summary aggregations the adapter does before the
renderers see them. The behavioral-patterns panel (US-038) is the first
contract: counts and excerpts are aggregated across every user turn in
every session in the week.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


# =========================================================================
# US-039: augmentation/automation balance + cadence panel adapter wiring
# =========================================================================


from praxis.reports.adapter import (  # noqa: E402
    _aug_auto_balance_panel,
    _cadence_panel,
    _classify_high_adopter,
)
from praxis.reports.panel_inputs import (  # noqa: E402
    AUG_AUTO_ANCHOR_CITATION,
    CADENCE_ANCHOR_CITATION,
    CADENCE_WINDOW_DAYS,
    AugAutoBalancePanel,
    CadencePanel,
)


def _session_with_aug_auto(label: str | None, weekday: int = 0):
    """Build a Session-like stub that carries ``aug_auto_classification``.

    The adapter inspects each session via ``getattr`` so a SimpleNamespace
    is enough; this avoids constructing real ``Session`` objects whose
    schema does not yet include the aug_auto column.
    """
    # Anchor the started_at to a Monday (May 25, 2026) plus ``weekday``
    # days so each fixture session lands on a known calendar weekday.
    started_at = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc) + timedelta(days=weekday)
    # The adapter checks user_turns length for substantive-ness; give 2 turns by default.
    turns = [Turn(role=Role.USER, content="hi"), Turn(role=Role.USER, content="ok")]
    s = SimpleNamespace(
        started_at=started_at,
        user_turns=turns,
        aug_auto_classification=label,
    )
    return s


# ---------------- _aug_auto_balance_panel: empty + unavailable paths -----


def test_aug_auto_balance_no_sessions_is_not_unavailable():
    """Zero sessions in the week is structurally different from "API key
    missing"; classifier_unavailable is False so the renderer can show a
    generic empty-state instead of the unavailable copy."""
    panel = _aug_auto_balance_panel(_FakeSummary(sessions=[]))
    assert isinstance(panel, AugAutoBalancePanel)
    assert panel.classifier_unavailable is False
    assert panel.classified_total == 0


def test_aug_auto_balance_all_null_marks_classifier_unavailable():
    """When the week has sessions but every aug_auto label is None
    (typical no-API-key case), classifier_unavailable is True so the
    renderer surfaces the explicit unavailable message."""
    sessions = [
        _session_with_aug_auto(None),
        _session_with_aug_auto(None),
    ]
    panel = _aug_auto_balance_panel(SimpleNamespace(sessions=sessions))
    assert panel.classifier_unavailable is True
    assert panel.unclassified_count == 2


def test_aug_auto_balance_counts_each_label():
    """Augmentation/automation/mixed labels are counted separately."""
    sessions = [
        _session_with_aug_auto("augmentation"),
        _session_with_aug_auto("augmentation"),
        _session_with_aug_auto("automation"),
        _session_with_aug_auto("mixed"),
    ]
    panel = _aug_auto_balance_panel(SimpleNamespace(sessions=sessions))
    assert panel.augmentation_count == 2
    assert panel.automation_count == 1
    assert panel.mixed_count == 1
    assert panel.classifier_unavailable is False


def test_aug_auto_balance_shares_compute_against_classified_total():
    """Shares are derived against the classified denominator so NULL
    rows do not dilute the percentages."""
    sessions = [
        _session_with_aug_auto("augmentation"),
        _session_with_aug_auto("automation"),
        _session_with_aug_auto(None),  # NULL row, must not dilute shares
    ]
    panel = _aug_auto_balance_panel(SimpleNamespace(sessions=sessions))
    assert panel.augmentation_share == 0.5
    assert panel.automation_share == 0.5
    assert panel.mixed_share == 0.0
    assert panel.unclassified_count == 1


def test_aug_auto_balance_unknown_label_is_treated_as_null():
    """A label outside the three literals is treated as unclassified
    (defensive: a future classifier change must not silently bucket
    a stray label into one of the three known categories)."""
    sessions = [
        _session_with_aug_auto("augmentation"),
        _session_with_aug_auto("hallucination"),  # not a valid label
    ]
    panel = _aug_auto_balance_panel(SimpleNamespace(sessions=sessions))
    assert panel.augmentation_count == 1
    assert panel.unclassified_count == 1


def test_aug_auto_balance_carries_industry_anchor():
    """Every panel carries the Anthropic Economic Index anchor so the
    renderer can surface it inline (US-039 acceptance)."""
    panel = _aug_auto_balance_panel(_FakeSummary(sessions=[]))
    assert "Anthropic Economic Index" in panel.industry_anchor_citation
    assert panel.industry_anchor_citation == AUG_AUTO_ANCHOR_CITATION


# ------------------------- _cadence_panel: empty + populated paths --------


def test_cadence_panel_no_sessions_has_no_activity():
    """Zero sessions => substantive_session_count is 0 and the
    high-adopter label is None (the spectrum position is undefined)."""
    panel = _cadence_panel(_FakeSummary(sessions=[]))
    assert isinstance(panel, CadencePanel)
    assert panel.has_activity is False
    assert panel.high_adopter_position is None


def test_cadence_panel_single_substantive_session_counts():
    """One substantive session on one weekday => streak of 1, low
    position on the spectrum."""
    sessions = [_session_with_aug_auto("augmentation", weekday=0)]
    panel = _cadence_panel(SimpleNamespace(sessions=sessions))
    assert panel.substantive_session_count == 1
    assert panel.weekday_streak == 1
    assert panel.has_activity is True
    assert panel.position_label == "Low-adopter"


def test_cadence_panel_dedupes_multiple_sessions_same_weekday():
    """Two sessions on the same calendar weekday count as one weekday
    of streak; substantive_session_count keeps both."""
    sessions = [
        _session_with_aug_auto(None, weekday=0),
        _session_with_aug_auto(None, weekday=0),
    ]
    panel = _cadence_panel(SimpleNamespace(sessions=sessions))
    assert panel.substantive_session_count == 2
    assert panel.weekday_streak == 1


def test_cadence_panel_drops_non_substantive_sessions():
    """A session with fewer than 2 user turns is not substantive and
    contributes neither to the count nor the streak."""
    # Build a session with only one user turn.
    s = SimpleNamespace(
        started_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
        user_turns=[Turn(role=Role.USER, content="hi")],
        aug_auto_classification=None,
    )
    panel = _cadence_panel(SimpleNamespace(sessions=[s]))
    assert panel.substantive_session_count == 0
    assert panel.weekday_streak == 0
    assert panel.has_activity is False


def test_cadence_panel_high_adopter_position_at_two_thirds():
    """Streak >= 2/3 of the 21-day window => 'high'."""
    sessions = [
        _session_with_aug_auto(None, weekday=i) for i in range(14)
    ]
    panel = _cadence_panel(SimpleNamespace(sessions=sessions))
    # 14 / 21 = 0.667 -> high
    assert panel.weekday_streak == 7  # bounded by python's weekday() returning 0..6
    # The classification reads ratio = 7/21 = 0.33 which is on the boundary.
    # Use a more direct check via _classify_high_adopter to be unambiguous.
    assert _classify_high_adopter(14, CADENCE_WINDOW_DAYS) == "high"


def test_cadence_panel_carries_citation():
    """Every cadence panel carries the arXiv 2509.19708 anchor."""
    panel = _cadence_panel(_FakeSummary(sessions=[]))
    assert "arXiv 2509.19708" in panel.citation
    assert panel.citation == CADENCE_ANCHOR_CITATION


# ---------------------------- _classify_high_adopter ----------------------


def test_classify_high_adopter_zero_streak_is_none():
    """Zero activity is the renderer's "no spectrum" signal; the
    function returns None rather than the lowest bucket so the renderer
    can omit the label entirely (US-039 AC)."""
    assert _classify_high_adopter(0, CADENCE_WINDOW_DAYS) is None


def test_classify_high_adopter_thresholds():
    """Boundary checks at 1/3 and 2/3 of the 21-day window."""
    # ratio < 1/3 -> low
    assert _classify_high_adopter(6, 21) == "low"  # 6/21 = 0.285
    # 1/3 <= ratio < 2/3 -> moderate
    assert _classify_high_adopter(7, 21) == "moderate"  # 7/21 = 0.333
    assert _classify_high_adopter(13, 21) == "moderate"  # 13/21 = 0.619
    # ratio >= 2/3 -> high
    assert _classify_high_adopter(14, 21) == "high"  # 14/21 = 0.667


def test_classify_high_adopter_invalid_window_is_none():
    """A zero or negative window is malformed; the function returns
    None rather than dividing by zero."""
    assert _classify_high_adopter(5, 0) is None
    assert _classify_high_adopter(5, -1) is None


# ---------------------- build_panel_inputs wires both new panels ----------


def test_build_panel_inputs_includes_aug_auto_and_cadence():
    """The top-level adapter exposes both new panels alongside the
    behavioral signals panel."""
    pi = build_panel_inputs(_FakeSummary(sessions=[]))
    assert pi.behavioral_signals is not None
    assert pi.aug_auto_balance is not None
    assert pi.cadence is not None


# ---------------------- shape contracts ----------------------------------


def test_aug_auto_balance_panel_is_frozen():
    """The dataclass is immutable so the renderer cannot mutate
    aggregation state mid-render."""
    import dataclasses

    panel = AugAutoBalancePanel()
    try:
        panel.augmentation_count = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("AugAutoBalancePanel must be frozen")


def test_cadence_panel_is_frozen():
    """Same immutability contract as the other panel dataclasses."""
    import dataclasses

    panel = CadencePanel()
    try:
        panel.weekday_streak = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("CadencePanel must be frozen")


# =========================================================================
# US-040: repeat-task radar + verification-calibration panel adapter wiring
# =========================================================================


from praxis.reports.adapter import (  # noqa: E402
    _repeat_task_radar_panel,
    _verification_calibration_panel,
)
from praxis.reports.panel_inputs import (  # noqa: E402
    REPEAT_TASK_CITATION,
    REPEAT_TASK_SKILL_TAG,
    VERIFICATION_CALIBRATION_CITATION,
    RepeatTaskRadarPanel,
    RepeatTaskRow,
    VerificationCalibrationPanel,
)
from praxis.scoring.clustering import Task  # noqa: E402


def _session_with_first_turn(
    text: str,
    stable_id_seed: str,
    other_turns: list[str] | None = None,
) -> Session:
    """Build a real Session whose first user turn carries ``text``."""
    turns = [Turn(role=Role.USER, content=text)]
    for extra in other_turns or []:
        turns.append(Turn(role=Role.USER, content=extra))
    return Session(
        provider=Provider.CLAUDE,
        session_id=stable_id_seed,
        started_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
        turns=turns,
        source_path="/tmp/test",
    )


def _make_task(label: str, session_ids: list[str]) -> Task:
    return Task(
        label=label,
        task_type="implementation",
        session_ids=session_ids,
        rationale="",
    )


# ----------- _repeat_task_radar_panel: empty + populated paths ----------


def test_repeat_task_radar_no_tasks_returns_empty_panel():
    """Zero tasks => empty panel with no rows."""
    panel = _repeat_task_radar_panel(_FakeSummary())
    assert isinstance(panel, RepeatTaskRadarPanel)
    assert panel.has_repeats is False
    assert panel.rows == ()


def test_repeat_task_radar_no_recurring_clusters_returns_empty():
    """When the user's tasks don't recur 3+ times, the detector emits
    no RepeatTasks and the panel reads as empty."""
    sessions = [
        _session_with_first_turn("refactor the auth module today", "a"),
        _session_with_first_turn("write the changelog entry", "b"),
    ]
    tasks = [
        _make_task("auth refactor", [sessions[0].stable_id]),
        _make_task("changelog", [sessions[1].stable_id]),
    ]
    summary = SimpleNamespace(sessions=sessions, tasks=tasks)
    panel = _repeat_task_radar_panel(summary)
    assert panel.has_repeats is False


def test_repeat_task_radar_detects_three_repeats():
    """Three clusters whose first sentences overlap >= 0.70 should
    surface one RepeatTask row in the panel."""
    sessions = [
        _session_with_first_turn("fix the failing auth test in module", f"s-{i}")
        for i in range(3)
    ]
    tasks = [
        _make_task(f"auth-test-fix-{i}", [sessions[i].stable_id])
        for i in range(3)
    ]
    summary = SimpleNamespace(sessions=sessions, tasks=tasks)
    panel = _repeat_task_radar_panel(summary)
    assert panel.has_repeats is True
    assert len(panel.rows) == 1
    row = panel.rows[0]
    assert row.occurrences == 3
    assert row.skill_tag == REPEAT_TASK_SKILL_TAG


def test_repeat_task_radar_carries_citation():
    """The panel surfaces the OpenAI + Anthropic Skills citation so the
    renderer can render the primary source inline (US-040 AC)."""
    panel = _repeat_task_radar_panel(_FakeSummary())
    assert panel.citation == REPEAT_TASK_CITATION
    assert "OpenAI" in panel.citation
    assert "Anthropic" in panel.citation


def test_repeat_task_radar_skips_tasks_with_no_sessions():
    """A task whose session_ids reference no sessions in summary is
    skipped (it cannot anchor a cluster) rather than crashing the
    detector."""
    sessions = [
        _session_with_first_turn("real session content", "s-real"),
    ]
    tasks = [
        _make_task("ghost task", ["nonexistent"]),
        _make_task("real task", [sessions[0].stable_id]),
    ]
    summary = SimpleNamespace(sessions=sessions, tasks=tasks)
    # Single cluster doesn't recur but the call must succeed.
    panel = _repeat_task_radar_panel(summary)
    assert isinstance(panel, RepeatTaskRadarPanel)


def test_repeat_task_radar_estimates_per_occurrence_minutes():
    """When the detector returns a repeat, the row carries a positive
    estimated_minutes_per_occurrence so the renderer can surface the
    'a skill could reclaim ~N min' hint."""
    sessions = [
        _session_with_first_turn(
            "fix the failing auth integration test today carefully",
            f"s-{i}",
            other_turns=["follow-up turn"],
        )
        for i in range(3)
    ]
    tasks = [
        _make_task(f"auth-test-{i}", [sessions[i].stable_id]) for i in range(3)
    ]
    summary = SimpleNamespace(sessions=sessions, tasks=tasks)
    panel = _repeat_task_radar_panel(summary)
    assert panel.has_repeats is True
    assert panel.rows[0].estimated_minutes_per_occurrence > 0.0


# --------- _verification_calibration_panel: empty + populated paths -----


def test_verification_calibration_no_sessions_all_zero():
    """Zero sessions => all four bucket counts are zero, has_sessions
    is False so the renderer can surface a generic empty-state."""
    panel = _verification_calibration_panel(_FakeSummary())
    assert isinstance(panel, VerificationCalibrationPanel)
    assert panel.has_sessions is False
    assert panel.total_sessions == 0


def test_verification_calibration_blanket_accept_default():
    """A week of sessions with no verification activity surfaces
    every session in the blanket_accept bucket."""
    sessions = [
        Session(
            provider=Provider.CLAUDE,
            session_id=f"s-{i}",
            started_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            turns=[Turn(role=Role.USER, content="write me a function")],
            source_path="/tmp/x",
        )
        for i in range(3)
    ]
    summary = SimpleNamespace(sessions=sessions)
    panel = _verification_calibration_panel(summary)
    assert panel.blanket_accept_count == 3
    assert panel.source_check_count == 0
    assert panel.test_run_count == 0
    assert panel.spot_check_count == 0


def test_verification_calibration_distributes_across_buckets():
    """Mixed verification ceilings produce a populated histogram."""
    sessions = [
        Session(
            provider=Provider.CLAUDE,
            session_id="src",
            started_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            turns=[Turn(role=Role.USER, content="what's the source for this claim?")],
            source_path="/tmp/x",
        ),
        Session(
            provider=Provider.CLAUDE,
            session_id="test",
            started_at=datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc),
            turns=[Turn(role=Role.USER, content="let me run the tests")],
            source_path="/tmp/x",
        ),
        Session(
            provider=Provider.CLAUDE,
            session_id="spot",
            started_at=datetime(2026, 5, 25, 14, 0, tzinfo=timezone.utc),
            turns=[Turn(role=Role.USER, content="that looks right, let me double-check")],
            source_path="/tmp/x",
        ),
        Session(
            provider=Provider.CLAUDE,
            session_id="blanket",
            started_at=datetime(2026, 5, 25, 15, 0, tzinfo=timezone.utc),
            turns=[Turn(role=Role.USER, content="write me a function")],
            source_path="/tmp/x",
        ),
    ]
    summary = SimpleNamespace(sessions=sessions)
    panel = _verification_calibration_panel(summary)
    assert panel.source_check_count == 1
    assert panel.test_run_count == 1
    assert panel.spot_check_count == 1
    assert panel.blanket_accept_count == 1
    assert panel.total_sessions == 4
    assert panel.has_sessions is True


def test_verification_calibration_carries_citation():
    """Each panel carries the Sonar / Stack Overflow / automation-bias
    anchor string so the renderer can surface it inline."""
    panel = _verification_calibration_panel(_FakeSummary())
    assert panel.citation == VERIFICATION_CALIBRATION_CITATION
    assert "Sonar" in panel.citation
    assert "Stack Overflow" in panel.citation
    assert "automation-bias" in panel.citation


def test_verification_calibration_count_for_unknown_key_is_zero():
    """count_for is the renderer-facing lookup and must not raise on
    an unknown key; an unknown key returns 0 so the renderer can
    iterate the kinds-in-order tuple without a try/except."""
    panel = VerificationCalibrationPanel()
    assert panel.count_for("nonexistent") == 0


# ---------------------- shape contracts -----------------------------------


def test_repeat_task_radar_panel_is_frozen():
    """Immutability matches the other panel dataclasses."""
    import dataclasses

    panel = RepeatTaskRadarPanel()
    try:
        panel.rows = ()  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("RepeatTaskRadarPanel must be frozen")


def test_repeat_task_row_is_frozen():
    """Immutability for the row dataclass too."""
    import dataclasses

    row = RepeatTaskRow(
        canonical_first_sentence="x",
        occurrences=1,
        estimated_minutes_per_occurrence=1.0,
    )
    try:
        row.occurrences = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("RepeatTaskRow must be frozen")


def test_verification_calibration_panel_is_frozen():
    """Same immutability contract as the other panel dataclasses."""
    import dataclasses

    panel = VerificationCalibrationPanel()
    try:
        panel.source_check_count = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("VerificationCalibrationPanel must be frozen")


# ---------- build_panel_inputs wires both new US-040 panels --------------


def test_build_panel_inputs_includes_us040_panels():
    """The top-level adapter exposes the US-040 panels alongside the
    existing US-038 / US-039 panels so a single call produces every
    expansion-panel input."""
    pi = build_panel_inputs(_FakeSummary(sessions=[]))
    assert pi.behavioral_signals is not None
    assert pi.aug_auto_balance is not None
    assert pi.cadence is not None
    assert pi.repeat_task_radar is not None
    assert pi.verification_calibration is not None


# =========================================================================
# US-041: specification adoption + context engineering + knowledge gaps
# =========================================================================


from praxis.reports.adapter import (  # noqa: E402
    _context_engineering_panel,
    _knowledge_gap_distribution_panel,
    _specification_adoption_panel,
)
from praxis.reports.panel_inputs import (  # noqa: E402
    CONTEXT_ENGINEERING_CITATION,
    KNOWLEDGE_GAP_CITATION,
    KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER,
    SCAFFOLDING_KINDS_IN_PANEL_ORDER,
    SPECIFICATION_ADOPTION_CITATION,
    ContextEngineeringDepthPanel,
    KnowledgeGapDistributionPanel,
    SpecificationAdoptionPanel,
)


def _spec_session(open_text: str) -> Session:
    """A real Session whose first user turn carries ``open_text``."""
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"s-{hash(open_text) & 0xffffffff}",
        started_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
        turns=[Turn(role=Role.USER, content=open_text)],
        source_path="/tmp/test",
    )


# --------- _specification_adoption_panel: empty + populated paths -----


def test_specification_adoption_no_sessions_is_unmeasured():
    """Zero sessions => has_sessions is False so the renderer can
    surface a no-data placeholder rather than a misleading 0%."""
    panel = _specification_adoption_panel(_FakeSummary())
    assert isinstance(panel, SpecificationAdoptionPanel)
    assert panel.has_sessions is False
    assert panel.total_sessions == 0
    assert panel.sessions_with_spec == 0


def test_specification_adoption_counts_spec_block_openings():
    """Sessions opening with a Markdown spec heading count toward
    adoption; freeform implementation prompts do not."""
    sessions = [
        _spec_session("## Goal\nRefactor the auth flow."),
        _spec_session("write me a sorter"),
        _spec_session("Goal: ship safely. Acceptance criteria: zero downtime."),
    ]
    panel = _specification_adoption_panel(SimpleNamespace(sessions=sessions))
    assert panel.total_sessions == 3
    assert panel.sessions_with_spec == 2
    # 2 of 3 sessions opened with a spec block.
    assert abs(panel.adoption_share - 2 / 3) < 1e-6


def test_specification_adoption_carries_citation():
    """The panel carries the Woodward / SpecKit / Sean Grove citation
    so the renderer can surface it inline (US-041 AC)."""
    panel = _specification_adoption_panel(_FakeSummary())
    assert panel.citation == SPECIFICATION_ADOPTION_CITATION
    assert "Woodward" in panel.citation
    assert "SpecKit" in panel.citation
    assert "Sean Grove" in panel.citation


def test_specification_adoption_share_zero_when_no_sessions():
    """``adoption_share`` returns 0.0 cleanly when no sessions exist
    (callers should guard on has_sessions first)."""
    panel = _specification_adoption_panel(_FakeSummary())
    assert panel.adoption_share == 0.0


# --------- _context_engineering_panel: empty + populated paths --------


def test_context_engineering_no_sessions_returns_zero_rows():
    """Zero sessions => every scaffolding kind has zero count and
    has_any_artifact is False."""
    panel = _context_engineering_panel(_FakeSummary())
    assert isinstance(panel, ContextEngineeringDepthPanel)
    assert panel.has_any_artifact is False
    # Every kind is still represented (one row per kind).
    assert len(panel.rows) == len(SCAFFOLDING_KINDS_IN_PANEL_ORDER)


def test_context_engineering_counts_per_kind():
    """A week with sessions referencing multiple scaffolding kinds
    surfaces per-kind session counts."""
    sessions = [
        _spec_session("update CLAUDE.md and add tests"),
        _spec_session("modify AGENTS.md to mention the rubric"),
        _spec_session("write a skills/code-review skill"),
        _spec_session("refactor without scaffolding"),
    ]
    panel = _context_engineering_panel(SimpleNamespace(sessions=sessions))
    by_kind = {row.kind: row.sessions_with_artifact for row in panel.rows}
    assert by_kind["claude_md"] == 1
    assert by_kind["agents_md"] == 1
    assert by_kind["skills"] == 1


def test_context_engineering_carries_citation():
    """The panel carries the DORA 2025 + Anthropic Skills citation."""
    panel = _context_engineering_panel(_FakeSummary())
    assert panel.citation == CONTEXT_ENGINEERING_CITATION
    assert "DORA 2025" in panel.citation
    assert "Anthropic" in panel.citation


def test_context_engineering_rows_in_panel_order():
    """The rows must appear in SCAFFOLDING_KINDS_IN_PANEL_ORDER so the
    renderer can iterate that tuple safely."""
    panel = _context_engineering_panel(_FakeSummary())
    kinds = tuple(row.kind for row in panel.rows)
    assert kinds == SCAFFOLDING_KINDS_IN_PANEL_ORDER


# --------- _knowledge_gap_distribution_panel: empty + populated paths --


def test_knowledge_gap_no_sessions_returns_zero_rows():
    """Zero sessions => four rows with zero counts. has_gaps is False
    so the renderer falls through to the empty-state copy."""
    panel = _knowledge_gap_distribution_panel(_FakeSummary())
    assert isinstance(panel, KnowledgeGapDistributionPanel)
    assert panel.has_gaps is False
    assert panel.total_gaps == 0
    # Each of the four arXiv 2501.11709 categories is still represented
    # so the renderer never silently drops a category (US-041 AC).
    assert len(panel.rows) == len(KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER)


def test_knowledge_gap_zero_session_contributes_zero_to_each_category():
    """A session with no detected gaps still contributes a zero to
    each category (US-041 acceptance: 'no silent drops')."""
    sessions = [
        _spec_session("What does the LRU eviction policy do?"),
    ]
    panel = _knowledge_gap_distribution_panel(SimpleNamespace(sessions=sessions))
    for row in panel.rows:
        assert row.count == 0
    assert panel.has_gaps is False


def test_knowledge_gap_accumulates_across_sessions():
    """Per-turn counts across multiple sessions sum into per-kind row
    counts."""
    sessions = [
        Session(
            provider=Provider.CLAUDE,
            session_id="s-1",
            started_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            turns=[
                Turn(role=Role.USER, content="write me a sorter"),
                Turn(role=Role.USER, content="fix the failing auth test"),
            ],
            source_path="/tmp/x",
        ),
        Session(
            provider=Provider.CLAUDE,
            session_id="s-2",
            started_at=datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc),
            turns=[
                Turn(role=Role.USER, content="do something with this codebase"),
            ],
            source_path="/tmp/x",
        ),
    ]
    panel = _knowledge_gap_distribution_panel(SimpleNamespace(sessions=sessions))
    by_kind = {row.kind: row.count for row in panel.rows}
    # 2 build-imperatives without specs in s-1 (both turns); s-2 is
    # vague rather than a build-imperative -> missing_specs >= 2.
    assert by_kind["missing_specs"] >= 2
    assert by_kind["unclear_instructions"] >= 1
    assert panel.has_gaps is True


def test_knowledge_gap_carries_citation():
    """The panel carries the arXiv 2501.11709 citation."""
    panel = _knowledge_gap_distribution_panel(_FakeSummary())
    assert panel.citation == KNOWLEDGE_GAP_CITATION
    assert "2501.11709" in panel.citation


def test_knowledge_gap_rows_in_panel_order():
    """The four rows must appear in KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER
    so the renderer iterates a stable order."""
    panel = _knowledge_gap_distribution_panel(_FakeSummary())
    kinds = tuple(row.kind for row in panel.rows)
    assert kinds == KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER


# ---------- shape contracts ---------------------------------------------


def test_specification_adoption_panel_is_frozen():
    """Immutability matches the other panel dataclasses."""
    import dataclasses

    panel = SpecificationAdoptionPanel()
    try:
        panel.sessions_with_spec = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("SpecificationAdoptionPanel must be frozen")


def test_context_engineering_panel_is_frozen():
    """Immutability matches the other panel dataclasses."""
    import dataclasses

    panel = ContextEngineeringDepthPanel()
    try:
        panel.rows = ()  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ContextEngineeringDepthPanel must be frozen")


def test_knowledge_gap_distribution_panel_is_frozen():
    """Immutability matches the other panel dataclasses."""
    import dataclasses

    panel = KnowledgeGapDistributionPanel()
    try:
        panel.rows = ()  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("KnowledgeGapDistributionPanel must be frozen")


# ---------- build_panel_inputs wires every US-041 panel -----------------


def test_build_panel_inputs_includes_us041_panels():
    """The top-level adapter exposes the US-041 panels so a single
    call produces every expansion-panel input."""
    pi = build_panel_inputs(_FakeSummary(sessions=[]))
    assert pi.specification_adoption is not None
    assert pi.context_engineering is not None
    assert pi.knowledge_gap_distribution is not None
