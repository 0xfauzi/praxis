"""Tests for behavioral signals and trajectory analysis.

Spec 10.4:
  - Empty session list -> INSUFFICIENT_DATA
  - 10 sessions of monotone-rising engagement -> LEARNING
  - 10 sessions of pure_delegator=True -> STABLE_PASSIVE
  - _linear_slope([1,2,3,4,5]) ~= 1.0
  - LLM trajectory call returns None gracefully when keys absent
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from praxis.behavior.signals import (
    KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER,
    SCAFFOLDING_KINDS_IN_PANEL_ORDER,
    SIGNAL_KINDS_IN_PANEL_ORDER,
    BehavioralSignals,
    categorize_session_verification,
    count_session_knowledge_gaps,
    detect_knowledge_gap_kinds,
    detect_scaffolding_kinds,
    detect_signal_kinds,
    detect_spec_block,
    detect_verification_calibration_kinds,
    extract,
)
from praxis.behavior.trajectory import (
    TrajectoryLabel,
    _linear_slope,
    assess,
    assess_trajectory_heuristic,
    assess_trajectory_with_llm,
)
from praxis.models import Provider, Role, Session, Turn


def _make_session(turns: list[Turn], when: datetime) -> Session:
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"s-{when.isoformat()}",
        started_at=when,
        turns=turns,
        source_path="/tmp/x",
    )


def test_linear_slope_basic():
    assert abs(_linear_slope([1, 2, 3, 4, 5]) - 1.0) < 1e-9
    assert _linear_slope([1, 1, 1]) == 0.0
    assert _linear_slope([1, 2]) == 0.0  # n < 3


def test_extract_engaged_session_has_positive_engagement():
    turns = [
        Turn(role=Role.USER, content="Why does this approach work for caching?"),
        Turn(role=Role.ASSISTANT, content="..."),
        Turn(
            role=Role.USER,
            content="So if I understand, the LRU evicts oldest. Am I right that this avoids the thundering herd?",
        ),
    ]
    sig = extract(_make_session(turns, datetime.now(UTC)))
    assert sig.engagement_rate > 0
    assert sig.is_pure_delegator is False


def test_extract_skips_tool_injected_preamble_turns():
    """Regression for issue #4.

    A turn whose entire content is a tool-injected preamble (Codex
    AGENTS.md, Claude Code ``<system-reminder>``) was being counted
    against the user by the regex-based extractors. With the scanner
    tagging those turns ``tool_injected=True`` and ``extract`` iterating
    ``session.user_authored_turns``, the preamble must not contribute to
    any signal count.

    The preamble below contains the strings ``explain`` and ``why``
    which would otherwise match _EXPLANATION_REQUESTS and _WHY_QUESTIONS.
    """
    preamble = (
        "# AGENTS.md\n\n"
        "Please explain decisions and follow why-first communication.\n"
        "<INSTRUCTIONS>be precise</INSTRUCTIONS>"
    )
    turns = [
        Turn(role=Role.USER, content=preamble, tool_injected=True),
        Turn(role=Role.USER, content="write me a function"),
    ]
    sig = extract(_make_session(turns, datetime.now(UTC)))
    # Only the second turn should count.
    assert sig.user_turn_count == 1
    assert sig.why_question_count == 0
    assert sig.explanation_request_count == 0


def test_extract_delegating_session_flags_pure_delegator():
    turns = [
        Turn(role=Role.USER, content="write me a function"),
        Turn(role=Role.USER, content="make it handle errors"),
        Turn(role=Role.USER, content="fix this"),
        Turn(role=Role.USER, content="now write tests"),
    ]
    sig = extract(_make_session(turns, datetime.now(UTC)))
    assert sig.delegation_rate > 0.5
    assert sig.is_pure_delegator is True


def test_assess_trajectory_insufficient_data():
    assess_result = assess_trajectory_heuristic([])
    assert assess_result.label == TrajectoryLabel.INSUFFICIENT_DATA


def test_assess_trajectory_learning_label():
    # Construct 10 sessions where engagement rises monotonically and delegation falls.
    base = datetime.now(UTC) - timedelta(days=10)
    pairs = []
    for i in range(10):
        sig = BehavioralSignals(
            user_turn_count=4,
            why_question_count=i,
            comprehension_check_count=i // 2,
            explanation_request_count=0,
            pure_delegation_count=max(0, 5 - i),
            outsourced_debug_count=0,
            telegraphic_count=0,
            own_attempt_count=i // 2,
            engagement_rate=min(1.0, 0.05 * i),
            delegation_rate=max(0.0, 0.6 - 0.06 * i),
            independence_rate=0.0,
            is_pure_delegator=False,
        )
        session = _make_session(
            [Turn(role=Role.USER, content=f"turn-{i}")], base + timedelta(days=i)
        )
        pairs.append((session, sig))
    result = assess_trajectory_heuristic(pairs)
    assert result.label == TrajectoryLabel.LEARNING


def test_assess_trajectory_stable_passive_label_for_delegators():
    base = datetime.now(UTC) - timedelta(days=10)
    pairs = []
    for i in range(10):
        sig = BehavioralSignals(
            user_turn_count=4,
            why_question_count=0,
            comprehension_check_count=0,
            explanation_request_count=0,
            pure_delegation_count=4,
            outsourced_debug_count=0,
            telegraphic_count=4,
            own_attempt_count=0,
            engagement_rate=0.0,
            delegation_rate=1.0,
            independence_rate=0.0,
            is_pure_delegator=True,
        )
        session = _make_session([Turn(role=Role.USER, content="x")], base + timedelta(days=i))
        pairs.append((session, sig))
    result = assess_trajectory_heuristic(pairs)
    assert result.label == TrajectoryLabel.STABLE_PASSIVE


def test_detect_signal_kinds_why_question_only():
    """A turn that only matches the why-questions regex returns just
    'why_question' (no false-positives across the other kinds)."""
    turn = Turn(
        role=Role.USER,
        content="Why does this approach work for caching?",
    )
    kinds = detect_signal_kinds(turn)
    assert "why_question" in kinds


def test_detect_signal_kinds_telegraphic_short_prompt():
    """A very short prompt with no question mark or newline is
    telegraphic per the existing heuristic."""
    turn = Turn(role=Role.USER, content="add tests")
    kinds = detect_signal_kinds(turn)
    assert "telegraphic" in kinds


def test_detect_signal_kinds_pure_delegation_imperative_opener():
    """A turn that opens with an imperative verb fires pure_delegation."""
    turn = Turn(role=Role.USER, content="write me a function that sorts a list")
    kinds = detect_signal_kinds(turn)
    assert "pure_delegation" in kinds


def test_detect_signal_kinds_own_attempt_marker():
    """A 'my approach is...' opener fires the independence signal."""
    turn = Turn(
        role=Role.USER,
        content="My approach is to memoize the lookup, but I'd like a sanity check.",
    )
    kinds = detect_signal_kinds(turn)
    assert "own_attempt" in kinds


def test_detect_signal_kinds_returns_subset_of_panel_order():
    """The keys returned by detect_signal_kinds must all be members of
    SIGNAL_KINDS_IN_PANEL_ORDER so the adapter / renderer can iterate
    that tuple safely."""
    panel_kinds = set(SIGNAL_KINDS_IN_PANEL_ORDER)
    turn = Turn(role=Role.USER, content="why does this even work, fix this")
    for kind in detect_signal_kinds(turn):
        assert kind in panel_kinds


def test_detect_signal_kinds_can_match_multiple_kinds():
    """A single turn can match multiple signal kinds (e.g. an
    imperative request followed by 'why' phrasing)."""
    turn = Turn(
        role=Role.USER,
        content="fix this, and explain why it broke in the first place",
    )
    kinds = detect_signal_kinds(turn)
    # Both a debug outsourcing pattern AND an explanation request.
    assert "outsourced_debug" in kinds
    assert len(kinds) >= 2


# =========================================================================
# US-040: verification-calibration signal detection
# =========================================================================


def test_detect_verification_calibration_kinds_source_check():
    """A turn that asks for the source/citation fires source_check."""
    turn = Turn(
        role=Role.USER,
        content="What's the source for this claim? Can you cite a reference?",
    )
    kinds = detect_verification_calibration_kinds(turn)
    assert "source_check" in kinds


def test_detect_verification_calibration_kinds_test_run():
    """A turn that asks to run tests fires test_run."""
    turn = Turn(
        role=Role.USER,
        content="Let me run the tests and see if they pass.",
    )
    kinds = detect_verification_calibration_kinds(turn)
    assert "test_run" in kinds


def test_detect_verification_calibration_kinds_spot_check():
    """A 'let me double-check' turn fires spot_check."""
    turn = Turn(
        role=Role.USER,
        content="That looks right but let me double-check the boundary.",
    )
    kinds = detect_verification_calibration_kinds(turn)
    assert "spot_check" in kinds


def test_detect_verification_calibration_kinds_no_verification_returns_empty():
    """A turn with no verification activity returns an empty set
    (blanket_accept is the SESSION-level default, never per-turn)."""
    turn = Turn(role=Role.USER, content="write me a function")
    kinds = detect_verification_calibration_kinds(turn)
    assert kinds == set()


def test_categorize_session_verification_blanket_accept_default():
    """A session with no verification activity lands in blanket_accept."""
    turns = [
        Turn(role=Role.USER, content="write me a function"),
        Turn(role=Role.USER, content="make it handle errors"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert categorize_session_verification(session) == "blanket_accept"


def test_categorize_session_verification_spot_check():
    """A session that spot-checks but doesn't test or source-check
    lands in spot_check."""
    turns = [
        Turn(role=Role.USER, content="add error handling"),
        Turn(role=Role.USER, content="that looks right, let me double-check"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert categorize_session_verification(session) == "spot_check"


def test_categorize_session_verification_test_run_beats_spot_check():
    """When both test_run and spot_check fire across the session,
    the highest-rigor kind (test_run) wins."""
    turns = [
        Turn(role=Role.USER, content="that looks right"),
        Turn(role=Role.USER, content="let me run the tests"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert categorize_session_verification(session) == "test_run"


def test_categorize_session_verification_source_check_beats_test_run():
    """source_check trumps test_run when both fire across the session."""
    turns = [
        Turn(role=Role.USER, content="let me run the tests"),
        Turn(role=Role.USER, content="what's the source for this approach?"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert categorize_session_verification(session) == "source_check"


def test_categorize_session_verification_empty_session_blanket_accept():
    """A session with zero user turns falls into blanket_accept (no
    activity to derive a verification signal from)."""
    session = _make_session([], datetime.now(UTC))
    assert categorize_session_verification(session) == "blanket_accept"


# =========================================================================
# US-041: specification adoption, context engineering, knowledge gaps
# =========================================================================


def test_detect_spec_block_markdown_heading():
    """A session whose first user turn opens with a Markdown ## Goal
    heading fires the spec-block signal."""
    turns = [
        Turn(
            role=Role.USER,
            content=(
                "## Goal\n"
                "Refactor the auth middleware to use bearer tokens.\n"
                "## Constraints\n"
                "Must not break the existing session API."
            ),
        ),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert detect_spec_block(session) is True


def test_detect_spec_block_label_colon_form():
    """An inline ``Goal:`` / ``Acceptance criteria:`` label-colon form
    at line start also fires the spec-block signal."""
    turns = [
        Turn(
            role=Role.USER,
            content=(
                "Goal: ship the migration without downtime.\n"
                "Acceptance criteria: zero failed requests in the read replica."
            ),
        ),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert detect_spec_block(session) is True


def test_detect_spec_block_freeform_prompt_negative():
    """A short freeform implementation prompt does NOT fire even when it
    mentions a goal word in passing."""
    turns = [
        Turn(
            role=Role.USER,
            content="my goal is to be faster, just write me the function",
        ),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert detect_spec_block(session) is False


def test_detect_spec_block_only_first_turn_evaluated():
    """A spec block written mid-session (turn 2+) does not fire the
    signal; the panel measures session OPENINGS specifically."""
    turns = [
        Turn(role=Role.USER, content="write a function that sorts a list"),
        Turn(
            role=Role.USER,
            content=(
                "## Goal\nLet me try again with a structured spec.\n## Constraints\n- O(n log n)."
            ),
        ),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert detect_spec_block(session) is False


def test_detect_spec_block_empty_session():
    """A session with no user turns returns False - no opening to
    measure."""
    session = _make_session([], datetime.now(UTC))
    assert detect_spec_block(session) is False


def test_detect_scaffolding_kinds_claude_md():
    """Mentioning CLAUDE.md anywhere in the session fires the
    claude_md kind."""
    turns = [
        Turn(role=Role.USER, content="update CLAUDE.md to mention the new feature"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    kinds = detect_scaffolding_kinds(session)
    assert "claude_md" in kinds


def test_detect_scaffolding_kinds_agents_md_and_skills():
    """Multiple scaffolding kinds across multiple turns surface as a
    set; the detector reports presence, not frequency."""
    turns = [
        Turn(role=Role.USER, content="add a section in AGENTS.md about the rubric"),
        Turn(role=Role.USER, content="and a skills/code-review skill for the team"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    kinds = detect_scaffolding_kinds(session)
    assert "agents_md" in kinds
    assert "skills" in kinds


def test_detect_scaffolding_kinds_no_artifacts_returns_empty():
    """A session with no scaffolding references returns an empty set."""
    turns = [
        Turn(role=Role.USER, content="fix the failing test in module X"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    assert detect_scaffolding_kinds(session) == set()


def test_detect_scaffolding_kinds_returns_subset_of_panel_order():
    """Every kind returned must be a member of the panel-order tuple
    so the adapter and renderer can iterate it safely."""
    turns = [
        Turn(role=Role.USER, content="check CLAUDE.md and copilot-instructions.md"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    for kind in detect_scaffolding_kinds(session):
        assert kind in set(SCAFFOLDING_KINDS_IN_PANEL_ORDER)


def test_detect_knowledge_gap_kinds_missing_context():
    """A turn that references 'this function' without a code block
    fires missing_context."""
    turn = Turn(
        role=Role.USER,
        content="why is this function so slow? It used to be fast.",
    )
    kinds = detect_knowledge_gap_kinds(turn)
    assert "missing_context" in kinds


def test_detect_knowledge_gap_kinds_missing_context_negative_with_code():
    """A turn that references 'this function' AND includes a code
    block does NOT fire missing_context (the user supplied the code)."""
    turn = Turn(
        role=Role.USER,
        content=(
            "why is this function so slow?\n\n"
            "```python\n"
            "def slow():\n"
            "    return sum(range(10**6))\n"
            "```"
        ),
    )
    kinds = detect_knowledge_gap_kinds(turn)
    assert "missing_context" not in kinds


def test_detect_knowledge_gap_kinds_missing_specs():
    """An imperative build-something prompt with no acceptance
    criteria / done-when markers fires missing_specs."""
    turn = Turn(role=Role.USER, content="write me a function that handles auth")
    kinds = detect_knowledge_gap_kinds(turn)
    assert "missing_specs" in kinds


def test_detect_knowledge_gap_kinds_missing_specs_negative_with_criteria():
    """An imperative prompt that DOES carry acceptance criteria does
    NOT fire missing_specs."""
    turn = Turn(
        role=Role.USER,
        content=(
            "write me a sorter. acceptance criteria: stable, O(n log n), handles empty lists."
        ),
    )
    kinds = detect_knowledge_gap_kinds(turn)
    assert "missing_specs" not in kinds


def test_detect_knowledge_gap_kinds_multiple_context():
    """A turn that strings together multiple unrelated tasks via 'and
    also' fires multiple_context."""
    turn = Turn(
        role=Role.USER,
        content="write me the function, and also fix the failing test",
    )
    kinds = detect_knowledge_gap_kinds(turn)
    assert "multiple_context" in kinds


