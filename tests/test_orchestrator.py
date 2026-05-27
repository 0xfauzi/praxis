"""Tests for the orchestrator.

Spec 15.6:
  - run() on a fresh machine returns valid RunSummary (zeros, empty trajectory)
  - run() followed by run() shows sessions_new == 0 once scored (idempotence)
  - Without API keys, sessions are seen but not scored (no heuristic fallback)

Spec 9.1 / US-027: pass 1 runs on every session in the weekly window. The
orchestrator's pass-1 path is what these tests intercept (via ``fake_judge``).

The previous test_force_consolidate_replaces_today_row was removed in US-002
along with the daily_consolidations table.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from praxis.orchestrator import run
from praxis.scoring.judge import JudgeResult
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


@pytest.fixture
def fake_judge(monkeypatch):
    """Replace the pass-1 LLM judge with a deterministic stub.

    Spec §9.1: the weekly orchestrator's first judging step is pass 1
    (cheap-tier judge) on every session. We patch the pass-1 entrypoint
    that ``score_one_session_pass1`` calls so tests can run end-to-end
    without an API key.
    """

    def _fake(session, prefer="claude"):  # noqa: ARG001
        return JudgeResult(
            dimension_scores={d.key: 6.0 for d in RUBRIC},
            rationale={d.key: "fixture" for d in RUBRIC},
            standout_moments=["fixture standout"],
            failure_modes=["fixture failure"],
            overall_note="fixture",
            judge_model="fixture",
        )

    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass1", _fake)
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


def _write_synthetic_claude_session(
    root: Path, session_index: int, *, hours_ago: int = 1
) -> Path:
    """Write one minimal Claude JSONL into ``root`` and return its path.

    Helper for the pass-1 coverage tests below: builds N small sessions
    that all live in the same project directory so the scanner picks
    them up in one pass. We do not call the fixture-level helper
    because it always writes to a single fixed filename.
    """
    root.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    path = root / f"{session_id}.jsonl"
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    events = [
        {
            "type": "user",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "user",
                "content": f"Goal: session {session_index} task.",
            },
        },
        {
            "type": "assistant",
            "timestamp": (when + timedelta(seconds=10))
            .isoformat()
            .replace("+00:00", "Z"),
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": f"Working on session {session_index}."}],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


def test_pass1_runs_on_every_session_in_window(tmp_home, fake_judge):
    """US-027: every session in the weekly window receives a pass-1 judgment.

    Build a multi-session window with one session that has a tiny prompt
    and one that has a long prompt - the kind of split that a heuristic
    skip path (e.g. "short prompts are obvious; don't waste a judge call")
    would gate on. The orchestrator must judge all of them.
    """
    project_root = tmp_home / ".claude" / "projects" / "weekly-window"
    session_count = 8
    for i in range(session_count):
        _write_synthetic_claude_session(project_root, i)

    summary = run()
    assert summary.sessions_seen == session_count
    assert summary.sessions_new == session_count
    assert summary.sessions_scored == session_count


def test_pass1_no_heuristic_skip_path(tmp_home, monkeypatch):
    """US-027: no heuristic feature gates the pass-1 call.

    We verify by recording every Session the pass-1 judge sees - if any
    feature-based filter were inserted between the orchestrator and the
    judge, some sessions would be missing. We assert the recorded set
    matches the set of seen sessions exactly.
    """
    project_root = tmp_home / ".claude" / "projects" / "no-skip"
    session_count = 5
    for i in range(session_count):
        # Mix turn counts and prompt lengths to exercise common heuristic
        # candidates (turn_count < N, avg_prompt_chars < N).
        _write_synthetic_claude_session(project_root, i, hours_ago=i + 1)

    judged_ids: list[str] = []

    def _recording_pass1(session, prefer="claude"):  # noqa: ARG001
        judged_ids.append(session.stable_id)
        return JudgeResult(
            dimension_scores={d.key: 6.0 for d in RUBRIC},
            rationale={d.key: "fixture" for d in RUBRIC},
            standout_moments=[],
            failure_modes=[],
            overall_note="fixture",
            judge_model="fixture-cheap-tier",
        )

    monkeypatch.setattr(
        "praxis.scoring.aggregate.score_session_pass1", _recording_pass1
    )

    summary = run()
    assert summary.sessions_seen == session_count
    assert summary.sessions_scored == session_count
    assert len(judged_ids) == session_count
    assert len(set(judged_ids)) == session_count


def test_pass1_uses_cheap_tier_anthropic_model(tmp_home, monkeypatch):
    """US-027: pass 1 calls Claude with the cheap-tier model id, not the frontier."""
    project_root = tmp_home / ".claude" / "projects" / "pass1-model-check"
    _write_synthetic_claude_session(project_root, 0)

    seen_models: list[str] = []

    def _recording_with_claude(session, model="claude-opus-4-7"):  # noqa: ARG001
        seen_models.append(model)
        return JudgeResult(
            dimension_scores={d.key: 5.0 for d in RUBRIC},
            rationale={d.key: "x" for d in RUBRIC},
            standout_moments=[],
            failure_modes=[],
            overall_note="x",
            judge_model=model,
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "praxis.scoring.judge.score_with_claude", _recording_with_claude
    )

    summary = run()
    assert summary.sessions_scored == 1
    assert seen_models == ["claude-haiku-4-5"]


# ---------------------------------------------------------------------------
# US-028: low-confidence sessions escalate to the pass-2 frontier judge
# ---------------------------------------------------------------------------


def _pass1_result(*, confidence: str, judge_model: str = "pass-1-cheap") -> JudgeResult:
    """Pass-1-style JudgeResult fixture with the given self-confidence."""
    return JudgeResult(
        dimension_scores={d.key: 5.0 for d in RUBRIC},
        rationale={d.key: "pass1" for d in RUBRIC},
        standout_moments=["pass1 standout"],
        failure_modes=["pass1 failure"],
        overall_note="pass1 overall",
        judge_model=judge_model,
        confidence=confidence,  # type: ignore[arg-type]
    )


def _pass2_result() -> JudgeResult:
    """Pass-2-style JudgeResult with distinctive values so we can verify override."""
    return JudgeResult(
        dimension_scores={d.key: 9.0 for d in RUBRIC},
        rationale={d.key: "pass2-frontier" for d in RUBRIC},
        standout_moments=["pass2 standout"],
        failure_modes=["pass2 failure"],
        overall_note="pass2 overall",
        judge_model="pass-2-frontier",
        confidence="high",
    )


def test_pass2_escalates_only_low_confidence_sessions(tmp_home, monkeypatch):
    """US-028: pass 2 runs for low-confidence sessions, not medium or high.

    Builds a window where pass-1 returns a different confidence per session
    (medium / high / low). Pass-2 must be invoked exactly once - for the
    low-confidence session - and never for the others.
    """
    project_root = tmp_home / ".claude" / "projects" / "escalation-mix"
    for i in range(3):
        _write_synthetic_claude_session(project_root, i, hours_ago=i + 1)

    confidences = iter(["medium", "high", "low"])

    def _fake_pass1(session, prefer="claude"):  # noqa: ARG001
        return _pass1_result(confidence=next(confidences))

    pass2_calls: list[str] = []

    def _fake_pass2(session, prefer="claude"):  # noqa: ARG001
        pass2_calls.append(session.stable_id)
        return _pass2_result()

    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass1", _fake_pass1)
    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass2", _fake_pass2)

    summary = run()
    assert summary.sessions_scored == 3
    assert len(pass2_calls) == 1, (
        f"pass 2 should run only on the low-confidence session, got {len(pass2_calls)} call(s)"
    )


def test_pass2_result_overrides_pass1(tmp_home, monkeypatch):
    """US-028: pass-2 scores, rationale, and moments override pass-1 for that session."""
    project_root = tmp_home / ".claude" / "projects" / "override-check"
    _write_synthetic_claude_session(project_root, 0)

    def _fake_pass1(session, prefer="claude"):  # noqa: ARG001
        return _pass1_result(confidence="low")

    def _fake_pass2(session, prefer="claude"):  # noqa: ARG001
        return _pass2_result()

    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass1", _fake_pass1)
    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass2", _fake_pass2)

    run()

    store = ProfileStore(home=resolve_home())
    rows = store.load_session_scores()
    assert len(rows) == 1
    row = rows[0]
    # All dim scores come from pass 2 (9.0), not pass 1 (5.0).
    assert all(v == 9.0 for v in row["dimension_scores"].values())
    # The persisted judge_result carries pass-2 metadata, not pass-1.
    jr = row["judge_result"]
    assert jr["judge_model"] == "pass-2-frontier"
    assert jr["overall_note"] == "pass2 overall"
    assert jr["rationale"]["planning"] == "pass2-frontier"
    assert "pass2 standout" in jr["standout_moments"]
    assert "pass2 failure" in jr["failure_modes"]


def test_pass2_is_skipped_for_medium_confidence(tmp_home, monkeypatch):
    """US-028 negative path: a medium-confidence pass-1 result is the final score.

    The persisted row must reflect pass-1's output and the pass-2 entrypoint
    must not be invoked at all.
    """
    project_root = tmp_home / ".claude" / "projects" / "medium-only"
    _write_synthetic_claude_session(project_root, 0)

    def _fake_pass1(session, prefer="claude"):  # noqa: ARG001
        return _pass1_result(confidence="medium", judge_model="pass-1-medium")

    def _exploding_pass2(session, prefer="claude"):  # noqa: ARG001
        raise AssertionError("pass 2 should not run for medium confidence")

    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass1", _fake_pass1)
    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass2", _exploding_pass2)

    run()

    store = ProfileStore(home=resolve_home())
    rows = store.load_session_scores()
    assert len(rows) == 1
    assert rows[0]["judge_result"]["judge_model"] == "pass-1-medium"


def test_pass2_failure_keeps_pass1_score(tmp_home, monkeypatch):
    """If pass 2 returns None (no key, transient error), keep the pass-1 score.

    Spec: a session should never silently disappear; if escalation cannot
    run, pass 1's read is what we have, so persist it rather than dropping
    the session.
    """
    project_root = tmp_home / ".claude" / "projects" / "pass2-failure"
    _write_synthetic_claude_session(project_root, 0)

    def _fake_pass1(session, prefer="claude"):  # noqa: ARG001
        return _pass1_result(confidence="low", judge_model="pass-1-fallback")

    def _failing_pass2(session, prefer="claude"):  # noqa: ARG001
        return None

    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass1", _fake_pass1)
    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass2", _failing_pass2)

    summary = run()
    assert summary.sessions_scored == 1

    store = ProfileStore(home=resolve_home())
    rows = store.load_session_scores()
    assert len(rows) == 1
    assert rows[0]["judge_result"]["judge_model"] == "pass-1-fallback"


def test_pass2_call_does_not_include_pass1_outputs(tmp_home, monkeypatch):
    """AC: pass-2 prompts do not include pass-1 outputs.

    The pass-2 entrypoint is wrapped to capture its call signature and we
    assert it only receives the Session - never a JudgeResult, score dict,
    rationale, or moments from pass 1.
    """
    project_root = tmp_home / ".claude" / "projects" / "pass2-args"
    _write_synthetic_claude_session(project_root, 0)

    captured: list[tuple[tuple, dict]] = []

    def _fake_pass1(session, prefer="claude"):  # noqa: ARG001
        return _pass1_result(confidence="low")

    def _capturing_pass2(*args, **kwargs):
        captured.append((args, kwargs))
        return _pass2_result()

    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass1", _fake_pass1)
    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass2", _capturing_pass2)

    run()

    assert len(captured) == 1
    args, kwargs = captured[0]
    # Only the session goes in. No pass-1 JudgeResult is forwarded.
    assert len(args) == 1
    from praxis.models import Session as _Session
    assert isinstance(args[0], _Session)
    for v in list(args[1:]) + list(kwargs.values()):
        assert not isinstance(v, JudgeResult), "pass-2 must not receive a JudgeResult"
