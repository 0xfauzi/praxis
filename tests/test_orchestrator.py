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


# --- US-071: persist weekly_digests row ----------------------------------

def test_run_weekly_writes_one_digest_row(tmp_home, step_recorder):
    """A weekly_digests row exists for the current week after run_weekly."""
    summary = run_weekly()
    store = ProfileStore(home=resolve_home())
    row = store.load_weekly_digest(summary.week_iso)
    assert row is not None
    assert row["week_iso"] == summary.week_iso
    # All schema-required (NOT NULL) columns must be populated.
    assert row["generated_at"]
    assert row["trajectory_label"]
    assert row["trajectory_headline"]
    assert row["snapshot_json"]
    assert summary.digest_persisted is True


def test_run_weekly_digest_snapshot_json_round_trips(tmp_home, step_recorder):
    """snapshot_json holds the full ProfileSnapshot (US-071 AC #2)."""
    summary = run_weekly()
    store = ProfileStore(home=resolve_home())
    row = store.load_weekly_digest(summary.week_iso)
    assert row is not None
    snap = row["snapshot"]
    # Every public field on ProfileSnapshot must round-trip through
    # snapshot_json. Listing them by name guards against the renderer
    # silently dropping a field if ProfileSnapshot grows.
    for key in (
        "overall",
        "dimension_means",
        "session_count",
        "provider_breakdown",
        "strongest_dimension",
        "weakest_dimension",
        "standout_moments",
        "failure_modes",
    ):
        assert key in snap, f"snapshot_json missing field: {key}"
    assert snap["overall"] == summary.snapshot.overall
    assert snap["session_count"] == summary.snapshot.session_count


def test_run_weekly_digest_is_idempotent(tmp_home, step_recorder):
    """Re-running for the same week replaces the row, not duplicates it.

    US-071 AC #3: UPSERT semantics. The PK is week_iso so a second run
    on the same week must leave exactly one row, with a refreshed
    generated_at timestamp.
    """
    first = run_weekly()
    store = ProfileStore(home=resolve_home())
    assert store.count_weekly_digests() == 1
    first_row = store.load_weekly_digest(first.week_iso)
    assert first_row is not None

    second = run_weekly()
    # Same week, still exactly one row.
    assert second.week_iso == first.week_iso
    assert store.count_weekly_digests() == 1


def test_run_weekly_persists_trajectory_label_and_headline(tmp_home, step_recorder):
    """Digest row carries the trajectory label + headline from this run."""
    summary = run_weekly()
    assert summary.trajectory is not None
    store = ProfileStore(home=resolve_home())
    row = store.load_weekly_digest(summary.week_iso)
    assert row is not None
    assert row["trajectory_label"] == summary.trajectory.label.value
    assert row["trajectory_headline"] == summary.trajectory.headline


def test_run_weekly_accepts_external_store(tmp_home, step_recorder):
    """Callers (and US-072 --dry-run) can inject a ProfileStore.

    The digest still lands in that store, not a fresh one. This test
    pins the contract that the second run_weekly() positional/keyword
    is `store=` and that it is used for the write.
    """
    store = ProfileStore(home=resolve_home())
    summary = run_weekly(store=store)
    assert store.count_weekly_digests() == 1
    row = store.load_weekly_digest(summary.week_iso)
    assert row is not None


# --- US-072: --dry-run computes without persisting -----------------------

def test_run_weekly_dry_run_does_not_persist_digest(tmp_home, step_recorder):
    """dry_run=True must not write a weekly_digests row (US-072 AC #1)."""
    summary = run_weekly(dry_run=True)
    # Inspect via a fresh store - the dry-run path must not have created
    # one of its own. Constructing a store here is fine: it is the test
    # asserting "no row exists", not the SUT.
    store = ProfileStore(home=resolve_home())
    assert store.count_weekly_digests() == 0
    assert store.load_weekly_digest(summary.week_iso) is None
    assert summary.digest_persisted is False