def test_detect_knowledge_gap_kinds_unclear_instructions():
    """A turn with vague verb-object phrasing fires
    unclear_instructions."""
    turn = Turn(
        role=Role.USER,
        content="do something with this codebase to make it better",
    )
    kinds = detect_knowledge_gap_kinds(turn)
    assert "unclear_instructions" in kinds


def test_detect_knowledge_gap_kinds_clear_prompt_no_gap():
    """A clear, well-scoped question with no gap markers returns an
    empty set."""
    turn = Turn(
        role=Role.USER,
        content="What does the LRU eviction policy do under thread contention?",
    )
    kinds = detect_knowledge_gap_kinds(turn)
    assert kinds == set()


def test_detect_knowledge_gap_kinds_returns_subset_of_panel_order():
    """Every key returned must be a member of the panel-order tuple."""
    turn = Turn(
        role=Role.USER,
        content="just write me something that handles this somehow",
    )
    for kind in detect_knowledge_gap_kinds(turn):
        assert kind in set(KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER)


def test_count_session_knowledge_gaps_always_includes_all_kinds():
    """The returned dict always has all four kinds as keys (US-041
    acceptance: no silent drops) even when a session has zero gaps."""
    turns = [
        Turn(
            role=Role.USER,
            content="What does the LRU eviction policy do under contention?",
        ),
    ]
    session = _make_session(turns, datetime.now(UTC))
    counts = count_session_knowledge_gaps(session)
    for kind in KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER:
        assert kind in counts
        assert counts[kind] == 0


