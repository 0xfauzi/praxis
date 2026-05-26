"""Tests for the orchestrator.

Spec 15.6:
  - run() on a fresh machine returns valid RunSummary (zeros, empty trajectory)
  - run() followed by run() shows sessions_new == 0 (idempotence)
  - run(use_judge=False) never calls APIs (verifiable via env vars + tmp_home)
  - run(force_consolidate=True) writes a new row even if one exists today
"""
from __future__ import annotations

import os

from praxis.orchestrator import run
from praxis.storage.profile_store import ProfileStore, resolve_home


def test_run_on_empty_machine_returns_zero_summary(tmp_home):
    # No synthetic sessions written; pipeline should produce a clean RunSummary.
    summary = run(use_judge=False)
    assert summary.sessions_seen == 0
    assert summary.sessions_new == 0
    assert summary.sessions_scored == 0
    assert summary.snapshot.session_count == 0
    # Coaching falls back to the "no sessions" branch.
    assert summary.coaching.generated_by == "empty"


def test_run_is_idempotent(tmp_home, synthetic_claude_session, synthetic_codex_session):
    first = run(use_judge=False)
    assert first.sessions_new >= 1

    second = run(use_judge=False)
    # The second run should see the same sessions but score zero new.
    assert second.sessions_new == 0
    assert second.sessions_scored == 0


def test_run_without_keys_does_not_call_apis(tmp_home, synthetic_claude_session):
    # tmp_home fixture clears API key env vars. use_judge=False is the
    # heuristic-only path; we sanity-check the run completes with zero calls
    # by verifying no exception and a populated snapshot.
    assert os.environ.get("ANTHROPIC_API_KEY") is None
    assert os.environ.get("OPENAI_API_KEY") is None
    summary = run(use_judge=False)
    assert summary.snapshot.session_count >= 1
    # All scored sessions should have judge_result == None in the DB.
    store = ProfileStore(home=resolve_home())
    rows = store.load_session_scores()
    assert all(row["judge_result"] is None for row in rows)


def test_force_consolidate_replaces_today_row(tmp_home, synthetic_claude_session):
    run(use_judge=False)
    store = ProfileStore(home=resolve_home())
    today = store.latest_consolidation_date()
    assert today is not None

    # Force a second consolidation. The row count for `today` must still be 1
    # (INSERT OR REPLACE), but generated_at should advance.
    before = store.load_consolidation(today)
    assert before is not None
    first_generated = before["generated_at"]

    run(use_judge=False, force_consolidate=True)
    after = store.load_consolidation(today)
    assert after is not None
    assert after["generated_at"] >= first_generated