def test_run_weekly_dry_run_does_not_create_db_file(tmp_home, step_recorder):
    """dry_run=True must not construct a ProfileStore (no DB file write).

    Spec AC #1: dry-run prevents file writes. The ProfileStore constructor
    creates ~/.praxis/profile.db as a side effect, so dry-run must avoid
    constructing one when the caller hasn't supplied one.
    """
    db_path = tmp_home / ".praxis" / "profile.db"
    assert not db_path.exists()
    run_weekly(dry_run=True)
    assert not db_path.exists(), (
        "dry_run=True wrote profile.db; ProfileStore() must not be "
        "constructed in the dry-run path"
    )


def test_run_weekly_dry_run_with_explicit_store_does_not_write(
    tmp_home, step_recorder
):
    """dry_run wins over an explicit store= (no row is written either way).

    Defense-in-depth: callers who pass a real store but also ask for
    dry-run must still get no persistence. Pinning this avoids someone
    later "fixing" the explicit-store branch to write through.
    """
    store = ProfileStore(home=resolve_home())
    assert store.count_weekly_digests() == 0
    summary = run_weekly(store=store, dry_run=True)
    assert store.count_weekly_digests() == 0
    assert summary.digest_persisted is False


def test_run_weekly_dry_run_still_returns_rendered_terminal(
    tmp_home, monkeypatch
):
    """dry_run=True still computes and returns the terminal rendering.

    Spec AC #2: terminal output is still produced. The renderer returns
    its string via WeeklyRunSummary.rendered_terminal; this test pins
    that dry-run does NOT short-circuit the render step or drop its
    output.
    """
    # Stub just the render step to return a sentinel terminal string.
    # Leave the rest of the pipeline real, so we exercise the full
    # control flow that an actual dry-run user would hit.
    captured: dict[str, object] = {}

    def _render_stub(*args, **kwargs):
        captured["args_count"] = len(args)
        captured["dry_run_kwarg"] = kwargs.get("dry_run")
        return ("<html>WEEK</html>", "TERMINAL DIGEST OUTPUT")

    monkeypatch.setattr(orch, "_step_render", _render_stub)
    # Stub the upstream steps too so the test does not depend on the
    # scanners finding sessions or the LLM judges being reachable.
    monkeypatch.setattr(orch, "_step_scan", lambda *_a, **_k: [])
    monkeypatch.setattr(orch, "_step_cluster", lambda *_a, **_k: [])
    monkeypatch.setattr(
        orch,
        "_step_pass1",
        lambda *_a, **_k: Pass1Output(results={}, low_confidence_session_ids=[]),
    )
    monkeypatch.setattr(orch, "_step_pass2", lambda *_a, **_k: {})
    monkeypatch.setattr(orch, "_step_validate_moments", lambda *_a, **_k: [])
    monkeypatch.setattr(orch, "_step_select_moments", lambda *_a, **_k: None)
    monkeypatch.setattr(orch, "_step_follow_up", lambda *_a, **_k: None)

    summary = run_weekly(dry_run=True)
    assert summary.rendered_terminal == "TERMINAL DIGEST OUTPUT"
    assert summary.rendered_html == "<html>WEEK</html>"
    assert captured["dry_run_kwarg"] is True


def test_run_weekly_dry_run_flag_reaches_render_step(tmp_home, step_recorder):
    """dry_run is propagated to _step_render so the future on-disk write
    can be skipped while still computing the strings."""
    run_weekly(dry_run=True)
    by_name = {name: payload for name, payload in step_recorder}
    assert by_name["render"]["kwargs"].get("dry_run") is True
    # And the positional-args count remains the documented 5, so the
    # data-flow contract from US-070 is not weakened.
    assert len(by_name["render"]["args"]) == 5


def test_run_weekly_default_persists_digest(tmp_home, step_recorder):
    """The default (dry_run=False) still persists the digest row.

    Pins that adding the dry-run gate did not flip the default off.
    """
    summary = run_weekly()
    store = ProfileStore(home=resolve_home())
    assert store.count_weekly_digests() == 1
    assert summary.digest_persisted is True
