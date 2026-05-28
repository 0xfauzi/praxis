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

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from praxis import orchestrator as orch
from praxis.models import Moment as JudgeMoment
from praxis.orchestrator import Pass1Output, run, run_weekly
from praxis.scoring.clustering import Task
from praxis.scoring.judge import JudgeResult
from praxis.scoring.moment_selector import MomentSelection
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


@pytest.fixture
def fake_judge(monkeypatch):
    """Replace the LLM judge with a deterministic stub returning fixed scores.

    Patches both pass-1 (cheap) and pass-2 (frontier) entrypoints so the
    weekly orchestrator path picks up the stub regardless of which pass
    a given test exercises.
    """

    def _fake(session, prefer="claude", **kwargs):  # noqa: ARG001
        return JudgeResult(
            dimension_scores={d.key: 6.0 for d in RUBRIC},
            rationale={d.key: "fixture" for d in RUBRIC},
            standout_moments=["fixture standout"],
            failure_modes=["fixture failure"],
            overall_note="fixture",
            judge_model="fixture",
        )

    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass1", _fake)
    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass2", _fake)
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

    # pass1: sessions (scan), tasks (cluster). The only kwarg is the
    # CLI-level frontier_only switch (spec §9.6), which is a static
    # mode flag, not data flowing back from a later step.
    assert len(by_name["pass1"]["args"]) == 2
    assert set(by_name["pass1"]["kwargs"].keys()) <= {"frontier_only"}

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


# --- US-073: perf target on 30-session week ------------------------------

# Spec 15.2 / US-073 AC: a 30-session synthetic week must complete in <120s
# wall time and project <$2 in LLM spend. The test stubs every LLM call so
# the elapsed time measures only the in-process pipeline (scanner I/O, the
# orchestrator's own bookkeeping, follow-up template, persistence), and the
# cost figure measures what those calls would have *cost* given their
# input/output token volumes and each judge's recorded model. With stubs in
# place the real headroom under the 120s gate is huge - that is by design;
# the gate exists to catch regressions where the pipeline accidentally grows
# an O(N^2) loop or a synchronous network call we forgot to mock.

PERF_WALL_TIME_LIMIT_S = 120.0
PERF_COST_LIMIT_USD = 2.0