def test_count_session_knowledge_gaps_accumulates_per_turn():
    """A session with multiple gap turns sums per-kind counts."""
    turns = [
        Turn(role=Role.USER, content="write me a function that handles auth"),
        Turn(role=Role.USER, content="build a token rotation cron"),
        Turn(role=Role.USER, content="do something with this codebase"),
    ]
    session = _make_session(turns, datetime.now(UTC))
    counts = count_session_knowledge_gaps(session)
    # Turns 1 + 2 are build-imperatives with no spec markers; turn 3
    # is vague-imperative.
    assert counts["missing_specs"] >= 2
    # Third turn fires unclear_instructions.
    assert counts["unclear_instructions"] >= 1


def test_count_session_knowledge_gaps_empty_session():
    """A session with no user turns returns zeros for every category."""
    session = _make_session([], datetime.now(UTC))
    counts = count_session_knowledge_gaps(session)
    assert counts == {kind: 0 for kind in KNOWLEDGE_GAP_KINDS_IN_PANEL_ORDER}


def test_llm_trajectory_returns_none_without_keys(tmp_home):
    # tmp_home fixture clears both API key env vars.
    pairs = []
    base = datetime.now(UTC) - timedelta(days=10)
    for i in range(6):
        sig = BehavioralSignals(
            user_turn_count=1,
            why_question_count=0,
            comprehension_check_count=0,
            explanation_request_count=0,
            pure_delegation_count=0,
            outsourced_debug_count=0,
            telegraphic_count=0,
            own_attempt_count=0,
            engagement_rate=0.5,
            delegation_rate=0.0,
            independence_rate=0.0,
            is_pure_delegator=False,
        )
        pairs.append(
            (_make_session([Turn(role=Role.USER, content="x")], base + timedelta(days=i)), sig)
        )
    assert assess_trajectory_with_llm(pairs) is None
    # Top-level assess should still produce a result via the heuristic fallback.
    assert assess(pairs).label != TrajectoryLabel.INSUFFICIENT_DATA


