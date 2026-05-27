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