def _write_synthetic_claude_session(home, when: datetime, content: str) -> None:
    """Drop one Claude JSONL into tmp_home so the scanner picks it up.

    Mirrors the shape of `synthetic_claude_session` in conftest.py but
    parameterized so the perf fixture can produce 30 distinct files.
    Each session has 4 turns of realistic length (~150-250 chars), which
    keeps the per-session compact transcript big enough to make the cost
    estimator's contribution non-trivial without being unrealistic.
    """
    root = home / ".claude" / "projects" / "perf-30-sessions"
    root.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    path = root / f"{session_id}.jsonl"
    events = [
        {
            "type": "user",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": content},
        },
        {
            "type": "assistant",
            "timestamp": (when + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{
                    "type": "text",
                    "text": (
                        "Plan: identify the failing assertion, trace the data "
                        "model back to the producer, and add a regression test."
                    ),
                }],
            },
        },
        {
            "type": "user",
            "timestamp": (when + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "user",
                "content": (
                    "Walk me through the trade-off. I want to understand why "
                    "this approach is safer than the alternative we tried."
                ),
            },
        },
        {
            "type": "assistant",
            "timestamp": (when + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{
                    "type": "text",
                    "text": (
                        "Because the audit_log writes are async, the old path "
                        "could commit the SQL while the audit was still in "
                        "flight - so a crash mid-flush dropped the trail."
                    ),
                }],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def thirty_synthetic_sessions(tmp_home):
    """Write 30 Claude JSONL files spread across the past week.

    Spreading the mtimes keeps the scanner's `since=` filter happy and
    matches the distribution a real user would have. The exact spacing
    is not load-bearing; the test only cares that 30 files exist within
    the orchestrator's default `since_days=7` window.
    """
    now = datetime.now(timezone.utc)
    for i in range(30):
        # ~5 hours between sessions keeps everything inside a 7-day window.
        when = now - timedelta(hours=5 * i + 1)
        _write_synthetic_claude_session(
            tmp_home,
            when,
            (
                f"Goal: refactor segment {i} of the auth module. "
                f"Constraints: keep CI green and audit_log writes synchronous."
            ),
        )


@pytest.fixture
def stub_weekly_llm_calls(monkeypatch):
    """Replace every LLM call the weekly pipeline reaches with a fast stub.

    Real API calls are not allowed in tests (no keys, slow, expensive).
    These stubs return realistic-shaped objects so the cost estimator's
    per-call sizing remains representative of production: the judge
    rationale lengths feed the output-token estimate, and the moment
    excerpts are taken verbatim from the synthetic transcripts so the
    substring verifier keeps them (otherwise the moment_count argument
    to the cost estimator would always be 0 and the selector wedge of
    the estimate would vanish).
    """
    # Every 5th session gets a low-confidence flag so pass 2 actually
    # runs - exercising the more expensive frontier wedge of the cost
    # model. 6/30 escalation rate sits inside the spec 15.2 #3 band
    # (15-30%) which the calibration target uses on real data.
    def _fake_score(session, prefer="claude"):  # noqa: ARG001
        # Recognizable substring from the synthetic transcripts above so
        # the moment survives the post-judge substring verifier.
        excerpt = "Walk me through the trade-off"
        confidence = "low" if hash(session.stable_id) % 5 == 0 else "medium"
        # Pass 2 uses the frontier model; pass 1 uses the cheap tier.
        # We can't tell which call this is from inside the stub, so we
        # use the session_stable_id hash as a deterministic proxy: a
        # session flagged "low" by pass 1 will get re-judged by pass 2
        # with a different model. To approximate that, the stub returns
        # a heavier judge_model when the session would escalate.
        judge_model = (
            "claude-opus-4-7" if confidence == "low" else "claude-haiku-4-5"
        )
        return JudgeResult(
            dimension_scores={d.key: 6.0 for d in RUBRIC},
            rationale={
                d.key: (
                    f"The user's {d.key} work was workmanlike: clear goal "
                    f"statement and some context but no explicit success criteria."
                )
                for d in RUBRIC
            },
            standout_moments=["asked for trade-off rationale before committing"],
            failure_modes=["did not verify audit_log write semantics"],
            overall_note=(
                "Solid mid-week session. The user asked good clarifying "
                "questions but accepted the SQL block without re-reading it."
            ),
            judge_model=judge_model,
            moments=[
                JudgeMoment(
                    dim_key="verification",
                    turn_index=1,
                    quoted_excerpt=excerpt,
                    why_it_lost_score=(
                        "accepted assistant explanation without checking the "
                        "audit_log write semantics"
                    ),
                    suggested_alternative=(
                        "ask the assistant to point to the exact lines that "
                        "make the writes synchronous before accepting"
                    ),
                    severity="moderate",
                )
            ],
            confidence=confidence,
            confidence_reason="single-session signal",
        )

    monkeypatch.setattr("praxis.scoring.aggregate.score_session", _fake_score)

    def _fake_cluster(sessions, prefer="anthropic"):  # noqa: ARG001
        # One realistic-shaped Task; coverage validation in cluster_sessions
        # is not exercised here (the orchestrator only consumes the result).
        return [
            Task(
                label="auth module refactor",
                task_type="refactoring",
                session_ids=[s.stable_id for s in sessions],
                rationale="all sessions share the audit_log/auth-module goal",
                label_source="llm",
            )
        ]

    monkeypatch.setattr(orch, "cluster_sessions", _fake_cluster)

    def _fake_select(candidates, primary_provider="anthropic", *, llm_caller=None):  # noqa: ARG001
        if not candidates:
            return None
        return MomentSelection(
            headline_moment_id=candidates[0].moment.moment_id,
            headline_reason="largest verification slip this week",
            supporting_moment_ids=[
                c.moment.moment_id for c in candidates[1:3]
            ],
        )

    monkeypatch.setattr(orch, "select_moments_with_fallback", _fake_select)


def test_run_weekly_perf_30_sessions_under_120s_and_2_usd(
    thirty_synthetic_sessions, stub_weekly_llm_calls
):
    """Spec 15.2 / US-073 AC #1-2: 30-session week is <120s wall, <$2 spend.

    AC #3 ("the perf test records the measurement") is satisfied by the
    print statement at the bottom: pytest captures stdout and surfaces
    it on `-s` / `-rA`, and it is included in the assert messages so a
    failure on either gate also records the measured numbers.
    """
    started = time.monotonic()
    summary = run_weekly()
    wall_elapsed = time.monotonic() - started

    # Sanity: the scanner found all 30 files we wrote.
    assert len(summary.sessions) == 30, (
        f"scanner discovered {len(summary.sessions)} sessions, expected 30"
    )

    # AC #1: <120s wall time. Check both the externally-measured wall time
    # (this test's scope) and the summary's own self-reported elapsed so a
    # regression in either the pipeline OR the timing instrumentation
    # surfaces here.
    assert wall_elapsed < PERF_WALL_TIME_LIMIT_S, (
        f"30-session run_weekly wall time {wall_elapsed:.2f}s "
        f"exceeds spec 15.2 limit of {PERF_WALL_TIME_LIMIT_S}s"
    )
    assert summary.elapsed_seconds < PERF_WALL_TIME_LIMIT_S, (
        f"WeeklyRunSummary.elapsed_seconds={summary.elapsed_seconds}s "
        f"exceeds spec 15.2 limit of {PERF_WALL_TIME_LIMIT_S}s"
    )

    # AC #2: <$2 projected spend. cost_total_usd is None only when no
    # priced calls happened, which would mean the stubs above silently
    # broke. Treat that as a perf-test failure too.
    assert summary.cost_total_usd is not None, (
        "cost_total_usd was None - the cost estimator saw no priced calls, "
        "which usually means the LLM stubs are returning unpriced model ids"
    )
    assert summary.cost_total_usd < PERF_COST_LIMIT_USD, (
        f"30-session run_weekly projected ${summary.cost_total_usd:.4f} in "
        f"LLM spend, exceeds spec 15.2 limit of ${PERF_COST_LIMIT_USD:.2f}"
    )

    # AC #3: record the measurement. Pytest captures stdout by default;
    # `pytest -s` or `-rA` surface this line in CI logs.
    print(
        f"[perf] run_weekly(30 sessions): "
        f"wall_elapsed={wall_elapsed:.3f}s, "
        f"summary.elapsed_seconds={summary.elapsed_seconds}s, "
        f"cost_total_usd=${summary.cost_total_usd:.4f}"
    )


def test_run_weekly_cost_total_usd_is_persisted(
    thirty_synthetic_sessions, stub_weekly_llm_calls
):
    """The estimated cost lands in weekly_digests.cost_total_usd.

    US-073's perf target is only useful if the number gets stored alongside
    the digest so it can feed the cost ledger panel in spec 10.1. This pins
    the persistence contract: the estimator's output must reach the row.
    """
    summary = run_weekly()
    store = ProfileStore(home=resolve_home())
    row = store.load_weekly_digest(summary.week_iso)
    assert row is not None
    assert row["cost_total_usd"] is not None
    assert row["cost_total_usd"] == pytest.approx(summary.cost_total_usd)


def test_run_weekly_cost_baseline_uses_prior_weekly_digests(tmp_home, step_recorder):
    """spec 10.1: cost_baseline_usd is the rolling weekly mean of prior runs.

    Seeds two prior digests with known costs, then runs the orchestrator and
    confirms the new row's cost_baseline_usd is the mean of the two priors.
    The current run is excluded by week_iso (the helper filters strictly <).
    """
    store = ProfileStore(home=resolve_home())
    # Seed two prior weeks with $0.50 and $1.00 spend.
    snapshot = orch.ProfileSnapshot.from_scores([])
    store.save_weekly_digest(
        week_iso="2026-W19",
        trajectory_label="steady",
        trajectory_headline="steady week",
        snapshot=snapshot,
        cost_total_usd=0.50,
    )
    store.save_weekly_digest(
        week_iso="2026-W20",
        trajectory_label="steady",
        trajectory_headline="steady week",
        snapshot=snapshot,
        cost_total_usd=1.00,
    )
    summary = run_weekly(store=store)
    row = store.load_weekly_digest(summary.week_iso)
    assert row is not None
    # Mean of 0.50 and 1.00 = 0.75. The current week is NOT included in
    # its own baseline (the helper's WHERE week_iso < ? filter).
    assert row["cost_baseline_usd"] == pytest.approx(0.75)


def test_run_weekly_cost_baseline_is_none_on_first_run(tmp_home, step_recorder):
    """First-ever digest has no prior weeks, so cost_baseline_usd is NULL.

    The renderer (spec 8.4 logic applied to cost) shows "--" in that case
    and skips the delta. Pinning None (not 0.0) here keeps that branch live.
    """
    summary = run_weekly()
    store = ProfileStore(home=resolve_home())
    row = store.load_weekly_digest(summary.week_iso)
    assert row is not None
    assert row["cost_baseline_usd"] is None


def test_run_weekly_dry_run_does_not_compute_baseline(tmp_home, step_recorder):
    """Dry-run skips the persistence block, so cost_baseline_usd stays None.

    The baseline read is in the `if not dry_run` block alongside the write,
    by design: dry-run callers do not need the baseline because they will
    not persist a row. This pins the contract so a future refactor cannot
    accidentally read from the store under dry-run.
    """
    # Seed a prior digest with known cost so the baseline read WOULD have
    # picked it up if dry-run were not blocking the read.
    store = ProfileStore(home=resolve_home())
    snapshot = orch.ProfileSnapshot.from_scores([])
    store.save_weekly_digest(
        week_iso="2026-W18",
        trajectory_label="steady",
        trajectory_headline="prior week",
        snapshot=snapshot,
        cost_total_usd=1.50,
    )
    summary = run_weekly(dry_run=True)
    # No row was written this week (dry-run), and the in-memory summary
    # carries cost_baseline_usd=None because the read was skipped.
    assert summary.digest_persisted is False
    assert summary.cost_baseline_usd is None


# --- US-010: aug_auto classifier integration in pass-1 -------------------

from praxis.behavior.aug_auto import (
    AugAutoParseError,
    AugAutoResult,
    AugAutoUnavailableError,
)
from praxis.scoring.features import SessionFeatures


def _build_session_score_for(session, *, dim_value: float = 6.0) -> orch.SessionScore:
    """Construct a minimal SessionScore for the per-session pass-1 stub.

    The orchestrator only reads ``judge_result.confidence`` and persists
    the score; dimension values do not affect aug_auto-specific assertions.
    """
    judge = JudgeResult(
        dimension_scores={d.key: dim_value for d in RUBRIC},
        rationale={d.key: "stub" for d in RUBRIC},
        standout_moments=["stub"],
        failure_modes=["stub"],
        overall_note="stub",
        judge_model="stub-model",
    )
    return orch.SessionScore(
        session_stable_id=session.stable_id,
        provider=session.provider.value,
        started_at=session.started_at,
        dimension_scores={d.key: dim_value for d in RUBRIC},
        overall=dim_value,
        judge_result=judge,
        features=SessionFeatures(turn_count=len(session.turns), avg_prompt_chars=50.0),
        source_path=session.source_path,
        judge_pass=1,
    )


@pytest.fixture
def stub_pass1_judge(monkeypatch):
    """Replace ``score_one_session_pass1`` with a fixed-shape SessionScore.

    The aug_auto integration tests want the orchestrator to reach the
    "score saved, now classify" branch deterministically without hitting
    the real judge LLM or routing through API-key gating.
    """

    def _fake(session, *, sharpen_calibration=False, stricter_low=False):  # noqa: ARG001
        return _build_session_score_for(session)

    monkeypatch.setattr(orch, "score_one_session_pass1", _fake)
    return _fake


def test_step_pass1_invokes_classifier_per_session(
    tmp_home, synthetic_session_object, stub_pass1_judge, monkeypatch
):
    """The classifier is called once per session that pass-1 successfully scores."""
    calls: list[str] = []

    def _fake_classify(transcript_text: str) -> AugAutoResult:
        calls.append(transcript_text)
        return AugAutoResult(
            classification="augmentation", confidence=0.8, rationale="stub"
        )

    monkeypatch.setattr(orch, "classify_session", _fake_classify)
    sessions = [synthetic_session_object]
    pass1 = orch._step_pass1(sessions, tasks=[])
    assert len(calls) == 1
    # The transcript text is non-empty and contains a user turn marker.
    assert "<user>" in calls[0]
    # The judge ran too: pass1.results carries the session's stub JudgeResult.
    assert synthetic_session_object.stable_id in pass1.results


def test_step_pass1_persists_classification_to_session_scores(
    tmp_home, synthetic_session_object, stub_pass1_judge, monkeypatch
):
    """On a successful classify, the aug_auto columns hold the result."""

    def _fake_classify(transcript_text: str) -> AugAutoResult:  # noqa: ARG001
        return AugAutoResult(
            classification="mixed", confidence=0.55, rationale="stub"
        )

    monkeypatch.setattr(orch, "classify_session", _fake_classify)
    orch._step_pass1([synthetic_session_object], tasks=[])
    store = ProfileStore(home=resolve_home())
    classification, confidence = store.get_session_aug_auto(synthetic_session_object.stable_id)
    assert classification == "mixed"
    assert confidence == pytest.approx(0.55)


def test_step_pass1_no_api_key_leaves_aug_auto_null_and_logs_once(
    tmp_home, synthetic_claude_session, synthetic_codex_session,
    stub_pass1_judge, monkeypatch, capsys
):
    """No API key for classifier: NULL columns, one info-level log per run.

    The orchestrator's other LLM calls (judge, selector) gate on the key
    separately; this test pins the AC for the classifier side-channel:
    the run does not abort and the classifier columns stay NULL while
    only a single message is emitted.
    """
    # Stub classifier to behave as if no API key was set.
    def _fake_classify(transcript_text: str) -> AugAutoResult:  # noqa: ARG001
        raise AugAutoUnavailableError(
            "no API key configured for aug_auto classifier"
        )

    monkeypatch.setattr(orch, "classify_session", _fake_classify)
    # Use two sessions so we can assert "logged once," not once per session.
    from praxis.scanners import ALL_SCANNERS
    sessions = []
    for scanner_cls in ALL_SCANNERS:
        for s in scanner_cls().scan(since=None):
            sessions.append(s)
    sessions = [s for s in sessions if s.user_turns]
    assert len(sessions) >= 2, "fixtures should produce >=2 sessions"

    orch._step_pass1(sessions, tasks=[])

    captured = capsys.readouterr()
    log_line_count = captured.err.count("aug_auto classifier unavailable")
    assert log_line_count == 1, (
        f"expected one unavailability log line per run, got {log_line_count}"
    )

    store = ProfileStore(home=resolve_home())
    for s in sessions:
        assert store.get_session_aug_auto(s.stable_id) == (None, None), (
            f"session {s.stable_id} should have null aug_auto columns"
        )


def test_step_pass1_parse_error_leaves_aug_auto_null_and_logs(
    tmp_home, synthetic_session_object, stub_pass1_judge, monkeypatch, capsys
):
    """AugAutoParseError: NULL columns, error message logged, run continues."""

    def _fake_classify(transcript_text: str) -> AugAutoResult:  # noqa: ARG001
        raise AugAutoParseError(
            "classifier response is not valid JSON: synthetic test failure"
        )

    monkeypatch.setattr(orch, "classify_session", _fake_classify)
    pass1 = orch._step_pass1([synthetic_session_object], tasks=[])

    # The pass-1 judge result still landed - the parse failure must not
    # abort the rest of the per-session processing.
    assert synthetic_session_object.stable_id in pass1.results

    captured = capsys.readouterr()
    assert "aug_auto classifier parse error" in captured.err
    assert "synthetic test failure" in captured.err, (
        "the rationale (exception message) must be logged so the user "
        "can diagnose without re-running"
    )

    store = ProfileStore(home=resolve_home())
    assert store.get_session_aug_auto(synthetic_session_object.stable_id) == (None, None)


def test_step_pass1_classifier_failure_does_not_abort_other_sessions(
    tmp_home, synthetic_claude_session, synthetic_codex_session,
    stub_pass1_judge, monkeypatch
):
    """One session's parse error must not stop the other session's classify."""
    from praxis.scanners import ALL_SCANNERS
    sessions = []
    for scanner_cls in ALL_SCANNERS:
        for s in scanner_cls().scan(since=None):
            sessions.append(s)
    sessions = [s for s in sessions if s.user_turns]
    assert len(sessions) >= 2

    # First session through the door raises, all others succeed. Use a
    # mutable counter rather than a per-id branch so the stub is simple.
    state = {"calls": 0}

    def _fake_classify(transcript_text: str) -> AugAutoResult:  # noqa: ARG001
        state["calls"] += 1
        if state["calls"] == 1:
            raise AugAutoParseError("first session intentionally broken")
        return AugAutoResult(
            classification="automation", confidence=0.7, rationale="ok"
        )

    monkeypatch.setattr(orch, "classify_session", _fake_classify)
    orch._step_pass1(sessions, tasks=[])

    store = ProfileStore(home=resolve_home())
    classified = 0
    for s in sessions:
        classification, _ = store.get_session_aug_auto(s.stable_id)
        if classification is not None:
            classified += 1
    # At least one session classified successfully (every session except the
    # first as_completed result); the broken session leaves NULL behind.
    assert classified >= 1


def test_step_pass1_frontier_only_skips_classifier(
    tmp_home, synthetic_session_object, monkeypatch
):
    """In --frontier-only mode pass-1 (and therefore the classifier) is bypassed.

    The AC ties the classifier to pass-1 specifically; when pass-1 is
    skipped, the classifier is skipped too, leaving aug_auto NULL.
    """
    called = {"hit": False}

    def _fake_classify(transcript_text: str) -> AugAutoResult:  # noqa: ARG001
        called["hit"] = True
        return AugAutoResult(
            classification="augmentation", confidence=0.9, rationale="x"
        )

    monkeypatch.setattr(orch, "classify_session", _fake_classify)
    orch._step_pass1([synthetic_session_object], tasks=[], frontier_only=True)
    assert called["hit"] is False


def test_save_and_get_session_aug_auto_round_trip(tmp_home):
    """ProfileStore helpers round-trip the classifier output.

    save_session_aug_auto updates an existing session_scores row; the
    test seeds one via save_session_score first, then reads back.
    """
    store = ProfileStore(home=resolve_home())
    # Build a row by seeding session_scores directly via the SessionScore
    # path. We don't have a real session here; just use a minimal score.
    judge = JudgeResult(
        dimension_scores={d.key: 5.0 for d in RUBRIC},
        rationale={d.key: "x" for d in RUBRIC},
        standout_moments=[],
        failure_modes=[],
        overall_note="x",
        judge_model="x",
    )
    from datetime import datetime as _dt, timezone as _tz
    score = orch.SessionScore(
        session_stable_id="abc123",
        provider="claude",
        started_at=_dt.now(_tz.utc),
        dimension_scores={d.key: 5.0 for d in RUBRIC},
        overall=5.0,
        judge_result=judge,
        features=SessionFeatures(turn_count=2, avg_prompt_chars=40.0),
        source_path="/tmp/abc.jsonl",
        judge_pass=1,
    )
    store.save_session_score(score)

    # Before saving aug_auto, both columns are NULL.
    pre = store.get_session_aug_auto("abc123")
    assert pre == (None, None)

    store.save_session_aug_auto("abc123", "augmentation", 0.83)
    post = store.get_session_aug_auto("abc123")
    assert post[0] == "augmentation"
    assert post[1] == pytest.approx(0.83)


def test_get_session_aug_auto_missing_session_returns_none(tmp_home):
    """No row in session_scores -> get_session_aug_auto returns (None, None)."""
    store = ProfileStore(home=resolve_home())
    assert store.get_session_aug_auto("never-saved") == (None, None)