# =========================================================================
# US-042: tool/agent ladder signal detection
# =========================================================================


from praxis.behavior.signals import (
    LADDER_KINDS_IN_PANEL_ORDER,
    categorize_session_ladder_rung,
    detect_session_ladder_rungs,
)


def _ladder_session(turns: list[Turn]) -> Session:
    """Build a session for ladder tests, mirroring _make_session."""
    return _make_session(turns, datetime.now(UTC))


def test_detect_session_ladder_rungs_skills_marker():
    """A user turn that names a .skill artifact fires the skills kind."""
    turns = [
        Turn(role=Role.USER, content="please run my code-review.skill again"),
    ]
    session = _ladder_session(turns)
    kinds = detect_session_ladder_rungs(session)
    assert "skills" in kinds


def test_detect_session_ladder_rungs_hooks_marker():
    """A reference to a hooks/<name> file fires the hooks kind."""
    turns = [
        Turn(role=Role.USER, content="add hooks/pre-commit to lint before push"),
    ]
    session = _ladder_session(turns)
    kinds = detect_session_ladder_rungs(session)
    assert "hooks" in kinds


def test_detect_session_ladder_rungs_subagents_marker():
    """A 'subagent' mention fires the subagents kind."""
    turns = [
        Turn(role=Role.USER, content="spawn a subagent to handle the migration"),
    ]
    session = _ladder_session(turns)
    kinds = detect_session_ladder_rungs(session)
    assert "subagents" in kinds


