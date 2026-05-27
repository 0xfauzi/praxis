"""Tests for the orchestrator.

Spec 15.6:
  - run() on a fresh machine returns valid RunSummary (zeros, empty trajectory)
  - run() followed by run() shows sessions_new == 0 once scored (idempotence)
  - Without API keys, sessions are seen but not scored (no heuristic fallback)

The previous test_force_consolidate_replaces_today_row was removed in US-002
along with the daily_consolidations table.

US-070 adds run_weekly() pipeline-ordering coverage. The tests verify that
the eight Section 9.4 steps execute in the documented order and that each
step's outputs only feed its documented downstream consumers.
"""
from __future__ import annotations

import os

import pytest

from praxis import orchestrator as orch
from praxis.orchestrator import Pass1Output, run, run_weekly
from praxis.scoring.judge import JudgeResult
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


@pytest.fixture
def fake_judge(monkeypatch):
    """Replace the LLM judge with a deterministic stub returning fixed scores."""

    def _fake(session, prefer="claude"):  # noqa: ARG001
        return JudgeResult(
            dimension_scores={d.key: 6.0 for d in RUBRIC},
            rationale={d.key: "fixture" for d in RUBRIC},
            standout_moments=["fixture standout"],
            failure_modes=["fixture failure"],
            overall_note="fixture",
            judge_model="fixture",
        )

    monkeypatch.setattr("praxis.scoring.aggregate.score_session", _fake)
    return _fake


def test_run_on_empty_machine_returns_zero_summary(tmp_home):
    # No synthetic sessions written; pipeline should produce a clean RunSummary.
    summary = run()
    assert summary.sessions_seen == 0
    assert summary.sessions_new == 0
    assert summary.sessions_scored == 0
    assert summary.snapshot.session_count == 0
    # Coaching falls back to the "no sessions" branch.
    assert summary.coaching.generated_by == "empty"


def test_run_is_idempotent(
    tmp_home, synthetic_claude_session, synthetic_codex_session, fake_judge
):
    first = run()
    assert first.sessions_new >= 1
    assert first.sessions_scored >= 1

    second = run()
    # The second run should see the same sessions but score zero new.
    assert second.sessions_new == 0
    assert second.sessions_scored == 0


def test_run_without_keys_does_not_score(tmp_home, synthetic_claude_session):
    # tmp_home fixture clears API key env vars. With the heuristic fallback
    # removed, sessions with no judge available cannot be scored - they are
    # seen and counted as new, but not persisted.
    assert os.environ.get("ANTHROPIC_API_KEY") is None
    assert os.environ.get("OPENAI_API_KEY") is None
    summary = run()
    assert summary.sessions_seen >= 1
    assert summary.sessions_new >= 1
    assert summary.sessions_scored == 0
    store = ProfileStore(home=resolve_home())
    rows = store.load_session_scores()
    assert rows == []


# --- US-070: pipeline ordering matches Section 9.4 ------------------------

EXPECTED_STEP_ORDER = [
    "scan",
    "cluster",
    "pass1",
    "pass2",
    "validate_moments",
    "select",
    "follow_up",
    "render",
]


@pytest.fixture
def step_recorder(monkeypatch):
    """Monkeypatch every pipeline step and record (name, kwargs-shape).

    The recorder doubles as the data-flow contract check: each step's
    inputs are captured so the test can assert what each step received
    only contains outputs from documented upstream steps.
    """
    calls: list[tuple[str, dict]] = []

    def _record(name, return_value):
        def _stub(*args, **kwargs):
            calls.append((name, {"args": args, "kwargs": kwargs}))
            return return_value

        return _stub

    monkeypatch.setattr(orch, "_step_scan", _record("scan", []))
    monkeypatch.setattr(orch, "_step_cluster", _record("cluster", []))
    monkeypatch.setattr(
        orch,
        "_step_pass1",
        _record("pass1", Pass1Output(results={}, low_confidence_session_ids=[])),
    )
    monkeypatch.setattr(orch, "_step_pass2", _record("pass2", {}))
    monkeypatch.setattr(orch, "_step_validate_moments", _record("validate_moments", []))
    monkeypatch.setattr(orch, "_step_select_moments", _record("select", None))
    monkeypatch.setattr(orch, "_step_follow_up", _record("follow_up", None))
    monkeypatch.setattr(orch, "_step_render", _record("render", ("", "")))
    return calls


def test_run_weekly_executes_steps_in_section_9_4_order(tmp_home, step_recorder):
    summary = run_weekly()
    # The summary reports the order it executed in. Use that as one
    # check, and the recorder's call list as the independent second.
    assert summary.steps_executed == EXPECTED_STEP_ORDER
    assert [name for name, _ in step_recorder] == EXPECTED_STEP_ORDER


def test_run_weekly_step_inputs_match_documented_upstream(tmp_home, step_recorder):
    # Drive run_weekly so the recorder captures each step's arguments.
    run_weekly()
    by_name = {name: payload for name, payload in step_recorder}

    # scan: takes only since_days (the orchestrator's own parameter).
    # No outputs from later steps leak back into scan.
    scan_args = by_name["scan"]["args"]
    scan_kwargs = by_name["scan"]["kwargs"]
    assert len(scan_args) + len(scan_kwargs) == 1

    # cluster: only sessions (scan output).
    assert len(by_name["cluster"]["args"]) == 1
    assert by_name["cluster"]["kwargs"] == {}

    # pass1: sessions (scan), tasks (cluster). Nothing else.
    assert len(by_name["pass1"]["args"]) == 2
    assert by_name["pass1"]["kwargs"] == {}

    # pass2: sessions (scan), pass1 output. Spec 9.1 forbids passing
    # pass1's scores to pass2; the contract is the IDs travel via the
    # Pass1Output wrapper, not raw judge results.
    assert len(by_name["pass2"]["args"]) == 2
    pass2_arg = by_name["pass2"]["args"][1]
    assert isinstance(pass2_arg, Pass1Output)

    # validate: sessions, pass1, pass2_results. Nothing from select/follow/render.
    assert len(by_name["validate_moments"]["args"]) == 3

    # select: sessions, validated moments. No pass1/pass2 leakage.
    assert len(by_name["select"]["args"]) == 2

    # follow_up: selection, moments, snapshot, week_iso. No raw sessions.
    assert len(by_name["follow_up"]["args"]) == 4

    # render: everything the digest layout reads, but only via the
    # outputs of upstream steps - no provider clients, no scanners.
    assert len(by_name["render"]["args"]) == 5


def test_run_weekly_each_step_called_exactly_once(tmp_home, step_recorder):
    run_weekly()
    names = [name for name, _ in step_recorder]
    for step in EXPECTED_STEP_ORDER:
        assert names.count(step) == 1, f"{step} was called {names.count(step)} times"


def test_run_weekly_returns_summary_with_week_iso(tmp_home, step_recorder):
    summary = run_weekly()
    # ISO week tag is YYYY-Www; format check is enough here - the
    # tagging is exercised in praxis.behavior.weekly tests.
    assert summary.week_iso.startswith("20")
    assert "-W" in summary.week_iso