def test_detect_session_ladder_rungs_tool_calls_on_assistant_turn():
    """An assistant turn carrying tool_calls fires the tools_on kind."""
    turns = [
        Turn(role=Role.USER, content="check the logs"),
        Turn(
            role=Role.ASSISTANT,
            content="Reading logs...",
            tool_calls=[{"name": "bash", "args": {"cmd": "tail logs"}}],
        ),
    ]
    session = _ladder_session(turns)
    kinds = detect_session_ladder_rungs(session)
    assert "tools_on" in kinds


def test_detect_session_ladder_rungs_prompt_only_returns_empty_set():
    """A session of pure-text prompts (no tools, no scaffolding) returns
    an empty set; prompt_only is the SESSION-level default, never a
    per-turn kind."""
    turns = [
        Turn(role=Role.USER, content="explain how a ring buffer works"),
        Turn(role=Role.ASSISTANT, content="A ring buffer uses..."),
    ]
    session = _ladder_session(turns)
    assert detect_session_ladder_rungs(session) == set()


def test_categorize_session_ladder_rung_prompt_only_default():
    """A session with no signals lands in prompt_only."""
    turns = [
        Turn(role=Role.USER, content="explain X"),
    ]
    session = _ladder_session(turns)
    assert categorize_session_ladder_rung(session) == "prompt_only"


def test_categorize_session_ladder_rung_subagents_beats_skills():
    """When multiple rungs fire, the highest-rung wins (subagents >
    skills > hooks > tools_on > prompt_only)."""
    turns = [
        Turn(
            role=Role.USER,
            content="run my code-review.skill and spawn a subagent for tests",
        ),
    ]
    session = _ladder_session(turns)
    assert categorize_session_ladder_rung(session) == "subagents"


def test_categorize_session_ladder_rung_hooks_beats_tools_on():
    """Hooks rank higher than tools_on, so a session that exercises
    both lands in hooks."""
    turns = [
        Turn(role=Role.USER, content="wire hooks/pre-tool-use to log"),
        Turn(
            role=Role.ASSISTANT,
            content="Wiring...",
            tool_calls=[{"name": "edit", "args": {}}],
        ),
    ]
    session = _ladder_session(turns)
    assert categorize_session_ladder_rung(session) == "hooks"


def test_categorize_session_ladder_rung_returns_member_of_panel_order():
    """The returned kind is always a member of LADDER_KINDS_IN_PANEL_ORDER."""
    turns = [Turn(role=Role.USER, content="nothing special here")]
    session = _ladder_session(turns)
    assert categorize_session_ladder_rung(session) in LADDER_KINDS_IN_PANEL_ORDER


def test_categorize_session_ladder_rung_empty_session():
    """A session with zero turns is prompt_only (the default)."""
    session = _ladder_session([])
    assert categorize_session_ladder_rung(session) == "prompt_only"
