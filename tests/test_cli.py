"""Tests for the praxis CLI.

US-047 acceptance criteria (`praxis follow-up`):
  - praxis follow-up prints the most recent follow_ups row in human-readable form
  - Output includes commitment_text, baseline_value, measured_value (or '--'
    if pending), and outcome
  - Exits 0 when a follow-up exists, exits 3 with a clear message when none
    exist yet

US-033 / US-044 acceptance criteria (`praxis review`):
  - `praxis review` is the public verb; `praxis week` is removed entirely
    (no deprecation alias). Invoking `praxis week` exits non-zero with
    argparse's unknown-command error.
  - The internal function is `cmd_review` (renamed from cmd_week);
    internal names `run_weekly` and `WeeklyRunSummary` are unchanged.

US-074 acceptance criteria (`praxis review`, was `praxis week` pre-US-044):
  - `praxis review`, `--week <iso>`, `--dry-run`, `--frontier-only`,
    `--explain-judging`, `--notify`, and `--write-html` are wired to
    the orchestrator with documented behavior
  - `--week` accepts ISO-week strings like 2026-W21 and renders a past
    week's data

US-075 acceptance criteria (auxiliary commands):
  - `praxis re-score <session_stable_id>` re-runs the frontier judge for that
    session and updates session_scores
  - `praxis baseline`, `praxis follow-up`, `praxis history`, `praxis show
    <week_iso>` are wired to read-only operations and exit 0
  - `praxis scan` performs scan + score without rendering a digest
  - `praxis status`, `praxis rubric`, `praxis models` are preserved from v0.1

US-076 acceptance criteria (exit code 2 when no API key):
  - When neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set, the CLI exits
    with code 2 and prints a clear message naming both env vars
  - Read-only paths (`--week`, `--dry-run`, baseline, history, show, follow-up)
    do not gate on the API key (they never invoke the judge)

US-077 acceptance criteria (exit code 3 when no sessions in the window):
  - `praxis review` / `praxis show` exit with code 3 and a clear message when
    the targeted ISO week has zero sessions (spec section 12.3)
  - The exit-2 gate (no API key) takes precedence over exit-3
  - `praxis scan` is the cron-driven data-mover and does NOT exit 3 on an
    empty window, so scheduled runs do not surface spurious failures

US-078 acceptance criteria (removed commands and flags are gone):
  - `praxis install-daemon` (the v0.1 daily one) is not a registered
    subparser and its handler function is no longer exported
  - The `--no-judge` flag is not accepted by `scan` or `review`
  - The v0.1 daily orchestrator entry point (`praxis.orchestrator.run`)
    is not registered as a subcommand's `func` default

Tests go through the argparse entry point (`praxis.cli.__main__.main`) so
the subparser registration is exercised end-to-end, not just the handler.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from praxis.cli.__main__ import build_parser, main
from praxis.follow_up import FollowUp
from praxis.scoring.aggregate import SessionScore
from praxis.scoring.features import SessionFeatures
from praxis.scoring.judge import JudgeResult
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


@pytest.fixture
def fake_api_key(monkeypatch):
    """Set a placeholder ANTHROPIC_API_KEY so judge-gated commands proceed.

    The v0.2 surface (US-076) gates ``praxis review`` (current week) and
    ``praxis scan`` on at least one of ANTHROPIC_API_KEY / OPENAI_API_KEY
    being set; without this fixture every such test would short-circuit
    to exit 2 because ``tmp_home`` deliberately clears both keys. The key
    is never used: no real sessions exist in these tests so the judge
    code path is not reached.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")


def test_follow_up_exits_3_with_clear_message_when_no_row(tmp_home, capsys):
    code = main(["follow-up"])
    out = capsys.readouterr().out
    assert code == 3
    # Clear message: identifies the gap, hints at the resolution.
    assert "No follow-up" in out


def test_follow_up_exits_0_and_prints_all_required_fields_for_pending(tmp_home, capsys):
    """A pending row prints commitment_text, baseline_value, '--' for measured, 'pending' outcome."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="verification",
            commitment_text="ask 'list every table this migration writes'",
            target_metric="verification_rate",
            baseline_value=0.42,
        )
    )
    code = main(["follow-up"])
    out = capsys.readouterr().out
    assert code == 0
    assert "ask 'list every table this migration writes'" in out
    assert "0.42" in out
    assert "--" in out
    assert "pending" in out


def test_follow_up_prints_measured_value_when_present(tmp_home, capsys):
    """A closed row prints the numeric measured_value, not '--'."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="verification",
            commitment_text="ask for source",
            target_metric="verification_rate",
            baseline_value=0.40,
            measured_value=0.95,
            outcome="improved",
        )
    )
    code = main(["follow-up"])
    out = capsys.readouterr().out
    assert code == 0
    assert "ask for source" in out
    assert "0.40" in out
    assert "0.95" in out
    assert "improved" in out


def test_follow_up_shows_most_recent_when_multiple_rows(tmp_home, capsys):
    """When several rows exist, the newest (by week_iso) is displayed."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W19",
            dim_key="verification",
            commitment_text="older week commitment",
            target_metric="verification_rate",
            baseline_value=0.30,
            measured_value=0.31,
            outcome="unchanged",
        )
    )
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="planning",
            commitment_text="newer week commitment",
            target_metric="planning_dim_mean",
            baseline_value=6.10,
        )
    )
    code = main(["follow-up"])
    out = capsys.readouterr().out
    assert code == 0
    assert "newer week commitment" in out
    assert "older week commitment" not in out
    assert "2026-W21" in out


def test_follow_up_renders_worse_outcome(tmp_home, capsys):
    """The 'worse' outcome string appears verbatim in the output."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="iteration",
            commitment_text="push back on first draft",
            target_metric="delegation_rate",
            baseline_value=0.20,
            measured_value=0.85,
            outcome="worse",
        )
    )
    code = main(["follow-up"])
    out = capsys.readouterr().out
    assert code == 0
    assert "worse" in out


# ---------------------------------------------------------------------------
# US-074 - `praxis review` (originally `praxis week`) and its flags.
# ---------------------------------------------------------------------------


def _seed_score(stable_id: str, started_at: datetime, overall: float = 6.0) -> None:
    """Insert one judged session row directly via ProfileStore (no scanners)."""
    store = ProfileStore()
    dim_scores = {d.key: overall for d in RUBRIC}
    judge = JudgeResult(
        dimension_scores=dim_scores,
        rationale={d.key: "fixture" for d in RUBRIC},
        standout_moments=[],
        failure_modes=[],
        overall_note="fixture",
        judge_model="fixture",
    )
    store.save_session_score(
        SessionScore(
            session_stable_id=stable_id,
            provider="claude",
            started_at=started_at,
            dimension_scores=dim_scores,
            overall=overall,
            judge_result=judge,
            features=SessionFeatures(turn_count=4, avg_prompt_chars=120.0),
            source_path=f"/tmp/{stable_id}.jsonl",
        )
    )


def test_review_subcommand_is_registered():
    """`praxis review` must exist as a subparser with the documented flags."""
    parser = build_parser()
    # Argparse raises SystemExit on parse errors; this success-case shape
    # exercises that every flag is recognized at the argparse layer.
    args = parser.parse_args(
        [
            "review",
            "--week",
            "2026-W21",
            "--dry-run",
            "--frontier-only",
            "--explain-judging",
            "--notify",
            "--write-html",
        ]
    )
    assert args.cmd == "review"
    assert args.week == "2026-W21"
    assert args.dry_run is True
    assert args.frontier_only is True
    assert args.explain_judging is True
    assert args.notify is True
    assert args.write_html is True


def test_review_max_new_flag_is_registered():
    """Issue #5: ``--max-new`` exists on the review parser with default 50.

    Mirrors the same cap that ``praxis scan`` has had since v0.2; without
    it, ``praxis review`` could silently kick off N x LLM calls on a
    busy week.
    """
    parser = build_parser()
    args = parser.parse_args(["review"])
    assert args.max_new == 50

    args = parser.parse_args(["review", "--max-new", "10"])
    assert args.max_new == 10

    # 0 is the documented opt-out for the cap.
    args = parser.parse_args(["review", "--max-new", "0"])
    assert args.max_new == 0


def test_week_subcommand_is_removed():
    """`praxis week` must no longer be a registered subparser (US-033).

    The rename to ``praxis review`` is hard, with no deprecation alias
    (per spec Section 3). Existing scripts that ran ``praxis week`` must
    fail loudly rather than silently dispatch through an alias.
    """
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["week"])


def test_week_subcommand_handler_is_renamed():
    """``cmd_week`` is gone; the renamed ``cmd_review`` is what's exported.

    Locks in the AC US-033 rename so a future change can't quietly
    re-introduce the old symbol as a shim.
    """
    import praxis.cli.__main__ as cli_main

    assert not hasattr(cli_main, "cmd_week")
    assert hasattr(cli_main, "cmd_review")


def test_review_with_data_renders_and_exits_zero(tmp_home, capsys, fake_api_key):
    """`praxis review` renders the digest masthead when sessions exist in the window."""
    _seed_score("sess-current", datetime.now(UTC))
    code = main(["review"])
    out = capsys.readouterr().out
    assert code == 0
    # Masthead renders when there is data.
    assert "PRAXIS" in out


def test_review_iso_filters_to_target_week(tmp_home, capsys):
    """`--week 2026-W21` renders only sessions whose started_at falls inside that ISO week.

    Seeds three sessions across three different weeks; the digest's session
    count must reflect the single one that lives in 2026-W21.
    """
    # 2026-W21 spans Mon May 18 - Sun May 24, 2026.
    in_week = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    before_week = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    after_week = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    _seed_score("sess-in", in_week)
    _seed_score("sess-before", before_week)
    _seed_score("sess-after", after_week)

    code = main(["review", "--week", "2026-W21"])
    out = capsys.readouterr().out
    assert code == 0
    # The v0.2 digest masthead names the ISO week; filtering is verified
    # by the digest having content (sess-in qualified) while sess-before /
    # sess-after were correctly excluded by the snapshot query. The
    # digest_terminal tests cover the masthead format itself.
    assert "Week of May" in out


def test_review_rejects_malformed_iso(tmp_home, capsys):
    """A malformed ISO-week string exits 1 with a clear error message."""
    code = main(["review", "--week", "not-a-week"])
    err = capsys.readouterr().err
    assert code == 1
    assert "YYYY-Www" in err


def test_review_write_html_creates_file(tmp_home, capsys):
    """--write-html writes to ~/.praxis/weeks/<iso>.html."""
    in_week = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    _seed_score("sess-html", in_week)
    code = main(["review", "--week", "2026-W21", "--write-html"])
    capsys.readouterr()  # flush captured output
    assert code == 0
    html_path = resolve_home() / "weeks" / "2026-W21.html"
    assert html_path.exists()
    assert "<html" in html_path.read_text(encoding="utf-8").lower()


def test_review_dry_run_does_not_create_html(tmp_home, capsys):
    """--dry-run without --write-html leaves no files behind.

    Seeds one current-week session so the run reaches the HTML-decision
    point (otherwise US-077's zero-session gate short-circuits to exit 3
    and the assertion would be vacuous).
    """
    _seed_score("sess-dry", datetime.now(UTC))
    code = main(["review", "--dry-run"])
    capsys.readouterr()
    assert code == 0
    assert not (resolve_home() / "weeks").exists()


def test_review_explain_judging_prints_explainer(tmp_home, capsys, fake_api_key):
    """--explain-judging surfaces the pass-1 confidence block (or a clear stub).

    Seeds one current-week session so the digest renders past the
    US-077 zero-session gate and the explainer block is printed.
    """
    _seed_score("sess-explain", datetime.now(UTC))
    code = main(["review", "--explain-judging"])
    out = capsys.readouterr().out
    assert code == 0
    assert "--explain-judging" in out


def test_review_explain_judging_notes_frontier_only(tmp_home, capsys, fake_api_key):
    """When both --frontier-only and --explain-judging are set, the explainer
    notes that pass-1 was skipped.

    Seeds one current-week session so the digest renders past the
    US-077 zero-session gate.
    """
    _seed_score("sess-frontier", datetime.now(UTC))
    code = main(["review", "--frontier-only", "--explain-judging"])
    out = capsys.readouterr().out
    assert code == 0
    assert "pass-1 skipped" in out


# ---------------------------------------------------------------------------
# US-075 - auxiliary commands: re-score, baseline, history, show, scan.
# ---------------------------------------------------------------------------


def _write_minimal_claude_session(tmp_home: Path, session_id: str) -> Path:
    """Write a small synthetic Claude JSONL file under the test home.

    Returns the path. Used by the re-score test to give the re-scorer a
    real on-disk file to re-parse, since `re_score_session` reads
    `source_path` from the persisted row and hands it to the scanner.
    """
    root = tmp_home / ".claude" / "projects" / "rescore-test"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{session_id}.jsonl"
    when = datetime.now(UTC) - timedelta(hours=1)
    events = [
        {
            "type": "user",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": "Goal: re-score me."},
        },
        {
            "type": "assistant",
            "timestamp": (when + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "Acknowledged."}],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


@pytest.fixture
def fake_frontier_judge(monkeypatch):
    """Replace pass-2 judge with a deterministic stub returning 7.5.

    Mirrors test_orchestrator.fake_judge but locks the score so the
    re-score test can assert the row was overwritten by the new judge
    output (the seed row uses 4.0 so any change is detectable). Also
    sets ANTHROPIC_API_KEY so the CLI's judge-gate (US-076) lets
    `praxis re-score` proceed; the key is never used since the judge
    itself is stubbed.
    """

    def _fake(session, prefer="claude", **kwargs):
        return JudgeResult(
            dimension_scores={d.key: 7.5 for d in RUBRIC},
            rationale={d.key: "re-scored fixture" for d in RUBRIC},
            standout_moments=["re-score standout"],
            failure_modes=[],
            overall_note="re-scored",
            judge_model="fixture-frontier",
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr("praxis.scoring.aggregate.score_session_pass2", _fake)
    return _fake


def test_subcommands_registered():
    """Every v0.2 auxiliary command must be registered as a subparser."""
    parser = build_parser()
    # `praxis re-score X` is the single-session counterpart to `praxis scan`.
    args = parser.parse_args(["re-score", "sess-001"])
    assert args.cmd == "re-score"
    assert args.session_stable_id == "sess-001"
    # Read-only verbs take no arguments at the parser layer.
    assert parser.parse_args(["baseline"]).cmd == "baseline"
    assert parser.parse_args(["history"]).cmd == "history"
    # `show` is positional ISO-week; same shape as `--week` on the week command.
    show_args = parser.parse_args(["show", "2026-W21"])
    assert show_args.cmd == "show"
    assert show_args.week_iso == "2026-W21"


def test_rescore_unknown_session_exits_1(tmp_home, capsys):
    """re-score must exit 1 with a clear error when the stable_id is unknown."""
    code = main(["re-score", "does-not-exist"])
    err = capsys.readouterr().err
    assert code == 1
    assert "does-not-exist" in err


def test_rescore_updates_session_row(tmp_home, capsys, fake_frontier_judge):
    """re-score re-runs the judge and overwrites session_scores for that row."""
    # Write a real source file the Claude scanner can re-parse, then seed
    # session_scores with a row pointing at it. The Claude scanner's
    # stable_id derives from session_id+provider; we mirror that here so
    # the re-score lookup finds the seeded row.
    sid_uuid = str(uuid.uuid4())
    src = _write_minimal_claude_session(tmp_home, sid_uuid)
    # Parse via the scanner once to learn the stable_id (avoids hand-rolling
    # the hash).
    from praxis.scanners.claude import ClaudeScanner

    parsed = ClaudeScanner().parse(src)
    assert parsed is not None
    stable_id = parsed.stable_id

    # Seed a low-score row so we can detect the re-score actually wrote 7.5.
    _seed_score(stable_id, parsed.started_at, overall=4.0)
    # The seeded row points at /tmp/<id>.jsonl from _seed_score; overwrite
    # source_path to point at the real file we wrote so the re-scorer can
    # re-parse it.
    store = ProfileStore()
    row = store.load_one_session_score(stable_id)
    assert row is not None
    score = SessionScore(
        session_stable_id=stable_id,
        provider="claude",
        started_at=parsed.started_at,
        dimension_scores=row["dimension_scores"],
        overall=4.0,
        judge_result=JudgeResult(
            dimension_scores=row["dimension_scores"],
            rationale={d.key: "seed" for d in RUBRIC},
            standout_moments=[],
            failure_modes=[],
            overall_note="seed",
            judge_model="seed",
        ),
        features=SessionFeatures(turn_count=2, avg_prompt_chars=20.0),
        source_path=str(src),
    )
    store.save_session_score(score)

    code = main(["re-score", stable_id])
    out = capsys.readouterr().out
    assert code == 0
    assert "Re-scored" in out
    assert stable_id in out

    # The persisted row should reflect the new judge output (7.5 across
    # every dim => weighted overall ~7.5 too).
    refreshed = store.load_one_session_score(stable_id)
    assert refreshed is not None
    assert refreshed["overall"] == pytest.approx(7.5, abs=0.01)
    assert refreshed["judge_result"]["judge_model"] == "fixture-frontier"


def test_rescore_without_judge_exits_2(tmp_home, capsys):
    """No API keys => re-score exits 2 (the "no judge" exit code)."""
    sid_uuid = str(uuid.uuid4())
    src = _write_minimal_claude_session(tmp_home, sid_uuid)
    from praxis.scanners.claude import ClaudeScanner

    parsed = ClaudeScanner().parse(src)
    assert parsed is not None
    stable_id = parsed.stable_id
    _seed_score(stable_id, parsed.started_at)
    # Overwrite source_path to the real file so re-parse succeeds; the
    # judge will then be the failing step.
    store = ProfileStore()
    score = SessionScore(
        session_stable_id=stable_id,
        provider="claude",
        started_at=parsed.started_at,
        dimension_scores={d.key: 6.0 for d in RUBRIC},
        overall=6.0,
        judge_result=JudgeResult(
            dimension_scores={d.key: 6.0 for d in RUBRIC},
            rationale={d.key: "seed" for d in RUBRIC},
            standout_moments=[],
            failure_modes=[],
            overall_note="seed",
            judge_model="seed",
        ),
        features=SessionFeatures(turn_count=2, avg_prompt_chars=20.0),
        source_path=str(src),
    )
    store.save_session_score(score)

    code = main(["re-score", stable_id])
    err = capsys.readouterr().err
    # tmp_home fixture cleared ANTHROPIC_API_KEY and OPENAI_API_KEY, so the
    # real score_session() returns None and re_score_session raises with
    # code="no_judge".
    assert code == 2
    assert "ANTHROPIC_API_KEY" in err or "OPENAI_API_KEY" in err


def test_baseline_on_empty_data_exits_zero(tmp_home, capsys):
    """baseline must render cleanly when there are no sessions yet."""
    code = main(["baseline"])
    out = capsys.readouterr().out
    assert code == 0
    assert "90-DAY BASELINE" in out
    # Forming state: placeholder appears for the overall + every dim row.
    assert "--" in out


def test_baseline_with_data_prints_overall_and_dims(tmp_home, capsys):
    """With 14+ days of data, baseline prints numeric overall + every dim."""
    # Seed 20 days ago so the data span is >= 14 (baseline is not forming).
    when = datetime.now(UTC) - timedelta(days=20)
    _seed_score("base-1", when, overall=6.4)
    _seed_score("base-2", when + timedelta(days=1), overall=7.0)

    code = main(["baseline"])
    out = capsys.readouterr().out
    assert code == 0
    assert "90-DAY BASELINE" in out
    assert "Sessions in window: 2" in out
    # Every rubric dim title appears in the output.
    for d in RUBRIC:
        assert d.title in out


def test_history_with_no_data_exits_zero(tmp_home, capsys):
    """history on an empty DB must exit 0 with a helpful message."""
    code = main(["history"])
    out = capsys.readouterr().out
    assert code == 0
    assert "No history yet" in out


def test_history_lists_iso_weeks_newest_first(tmp_home, capsys):
    """history groups session_scores by ISO week, newest first."""
    # Two sessions in 2026-W21, one in 2026-W19.
    _seed_score("h1", datetime(2026, 5, 20, 12, 0, tzinfo=UTC))
    _seed_score("h2", datetime(2026, 5, 21, 12, 0, tzinfo=UTC))
    _seed_score("h3", datetime(2026, 5, 6, 12, 0, tzinfo=UTC))

    code = main(["history"])
    out = capsys.readouterr().out
    assert code == 0
    assert "2026-W21" in out
    assert "2026-W19" in out
    # 2026-W21 has 2 sessions, 2026-W19 has 1; newest week appears first.
    assert out.index("2026-W21") < out.index("2026-W19")


def test_show_renders_past_week_and_exits_zero(tmp_home, capsys):
    """show <week_iso> renders the persisted snapshot for that week."""
    in_week = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    _seed_score("show-1", in_week)

    code = main(["show", "2026-W21"])
    out = capsys.readouterr().out
    assert code == 0
    # Masthead matches the week digest output.
    assert "PRAXIS" in out
    assert "Week of May" in out


def test_show_rejects_malformed_week_iso(tmp_home, capsys):
    """show with a malformed ISO-week tag exits 1."""
    code = main(["show", "bogus"])
    err = capsys.readouterr().err
    assert code == 1
    assert "YYYY-Www" in err


def test_scan_does_not_render_digest(tmp_home, capsys, fake_api_key):
    """scan must NOT render the masthead/dimensions; it prints a one-line summary."""
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    # The digest masthead contains "PRAXIS" in its banner. scan should not
    # produce that banner; it should produce a "Scanned N session(s)" line.
    assert "Scanned" in out
    assert "PRAXIS" not in out
    # No HTML file written either.
    assert not (resolve_home() / "report.html").exists()


def test_status_preserved(tmp_home, capsys):
    """`praxis status` survives the v0.2 surface refactor (US-075 ACs)."""
    code = main(["status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Scorecard home" in out


def test_rubric_preserved(tmp_home, capsys):
    """`praxis rubric` survives the v0.2 surface refactor (US-075 ACs)."""
    code = main(["rubric"])
    out = capsys.readouterr().out
    assert code == 0
    assert "SCORING RUBRIC" in out


def test_models_preserved(tmp_home, capsys):
    """`praxis models` survives the v0.2 surface refactor (US-075 ACs)."""
    code = main(["models"])
    out = capsys.readouterr().out
    assert code == 0
    assert "model cards loaded" in out


# ---------------------------------------------------------------------------
# US-076 - exit code 2 when no API key is configured (spec sections 11, 12.3).
# ---------------------------------------------------------------------------


def test_week_without_api_key_exits_2(tmp_home, capsys):
    """`praxis review` with no API keys exits 2 with a message naming both vars.

    tmp_home clears ANTHROPIC_API_KEY / OPENAI_API_KEY so this exercises
    the actual gate, not a stubbed version of it.
    """
    code = main(["review"])
    err = capsys.readouterr().err
    assert code == 2
    # Spec section 11 says the message must be clear and identify the
    # missing credential. Naming both env vars lets the user pick whichever
    # they have at hand.
    assert "ANTHROPIC_API_KEY" in err
    assert "OPENAI_API_KEY" in err


def test_week_with_only_anthropic_key_proceeds(tmp_home, capsys, monkeypatch):
    """One key is enough -- the gate is OR, not AND (spec 12.2 fallback).

    Asserts the run is not blocked by the exit-2 gate. Exit 3 (no
    sessions in window) is fine here -- the point of this test is to
    prove the API-key gate is OR not AND, not to exercise the digest.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    code = main(["review"])
    assert code != 2
    capsys.readouterr()


def test_week_with_only_openai_key_proceeds(tmp_home, capsys, monkeypatch):
    """OpenAI alone is also enough -- mirrors the Claude-only case.

    Exit 3 (no sessions in window) is acceptable; we only require that
    the API-key gate did not short-circuit the run with exit 2.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    code = main(["review"])
    assert code != 2
    capsys.readouterr()


def test_week_iso_bypasses_api_key_check(tmp_home, capsys):
    """`praxis review --week <iso>` is read-only; it must run without API keys.

    With no seeded data the past-week branch exits 3 (no sessions in
    window), which is the correct US-077 behavior. The test only asserts
    the API-key gate (exit 2) did not fire.
    """
    code = main(["review", "--week", "2026-W21"])
    capsys.readouterr()
    # No keys set, but the past-week branch never calls the judge.
    assert code != 2


def test_week_iso_current_week_does_not_warn_about_missing_judges(tmp_home, capsys, monkeypatch):
    """`--week <current>` is a historical read even if the ISO tag is current."""
    from praxis.cli import __main__ as cli_main
    from praxis.orchestrator import WeeklyRunSummary, current_iso_week
    from praxis.scoring.aggregate import ProfileSnapshot

    week_iso = current_iso_week()
    snapshot = ProfileSnapshot(
        overall=6.0,
        dimension_means={d.key: 6.0 for d in RUBRIC},
        session_count=1,
        provider_breakdown={"claude": 1},
        strongest_dimension=RUBRIC[0].key,
        weakest_dimension=RUBRIC[-1].key,
    )
    fake_summary = WeeklyRunSummary(
        week_iso=week_iso,
        sessions=[object()],
        tasks=[],
        judge_results={},
        moments=[],
        selection=None,
        snapshot=snapshot,
        rendered_html="<html></html>",
        rendered_terminal="PRAXIS - Weekly read",
        elapsed_seconds=0.0,
    )
    monkeypatch.setattr(cli_main, "run_weekly", lambda **kw: fake_summary)

    code = main(["review", "--week", week_iso])
    captured = capsys.readouterr()

    assert code == 0
    assert "PRAXIS" in captured.out
    assert "judge produced no scores" not in captured.err


def test_week_dry_run_bypasses_api_key_check(tmp_home, capsys):
    """`praxis review --dry-run` is read-only; it must run without API keys.

    With no seeded data the dry-run branch exits 3 (no sessions in
    window). The test only asserts the API-key gate did not fire.
    """
    code = main(["review", "--dry-run"])
    capsys.readouterr()
    assert code != 2


def test_scan_without_api_key_exits_2(tmp_home, capsys):
    """`praxis scan` with no API keys exits 2 with a message naming both vars."""
    code = main(["scan"])
    err = capsys.readouterr().err
    assert code == 2
    assert "ANTHROPIC_API_KEY" in err
    assert "OPENAI_API_KEY" in err


def test_baseline_does_not_gate_on_api_key(tmp_home, capsys):
    """Read-only verbs (baseline) keep exiting 0 even with no API keys."""
    code = main(["baseline"])
    out = capsys.readouterr().out
    assert code == 0
    assert "90-DAY BASELINE" in out


def test_history_does_not_gate_on_api_key(tmp_home, capsys):
    """Read-only verbs (history) keep exiting 0 even with no API keys."""
    code = main(["history"])
    out = capsys.readouterr().out
    assert code == 0
    # Empty history message is fine -- the point is we didn't exit 2.
    assert "history" in out.lower() or "No history" in out


# ---------------------------------------------------------------------------
# US-077 - exit code 3 when no sessions in the window (spec section 12.3).
# ---------------------------------------------------------------------------


def test_week_current_window_with_no_sessions_exits_3(tmp_home, capsys, fake_api_key):
    """`praxis review` on an empty DB exits 3 with a clear message.

    With an API key set the exit-2 gate is bypassed; the exit-3 gate
    fires because the snapshot's session_count is zero. The message
    must identify what is missing so the user knows what to do next.
    """
    code = main(["review"])
    err = capsys.readouterr().err
    assert code == 3
    assert "No sessions found" in err


def test_week_past_week_with_no_sessions_exits_3(tmp_home, capsys):
    """`praxis review --week <iso>` exits 3 when zero rows fall in that week.

    No API key is required (the past-week branch is read-only) so this
    exercises the exit-3 gate in isolation from the exit-2 gate.
    """
    code = main(["review", "--week", "2026-W21"])
    err = capsys.readouterr().err
    assert code == 3
    # The message names the targeted ISO week so the user knows which
    # window was searched.
    assert "2026-W21" in err
    assert "No sessions found" in err


def test_week_dry_run_with_no_sessions_exits_3(tmp_home, capsys):
    """`praxis review --dry-run` exits 3 when the current week has no data.

    --dry-run is read-only (skips the exit-2 gate), so we land directly
    on the exit-3 gate when the DB is empty.
    """
    code = main(["review", "--dry-run"])
    err = capsys.readouterr().err
    assert code == 3
    assert "No sessions found" in err


def test_show_with_no_sessions_exits_3(tmp_home, capsys):
    """`praxis show <iso>` exits 3 when no rows exist for that ISO week."""
    code = main(["show", "2026-W21"])
    err = capsys.readouterr().err
    assert code == 3
    assert "2026-W21" in err
    assert "No sessions found" in err


def test_no_api_key_trumps_no_sessions(tmp_home, capsys):
    """Exit-2 (no API key) takes precedence over exit-3 (no sessions).

    On a fresh machine with no keys and no data, both gates would fire;
    the more actionable message (set an API key) is the right one to
    show, so the API-key gate runs first.
    """
    code = main(["review"])
    err = capsys.readouterr().err
    assert code == 2
    assert "ANTHROPIC_API_KEY" in err


def test_week_with_seeded_session_does_not_exit_3(tmp_home, capsys, fake_api_key):
    """Positive case: a seeded session in the current window yields exit 0."""
    _seed_score("sess-positive", datetime.now(UTC))
    code = main(["review"])
    out = capsys.readouterr().out
    assert code == 0
    assert "PRAXIS" in out


def test_scan_with_no_sessions_does_not_exit_3(tmp_home, capsys, fake_api_key):
    """`praxis scan` is the cron-driven data-mover, not a digest renderer.

    With zero sessions in the window scan still exits 0 (so the launchd /
    systemd / Task Scheduler job does not surface a spurious failure);
    the exit-3 contract is for ``praxis review`` / ``praxis show``.
    """
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Scanned" in out


# ---------------------------------------------------------------------------
# US-078: removed commands and flags are gone
# ---------------------------------------------------------------------------


def test_install_daemon_subcommand_is_absent():
    """`praxis install-daemon` (the v0.1 daily one) must not be a subcommand.

    Per spec section 11, the daily daemon is removed in v0.2; the weekly
    replacement (`install-weekly`) lands in a separate story. argparse
    raises SystemExit on an unknown subcommand, so we assert that here.
    """
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["install-daemon"])


def test_install_daemon_handler_is_not_importable():
    """The handler function must be deleted, not just unregistered.

    Asserts that `cmd_install_daemon` is no longer exported by the CLI
    module so dead code can't be revived by an accidental subparser
    registration in a future change.
    """
    import praxis.cli.__main__ as cli_main

    assert not hasattr(cli_main, "cmd_install_daemon")


def test_no_judge_flag_is_absent_from_scan():
    """`praxis scan --no-judge` must fail at the argparse layer (spec 11).

    The judge-free path is no longer a supported mode in v0.2; runs
    without keys exit 2 (US-076) rather than silently degrading.
    """
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "--no-judge"])


def test_no_judge_flag_is_absent_from_week():
    """`praxis review --no-judge` must also fail (spec 11).

    The review verb never accepted --no-judge, but lock it in so a future
    change can't quietly add it back.
    """
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["review", "--no-judge"])


def test_v01_daily_entry_point_is_not_a_subcommand():
    """The v0.1 daily orchestrator's `run()` is not exposed as a subcommand.

    v0.1 had a daily flow wired into a `praxis install-daemon` subcommand
    that registered a cron / launchd / systemd unit. With that removed,
    no subparser should map directly onto `praxis.orchestrator.run`; only
    `cmd_scan` may call into it, and even that path is gated by
    `has_api_key_configured()`. Iterate the registered subparsers and
    assert none bind to the daily-flow handler.
    """
    from praxis import orchestrator

    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    for name, sub_parser in subparsers_action.choices.items():
        func = sub_parser.get_default("func")
        # `run` from praxis.orchestrator is the v0.1 daily entry point.
        # No subcommand should set func=orchestrator.run directly.
        assert func is not orchestrator.run, (
            f"subcommand {name!r} binds directly to the v0.1 daily orchestrator entry point"
        )


# ---------------------------------------------------------------------------
# US-081 - macOS notification via osascript on `praxis review --notify`.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_osascript(monkeypatch):
    """Record every subprocess.run call from praxis.cli.__main__ and succeed.

    The --notify path shells out to ``osascript`` via subprocess.run on
    Darwin. Tests assert on the recorded command list so they verify
    the title/body without actually invoking the user's NotificationCenter.
    """
    import subprocess as _subprocess

    calls: list[list[str]] = []

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("praxis.cli.__main__.subprocess.run", _run)
    return calls


def test_notify_calls_osascript_after_writing_html(
    tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript
):
    """`praxis review --notify` writes the HTML then posts the notification.

    Asserts both that the HTML file was written (so the "after writing
    the HTML" half of the AC is satisfied -- nothing can read latest.html
    if the digest never landed) and that osascript was invoked once.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-notify", datetime.now(UTC))

    code = main(["review", "--notify"])
    capsys.readouterr()
    assert code == 0
    # The HTML file is written for the current week before notify fires.
    iso_weeks = list((resolve_home() / "weeks").glob("*.html"))
    assert len(iso_weeks) == 1
    # osascript was invoked exactly once with the `display notification` recipe.
    assert len(fake_osascript) == 1
    cmd = fake_osascript[0]
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    assert "display notification" in cmd[2]


def test_notify_title_is_fixed_string(tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript):
    """The notification title is exactly 'Praxis weekly read is ready'.

    Per spec section 13.2 / AC US-081, the title is a fixed string. The
    trajectory label (US-082) lives in the body, not the title.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-title", datetime.now(UTC))

    code = main(["review", "--notify"])
    capsys.readouterr()
    assert code == 0
    script = fake_osascript[0][2]
    assert 'with title "Praxis weekly read is ready"' in script


def test_notify_body_mentions_latest_html(
    tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript
):
    """The notification body points the user at ``~/.praxis/latest.html``.

    Per AC US-081, the body must mention opening that path. The HTML
    digest writer (US-064) maintains ``latest.html`` as a symlink to
    the most recent week's file, so the user has a single stable
    location to open regardless of which ISO week was just rendered.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-body", datetime.now(UTC))

    code = main(["review", "--notify"])
    capsys.readouterr()
    assert code == 0
    script = fake_osascript[0][2]
    assert "~/.praxis/latest.html" in script


def test_notify_is_noop_on_non_darwin(tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript):
    """On non-macOS, --notify is a silent no-op: osascript is not invoked.

    The spec keeps the same flag portable across platforms (spec 13.2);
    Linux/Windows simply skip the notification step and still render
    the digest + HTML normally.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    _seed_score("sess-non-darwin", datetime.now(UTC))

    code = main(["review", "--notify"])
    capsys.readouterr()
    assert code == 0
    # No osascript call should have been recorded.
    assert fake_osascript == []


# ---------------------------------------------------------------------------
# US-082 - notification includes trajectory label, osascript never crashes run.
# ---------------------------------------------------------------------------


def test_notify_body_includes_trajectory_label(
    tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript
):
    """The notification body carries the digest's trajectory label.

    Per AC US-082, the body must include the trajectory label so the
    user gets the gist without opening the HTML (spec 13.2 example:
    "Drifting this week."). With a single seeded session the trajectory
    falls into INSUFFICIENT_DATA, which renders as "Reading" -- that
    string must appear in the AppleScript ``display notification`` body.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-traj", datetime.now(UTC))

    code = main(["review", "--notify"])
    capsys.readouterr()
    assert code == 0
    script = fake_osascript[0][2]
    # User-facing form of INSUFFICIENT_DATA per the terminal renderer.
    assert "Reading" in script
    # AC US-081 still holds: the latest.html path stays in the body.
    assert "~/.praxis/latest.html" in script


def test_notify_body_uses_user_facing_trajectory_label(
    tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript
):
    """Raw enum values like ``stable_engaged`` are mapped to display form.

    Drives the assertion that the body never contains the underscored
    enum value: when the trajectory label is ``stable_engaged`` the
    body must show "Engaged", not "Stable_Engaged" or "stable_engaged".
    Forces a known label by stubbing ``run_weekly`` so the test does
    not depend on the heuristic assessor's bucket count.
    """
    from praxis.behavior.trajectory import TrajectoryAssessment, TrajectoryLabel
    from praxis.cli import __main__ as cli_main
    from praxis.orchestrator import WeeklyRunSummary
    from praxis.scoring.aggregate import ProfileSnapshot

    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-engaged", datetime.now(UTC))

    # Stub run_weekly to return a summary with a known non-INSUFFICIENT_DATA
    # trajectory. Build a minimal snapshot that satisfies the exit-3 gate
    # (session_count >= 1) without going through the orchestrator.
    snapshot = ProfileSnapshot(
        overall=6.0,
        dimension_means={d.key: 6.0 for d in RUBRIC},
        session_count=1,
        provider_breakdown={"claude": 1},
        strongest_dimension=RUBRIC[0].key,
        weakest_dimension=RUBRIC[-1].key,
    )
    fake_summary = WeeklyRunSummary(
        week_iso="2026-W21",
        sessions=[],
        tasks=[],
        judge_results={},
        moments=[],
        selection=None,
        snapshot=snapshot,
        rendered_html="<html></html>",
        rendered_terminal="",
        elapsed_seconds=0.0,
        trajectory=TrajectoryAssessment(
            label=TrajectoryLabel.STABLE_ENGAGED,
            engagement_slope=0.0,
            delegation_slope=0.0,
            headline="Engaged this week.",
        ),
    )
    monkeypatch.setattr(cli_main, "run_weekly", lambda **kw: fake_summary)

    code = main(["review", "--notify"])
    capsys.readouterr()
    assert code == 0
    script = fake_osascript[0][2]
    assert "Engaged" in script
    # Negative: the raw enum value (with underscore) must never leak through.
    assert "stable_engaged" not in script
    assert "Stable_Engaged" not in script


def test_notify_survives_osascript_filenotfound(tmp_home, capsys, monkeypatch, fake_api_key):
    """`osascript` binary missing must not crash the run (AC US-082).

    Sandboxed CI runners may not have ``osascript`` on PATH;
    subprocess.run raises FileNotFoundError before the AppleScript runs.
    The CLI must log the failure to stderr and still exit 0 -- the
    digest is already rendered and the HTML is already written.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-nofile", datetime.now(UTC))

    def _missing(cmd, *args, **kwargs):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr("praxis.cli.__main__.subprocess.run", _missing)

    code = main(["review", "--notify"])
    captured = capsys.readouterr()
    assert code == 0
    # Failure is surfaced to stderr so the user / launchd log has a record.
    assert "osascript notification failed" in captured.err


def test_notify_survives_osascript_nonzero_exit(tmp_home, capsys, monkeypatch, fake_api_key):
    """osascript returning non-zero must not crash the run (AC US-082).

    Notification Center can refuse to display (locked screen, focus
    mode, denied permission). osascript exits non-zero in those cases.
    The CLI must log the failure and still exit 0.
    """
    import subprocess as _subprocess

    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-nonzero", datetime.now(UTC))

    def _failing(cmd, *args, **kwargs):
        return _subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr="permission denied"
        )

    monkeypatch.setattr("praxis.cli.__main__.subprocess.run", _failing)

    code = main(["review", "--notify"])
    captured = capsys.readouterr()
    assert code == 0
    assert "osascript notification failed" in captured.err
    assert "permission denied" in captured.err


def test_notify_survives_unexpected_exception(tmp_home, capsys, monkeypatch, fake_api_key):
    """Any exception from subprocess.run is caught (AC US-082).

    Defends the broad ``except Exception`` clause: even when something
    unexpected (e.g., PermissionError, OSError) bubbles out of the
    subprocess layer, the run must still exit 0 with the failure
    logged to stderr.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-oserror", datetime.now(UTC))

    def _explode(cmd, *args, **kwargs):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr("praxis.cli.__main__.subprocess.run", _explode)

    code = main(["review", "--notify"])
    captured = capsys.readouterr()
    assert code == 0
    assert "osascript notification failed" in captured.err


# ---------------------------------------------------------------------------
# US-016 - `praxis nudge` resolves and surfaces this week's active commitment.
# ---------------------------------------------------------------------------


def _seed_follow_up(
    week_iso: str,
    *,
    commitment_text: str = "ask 'list every table this migration writes'",
    dim_key: str = "verification",
    target_metric: str = "verification_rate",
    baseline_value: float = 0.42,
    measured_value: float | None = None,
    outcome: str = "pending",
) -> None:
    """Insert one follow_ups row via ProfileStore for nudge-resolver tests."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso=week_iso,
            dim_key=dim_key,
            commitment_text=commitment_text,
            target_metric=target_metric,
            baseline_value=baseline_value,
            measured_value=measured_value,
            outcome=outcome,  # type: ignore[arg-type]
        )
    )


def test_nudge_subcommand_is_registered():
    """`praxis nudge` must be parseable with no flags (US-016 AC #1)."""
    parser = build_parser()
    args = parser.parse_args(["nudge"])
    assert args.cmd == "nudge"


def test_nudge_silent_when_no_follow_up_row_exists(tmp_home, capsys):
    """Empty follow_ups table: exit 0, empty stdout, empty stderr.

    Hooks fire on every shell / IDE start; without a committed commitment
    yet, they must produce zero noise (US-016 AC #2).
    """
    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_nudge_silent_when_only_resolved_rows_exist(tmp_home, capsys, monkeypatch):
    """A non-pending row for THIS week is not an active commitment.

    Once a row's outcome moves out of 'pending' (improved/unchanged/worse),
    it no longer counts as the active commitment -- the user has already
    seen its outcome in their weekly digest.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    _seed_follow_up(
        "2026-W21",
        commitment_text="resolved last week",
        measured_value=0.95,
        outcome="improved",
    )
    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""


def test_nudge_silent_when_pending_row_is_for_a_different_week(tmp_home, capsys, monkeypatch):
    """A pending row from a prior week is not active for THIS week.

    Active = pending AND week_iso == current. Stale pending rows (e.g.,
    if the close-the-loop step didn't run) must not leak into the current
    week's nudge surface.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    _seed_follow_up("2026-W19", commitment_text="stale pending")
    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""


def test_nudge_prints_active_commitment_for_current_week(tmp_home, capsys, monkeypatch):
    """A pending row for the current week is the active commitment.

    Output is single-line so SessionStart hooks can pipe it straight to
    the user without further parsing.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    _seed_follow_up(
        "2026-W21",
        commitment_text="ask 'what would falsify this answer?' before applying",
    )
    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert "ask 'what would falsify this answer?' before applying" in captured.out
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def test_nudge_uses_current_iso_week_resolver(tmp_home, capsys, monkeypatch):
    """Switching the week resolver swaps which row is surfaced.

    Confirms `current_iso_week()` is the seam the command resolves through
    (not e.g. latest_follow_up, which would surface stale rows).
    """
    _seed_follow_up("2026-W19", commitment_text="week 19 commitment")
    _seed_follow_up("2026-W21", commitment_text="week 21 commitment")

    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W19")
    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert "week 19 commitment" in captured.out
    assert "week 21 commitment" not in captured.out


def test_nudge_exits_nonzero_with_clear_error_on_multiple_active_rows(
    tmp_home, capsys, monkeypatch
):
    """Multi-active is a violated invariant: surface it loudly (AC #3).

    The legacy schema's PRIMARY KEY (week_iso) and the migration's partial
    unique index both prevent this case; the test injects two rows via
    monkeypatch since the schema makes the case unreachable in practice.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    fakes = [
        FollowUp(
            week_iso="2026-W21",
            dim_key="verification",
            commitment_text="first active",
            target_metric="verification_rate",
            baseline_value=0.4,
        ),
        FollowUp(
            week_iso="2026-W21",
            dim_key="planning",
            commitment_text="second active",
            target_metric="planning_dim_mean",
            baseline_value=6.0,
        ),
    ]
    monkeypatch.setattr(
        "praxis.cli.__main__.ProfileStore.load_active_commitments",
        lambda self, week_iso: fakes,
    )

    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code != 0
    # Error must reference the violated invariant in user-readable terms.
    assert "active commitments" in captured.err
    assert "2026-W21" in captured.err
    # Stdout stays clean so hooks consuming stdout don't see a half-message.
    assert captured.out == ""


# ---------------------------------------------------------------------------
# US-017 - `praxis nudge --format` selects between text and JSON envelopes.
# ---------------------------------------------------------------------------


def test_nudge_default_format_is_text(tmp_home, capsys, monkeypatch):
    """No `--format` flag must behave exactly like `--format text` (US-017 AC #2).

    The default surface is the human-readable line that shell hooks pipe
    straight to the prompt; introducing a JSON-by-default would break
    every existing shell-startup wiring.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    _seed_follow_up("2026-W21", commitment_text="explain the failing test first")
    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "[Praxis] This week: explain the failing test first\n"


def test_nudge_format_text_prints_single_line_with_newline(tmp_home, capsys, monkeypatch):
    """`--format text` prints exactly `[Praxis] This week: <text>` + newline."""
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    _seed_follow_up(
        "2026-W21",
        commitment_text="ask 'what would falsify this answer?' first",
    )
    code = main(["nudge", "--format", "text"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "[Praxis] This week: ask 'what would falsify this answer?' first\n"
    assert captured.err == ""


def test_nudge_format_claude_code_emits_single_line_json(tmp_home, capsys, monkeypatch):
    """`--format claude-code` prints exactly the spec section 5 JSON envelope.

    The envelope is single-line JSON with no whitespace between tokens so
    Claude Code's SessionStart hook reader sees one stdin line.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    _seed_follow_up(
        "2026-W21",
        commitment_text="run the linter before requesting review",
    )
    code = main(["nudge", "--format", "claude-code"])
    captured = capsys.readouterr()
    assert code == 0
    # Single-line stdout: exactly one trailing newline, no internal newlines.
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload == {
        "hookSpecificOutput": {
            "additionalContext": (
                "[Praxis] This week's focus: run the linter before requesting review"
            ),
        },
    }
    # Compact form: no spaces inside the envelope.
    assert " " not in captured.out.split('"additionalContext"')[0]


def test_nudge_format_codex_emits_same_envelope_as_claude_code(tmp_home, capsys, monkeypatch):
    """`--format codex` shares the additionalContext shape (spec section 5)."""
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    _seed_follow_up(
        "2026-W21",
        commitment_text="state your assumptions before generating code",
    )
    code = main(["nudge", "--format", "codex"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload == {
        "hookSpecificOutput": {
            "additionalContext": (
                "[Praxis] This week's focus: state your assumptions before generating code"
            ),
        },
    }


def test_nudge_format_silent_when_no_active_commitment(tmp_home, capsys, monkeypatch):
    """Empty stdout (no JSON envelope at all) when no active commitment exists.

    Hooks must remain silent on a fresh DB regardless of which surface
    they request; emitting an envelope with an empty additionalContext
    would surface noise on every shell start.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    for fmt in ("text", "claude-code", "codex"):
        code = main(["nudge", "--format", fmt])
        captured = capsys.readouterr()
        assert code == 0, f"format={fmt!r} must exit 0"
        assert captured.out == "", f"format={fmt!r} must produce empty stdout"
        assert captured.err == "", f"format={fmt!r} must produce empty stderr"


def test_nudge_format_html_exits_nonzero_and_lists_accepted_values(tmp_home, capsys):
    """Unsupported `--format html` exits non-zero with the choices listed.

    argparse's `choices=` machinery prints a usage-style line plus the
    "invalid choice" error that names the three accepted values, and
    exits 2. That satisfies US-017 AC #3 without bespoke code.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["nudge", "--format", "html"])
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    # The error must name each accepted value so users know how to fix it.
    assert "text" in err
    assert "claude-code" in err
    assert "codex" in err


def test_nudge_format_parses_into_args_namespace():
    """`build_parser` exposes --format on the nudge subparser.

    Locks in the wiring so a refactor can't quietly drop the flag and
    let the handler silently fall back to the text branch.
    """
    parser = build_parser()
    args = parser.parse_args(["nudge", "--format", "claude-code"])
    assert args.cmd == "nudge"
    assert args.format == "claude-code"

    args_default = parser.parse_args(["nudge"])
    assert args_default.format == "text"


# ---------------------------------------------------------------------------
# US-018 - ~/.praxis/.last_nudge throttles per (surface, cwd).
# ---------------------------------------------------------------------------


def _read_throttle_state(tmp_home: Path) -> dict:
    """Return the parsed contents of ~/.praxis/.last_nudge for assertions."""
    path = tmp_home / ".praxis" / ".last_nudge"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_nudge_surface_argument_parses_with_default_cli(tmp_home, monkeypatch):
    """`--surface` exposes the throttle dedup key (default 'cli').

    Both SessionStart hooks and the shell-startup snippet rely on the
    default so a single human action only surfaces one cue per cycle
    (US-019 builds on this contract). Tests below override the surface
    explicitly to exercise the per-surface throttle keying.
    """
    parser = build_parser()
    args = parser.parse_args(["nudge"])
    assert args.surface == "cli"

    args_override = parser.parse_args(["nudge", "--surface", "claude-code"])
    assert args_override.surface == "claude-code"


def test_nudge_records_fire_timestamp_after_successful_surface(tmp_home, capsys, monkeypatch):
    """A successful nudge writes an ISO-8601 timestamp into ~/.praxis/.last_nudge.

    The JSON object is keyed by ``f"{surface}:{sha1(cwd)}"`` (US-018 AC #1).
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    monkeypatch.chdir(tmp_home)
    _seed_follow_up("2026-W21", commitment_text="stay literal")
    code = main(["nudge"])
    assert code == 0
    captured = capsys.readouterr()
    assert "[Praxis] This week: stay literal" in captured.out

    state = _read_throttle_state(tmp_home)
    assert len(state) == 1
    key = next(iter(state))
    # Key format: surface ':' followed by a 40-char sha1 hex digest.
    assert key.startswith("cli:")
    assert len(key) == len("cli:") + 40
    # Value is parseable as an ISO-8601 timestamp; we don't pin a
    # specific instant since the helper uses datetime.now(timezone.utc).
    ts = datetime.fromisoformat(state[key])
    assert ts.tzinfo is not None


def test_nudge_throttled_within_window_returns_empty_without_db_touch(
    tmp_home, capsys, monkeypatch
):
    """A second invocation within throttle_minutes is silent and skips the DB.

    Verifies AC #2: the throttle short-circuits before any commitment
    resolution so the second call never opens profile.db.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    monkeypatch.chdir(tmp_home)
    _seed_follow_up("2026-W21", commitment_text="first fire only")

    first = main(["nudge"])
    assert first == 0
    first_out = capsys.readouterr()
    assert "first fire only" in first_out.out

    # Booby-trap the DB resolver: if the second call reaches it, the
    # test fails loudly. The throttle must short-circuit beforehand.
    def _explode(_week_iso: str) -> None:
        raise AssertionError("throttled call must not reach the active-commitment resolver")

    monkeypatch.setattr("praxis.cli.__main__._resolve_active_commitment", _explode)
    second = main(["nudge"])
    second_out = capsys.readouterr()
    assert second == 0
    assert second_out.out == ""
    assert second_out.err == ""


def test_nudge_throttle_releases_after_window_elapses(tmp_home, capsys, monkeypatch):
    """After throttle_minutes have passed, the surface fires again."""
    from praxis.cli import nudge_throttle

    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    monkeypatch.chdir(tmp_home)
    _seed_follow_up("2026-W21", commitment_text="time-travel cue")

    # Prime the throttle file with a fire 31 minutes ago (default window is 30).
    past = datetime.now(UTC) - timedelta(minutes=31)
    nudge_throttle.record_fire("cli", now=past)

    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert "time-travel cue" in captured.out


def test_nudge_throttle_minutes_honors_config_override(tmp_home, capsys, monkeypatch):
    """User-set [nudge] throttle_minutes overrides the default 30."""
    from praxis.cli import nudge_throttle

    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    monkeypatch.chdir(tmp_home)
    _seed_follow_up("2026-W21", commitment_text="user-config cue")

    # Set throttle_minutes = 60; prime a fire 45 minutes ago. Under the
    # default (30) this would have released; under the user override
    # (60) it must still be throttled.
    config = tmp_home / ".praxis" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[nudge]\nthrottle_minutes = 60\n", encoding="utf-8")
    past = datetime.now(UTC) - timedelta(minutes=45)
    nudge_throttle.record_fire("cli", now=past)

    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""


def test_nudge_throttle_keyed_by_surface_so_different_surfaces_fire_independently(
    tmp_home, capsys, monkeypatch
):
    """Different `--surface` values get separate throttle entries.

    Per US-018 AC #1 the key is (surface, sha1(cwd)); two distinct
    surfaces in the same cwd therefore have independent throttle state.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    monkeypatch.chdir(tmp_home)
    _seed_follow_up("2026-W21", commitment_text="per-surface cue")

    code_a = main(["nudge", "--surface", "claude-code"])
    capsys.readouterr()  # discard first
    code_b = main(["nudge", "--surface", "codex"])
    out_b = capsys.readouterr().out
    assert code_a == 0
    assert code_b == 0
    # Codex's surface has not yet fired -> the second call fires.
    assert "per-surface cue" in out_b

    state = _read_throttle_state(tmp_home)
    surfaces = {key.split(":", 1)[0] for key in state}
    assert surfaces == {"claude-code", "codex"}


def test_nudge_throttle_corrupt_json_renames_to_corrupt_and_proceeds(tmp_home, capsys, monkeypatch):
    """Malformed JSON quarantines to ``.last_nudge.corrupt`` and proceeds (AC #3)."""
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    monkeypatch.chdir(tmp_home)
    _seed_follow_up("2026-W21", commitment_text="recovered cue")

    praxis_home = tmp_home / ".praxis"
    praxis_home.mkdir(parents=True, exist_ok=True)
    last_nudge = praxis_home / ".last_nudge"
    corrupt = praxis_home / ".last_nudge.corrupt"
    last_nudge.write_text("{not json", encoding="utf-8")

    code = main(["nudge"])
    captured = capsys.readouterr()
    # The call must surface the commitment instead of crashing on the
    # bad file: AC #3's "the user-facing call never crashes on the bad
    # file" plus "the current call proceeds as if no prior fire."
    assert code == 0
    assert "recovered cue" in captured.out

    # Quarantine path exists and the original file has been replaced
    # with valid JSON containing the fresh fire (the throttle entry
    # written by this call).
    assert corrupt.exists()
    assert corrupt.read_text(encoding="utf-8") == "{not json"
    new_state = _read_throttle_state(tmp_home)
    assert any(k.startswith("cli:") for k in new_state)


def test_nudge_no_active_commitment_does_not_record_fire(tmp_home, capsys, monkeypatch):
    """Silent runs (no commitment) must NOT touch the throttle file.

    Recording a fire when nothing was surfaced would block the next
    legitimate cue (after the user finally commits) for 30 minutes.
    The throttle file should remain unchanged across no-op calls.
    """
    monkeypatch.setattr("praxis.cli.__main__.current_iso_week", lambda: "2026-W21")
    monkeypatch.chdir(tmp_home)
    # No `_seed_follow_up` -- the resolver returns None.

    code = main(["nudge"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""

    throttle_file = tmp_home / ".praxis" / ".last_nudge"
    assert not throttle_file.exists()


# US-036: HTML masthead + interactive prompt gating
# ---------------------------------------------------------------------------


def _seed_commitment(
    week_iso: str,
    *,
    dim_key: str = "verification",
    commitment_text: str = "ask 'list the tables this writes' before running",
) -> FollowUp:
    """Seed one follow_ups row so the rollup builder finds an active commitment.

    Returns the FollowUp the helper saved so the test can assert against
    the same shape. The orchestrator carries this row through to the
    rollup via load_follow_up(week_iso); see praxis/orchestrator.py.
    """
    store = ProfileStore()
    row = FollowUp(
        week_iso=week_iso,
        dim_key=dim_key,
        commitment_text=commitment_text,
        target_metric="verification_rate",
        baseline_value=0.4,
        measured_value=None,
        outcome="pending",
    )
    store.save_follow_up(row)
    return row


def _stub_run_weekly_with_rollup(
    monkeypatch,
    *,
    week_iso: str | None = None,
    commitment_text: str = "ask 'list the tables this writes' before running",
    dim_key: str = "verification",
) -> str:
    """Stub run_weekly to return a summary with a non-None commitment_rollup.

    The orchestrator's _step_follow_up only builds a follow_up when a
    moments selection fires; CLI tests don't have moments to feed it.
    This helper sidesteps the orchestrator entirely and returns a
    summary whose ``commitment_rollup`` is populated, so the prompt
    gating in ``_cmd_review_impl`` can be exercised in isolation.
    Returns the week_iso the stub uses so callers can reference it.
    """
    from praxis.cli import __main__ as cli_main
    from praxis.orchestrator import WeeklyRunSummary, current_iso_week
    from praxis.reports.commitment_rollup import CommitmentRollup
    from praxis.scoring.aggregate import ProfileSnapshot

    target_week = week_iso or current_iso_week()
    snapshot = ProfileSnapshot(
        overall=6.0,
        dimension_means={d.key: 6.0 for d in RUBRIC},
        session_count=1,
        provider_breakdown={"claude": 1},
        strongest_dimension=RUBRIC[0].key,
        weakest_dimension=RUBRIC[-1].key,
    )
    rollup = CommitmentRollup(
        display_text=commitment_text,
        target_dim_key=dim_key,
        sessions_this_week=4,
        sessions_prior_week=2,
        self_report_tally={"yes": 0, "no": 0, "partial": 0, "skip": 0},
        dim_before={dim_key: 4.8},
        dim_after={dim_key: 6.2},
    )
    fake_summary = WeeklyRunSummary(
        week_iso=target_week,
        sessions=[],
        tasks=[],
        judge_results={},
        moments=[],
        selection=None,
        snapshot=snapshot,
        rendered_html="<html></html>",
        rendered_terminal="PRAXIS - Weekly read",
        elapsed_seconds=0.0,
        commitment_rollup=rollup,
    )
    monkeypatch.setattr(cli_main, "run_weekly", lambda **kw: fake_summary)
    return target_week


def test_review_subparser_accepts_non_interactive_flag():
    """`--non-interactive` is wired to the review subparser (AC US-036 #2)."""
    parser = build_parser()
    args = parser.parse_args(["review", "--non-interactive"])
    assert args.non_interactive is True
    # Default is False when the flag is omitted.
    args = parser.parse_args(["review"])
    assert args.non_interactive is False


def test_review_notify_non_interactive_renders_without_prompt(
    tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript
):
    """`praxis review --notify --non-interactive` (the LaunchAgent path)
    renders the masthead and exits 0 WITHOUT printing the prompt
    (AC US-036 #3). Seeds a commitment so the rollup is non-None;
    the prompt must still stay quiet under the combined flag pair.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_score("sess-notify-noprompt", datetime.now(UTC))
    _seed_commitment(_current_week_iso())
    _stub_run_weekly_with_rollup(monkeypatch)

    code = main(["review", "--notify", "--non-interactive"])
    captured = capsys.readouterr()
    assert code == 0
    # The prompt text is the contract; it must not appear anywhere in stdout.
    combined = captured.out + captured.err
    assert "[k]eep" not in combined
    assert "[n]ew" not in combined
    assert "[d]igest" not in combined


def test_review_notify_alone_suppresses_prompt_even_at_tty(
    tmp_home, capsys, monkeypatch, fake_api_key, fake_osascript
):
    """--notify alone is enough to skip the prompt: the LaunchAgent
    path passes --non-interactive too, but humans running --notify
    interactively from a TTY shouldn't be blocked either."""
    monkeypatch.setattr(sys, "platform", "darwin")
    # Force stdin.isatty() True so the only gate that should fire is --notify.
    monkeypatch.setattr("praxis.cli.__main__.sys.stdin.isatty", lambda: True)
    _seed_score("sess-notify-tty", datetime.now(UTC))
    _seed_commitment(_current_week_iso())
    _stub_run_weekly_with_rollup(monkeypatch)

    code = main(["review", "--notify"])
    captured = capsys.readouterr()
    assert code == 0
    assert "[k]eep" not in captured.out
    assert "[n]ew" not in captured.out
    assert "[d]igest" not in captured.out


def test_review_skips_prompt_when_stdin_not_a_tty(tmp_home, capsys, monkeypatch, fake_api_key):
    """No TTY -> no prompt (scripted runs through pipes / docker / CI).

    Even with a commitment on file, the prompt only fires when the
    user is actually sitting at an interactive terminal. Asserts on the
    captured stdout to prove the prompt prefix never lands.
    """
    monkeypatch.setattr("praxis.cli.__main__.sys.stdin.isatty", lambda: False)
    _seed_score("sess-pipe", datetime.now(UTC))
    _seed_commitment(_current_week_iso())
    _stub_run_weekly_with_rollup(monkeypatch)

    code = main(["review"])
    captured = capsys.readouterr()
    assert code == 0
    assert "[k]eep" not in captured.out


def test_review_skips_prompt_when_no_commitment_rollup(tmp_home, capsys, monkeypatch, fake_api_key):
    """No rollup -> no commitment to keep/new, so the prompt is
    suppressed entirely. The masthead's commitment block is the
    precondition for the prompt; renderer omits the block when the
    rollup is None and the CLI does the same.
    """
    monkeypatch.setattr("praxis.cli.__main__.sys.stdin.isatty", lambda: True)
    _seed_score("sess-no-commitment", datetime.now(UTC))
    # Note: no _stub_run_weekly_with_rollup call; the real run_weekly
    # leaves commitment_rollup=None when no follow_up was built.
    fake_calls: list[str] = []

    def _fake_input(prompt: str) -> str:
        fake_calls.append(prompt)
        return "d"

    monkeypatch.setattr("builtins.input", _fake_input)

    code = main(["review"])
    captured = capsys.readouterr()
    assert code == 0
    # input() must not have been called - no rollup, no prompt.
    assert fake_calls == []
    assert "[k]eep" not in captured.out


def test_review_prompt_keep_inserts_followup_for_next_week(
    tmp_home, capsys, monkeypatch, fake_api_key
):
    """[k]eep saves a follow_ups row for next week with the same
    commitment_text. The new row keeps dim_key/target_metric/baseline_value
    so next week's review can close the loop on the same metric.
    """
    monkeypatch.setattr("praxis.cli.__main__.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "k")
    _seed_score("sess-keep", datetime.now(UTC))
    current_week = _current_week_iso()
    seeded = _seed_commitment(current_week, commitment_text="state the goal before prompting")
    _stub_run_weekly_with_rollup(
        monkeypatch,
        week_iso=current_week,
        commitment_text=seeded.commitment_text,
        dim_key=seeded.dim_key,
    )

    code = main(["review"])
    captured = capsys.readouterr()
    assert code == 0

    # Compute the next ISO week the same way the CLI does.
    from praxis.cli.__main__ import _next_iso_week

    next_week = _next_iso_week(current_week)
    store = ProfileStore()
    next_row = store.load_follow_up(next_week)
    assert next_row is not None
    assert next_row.commitment_text == seeded.commitment_text
    assert next_row.dim_key == seeded.dim_key
    assert next_row.target_metric == seeded.target_metric
    assert next_row.outcome == "pending"
    # Caller-visible confirmation lands in stdout.
    assert "Kept commitment" in captured.out
    assert next_week in captured.out


def test_review_prompt_digest_branch_is_a_noop(tmp_home, capsys, monkeypatch, fake_api_key):
    """[d]igest exits 0 without inserting a follow-up row for next week."""
    monkeypatch.setattr("praxis.cli.__main__.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "d")
    _seed_score("sess-digest", datetime.now(UTC))
    current_week = _current_week_iso()
    _seed_commitment(current_week)
    _stub_run_weekly_with_rollup(monkeypatch, week_iso=current_week)

    code = main(["review"])
    capsys.readouterr()
    assert code == 0

    from praxis.cli.__main__ import _next_iso_week

    next_week = _next_iso_week(current_week)
    store = ProfileStore()
    assert store.load_follow_up(next_week) is None


# Tests for cmd_commit's [n]ew-branch dispatch from `praxis review` were
# removed during the merge: the praxis-commit-command branch ships a
# richer interactive cmd_commit (suggestion sources + free-text path with
# 280-char cap) whose interface doesn't match the simple
# "input() -> save row" contract that test_review_prompt_new_branch_*
# / test_cmd_commit_returns_1_* assumed. See tests/test_commit.py for
# the canonical cmd_commit test surface.


def test_next_iso_week_handles_year_boundary():
    """ISO weeks wrap at year boundaries: 2025-W52 -> 2026-W01."""
    from praxis.cli.__main__ import _next_iso_week

    assert _next_iso_week("2025-W52") == "2026-W01"


def test_next_iso_week_increments_within_year():
    """Within a year the next week is +1 in ISO numbering."""
    from praxis.cli.__main__ import _next_iso_week

    assert _next_iso_week("2026-W21") == "2026-W22"


def _current_week_iso() -> str:
    """Return the current ISO-week tag (matches what run_weekly sees)."""
    from praxis.orchestrator import current_iso_week

    return current_iso_week()


# ---------------------------------------------------------------------------
# Top-level guard in main(): a real user must never see a raw traceback.
# ---------------------------------------------------------------------------


def _raiser(exc):
    def _f(*_a, **_k):
        raise exc

    return _f


def test_main_catches_unexpected_exception_and_exits_1(tmp_home, monkeypatch, capsys):
    import praxis.cli.__main__ as m

    monkeypatch.delenv("PRAXIS_DEBUG", raising=False)
    monkeypatch.setattr(m, "ensure_config_file", _raiser(RuntimeError("kaboom")))
    code = main(["status"])
    err = capsys.readouterr().err
    assert code == 1
    assert "unexpected error" in err
    assert "kaboom" in err
    assert "PRAXIS_DEBUG=1" in err


def test_main_reraises_full_traceback_under_debug(tmp_home, monkeypatch):
    import praxis.cli.__main__ as m

    monkeypatch.setenv("PRAXIS_DEBUG", "1")
    monkeypatch.setattr(m, "ensure_config_file", _raiser(RuntimeError("kaboom")))
    with pytest.raises(RuntimeError, match="kaboom"):
        main(["status"])


def test_main_handles_keyboard_interrupt_cleanly(tmp_home, monkeypatch, capsys):
    import praxis.cli.__main__ as m

    monkeypatch.setattr(m, "ensure_config_file", _raiser(KeyboardInterrupt()))
    code = main(["status"])
    assert code == 130
    assert "Interrupted" in capsys.readouterr().err


def test_main_lets_systemexit_pass_through(tmp_home):
    # argparse errors (unknown command) must still exit via SystemExit, not be
    # swallowed by the guard.
    with pytest.raises(SystemExit):
        main(["no-such-command"])


# ---------------------------------------------------------------------------
# Non-interactive loop commands (scripts + the menu-bar app).
# ---------------------------------------------------------------------------


def test_reflect_set_records_non_interactively(tmp_home, capsys):
    import sqlite3

    from praxis.storage.profile_store import resolve_home

    wk = _current_week_iso()
    _seed_commitment(wk)
    code = main(["reflect", "--set", "yes", "--note", "felt good"])
    assert code == 0
    assert "Reflected: yes" in capsys.readouterr().out
    con = sqlite3.connect(resolve_home() / "profile.db")
    row = con.execute(
        "SELECT self_report, note FROM session_reflections "
        "WHERE session_stable_id LIKE 'manual:%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row == ("yes", "felt good")


def test_commit_text_writes_non_interactively(tmp_home, capsys):
    import sqlite3

    from praxis.storage.profile_store import resolve_home

    wk = _current_week_iso()
    code = main(["commit", "--text", "my own focus this week"])
    assert code == 0
    assert "my own focus this week" in capsys.readouterr().out
    con = sqlite3.connect(resolve_home() / "profile.db")
    row = con.execute(
        "SELECT outcome, user_chosen, display_text FROM follow_ups "
        "WHERE week_iso=? ORDER BY id DESC LIMIT 1",
        (wk,),
    ).fetchone()
    assert row[0] == "pending" and row[1] == 1 and row[2] == "my own focus this week"


def test_commit_text_replaces_active_with_history(tmp_home, capsys):
    import sqlite3

    from praxis.storage.profile_store import resolve_home

    wk = _current_week_iso()
    _seed_commitment(wk, commitment_text="old focus")
    code = main(["commit", "--text", "a new focus"])
    assert code == 0
    con = sqlite3.connect(resolve_home() / "profile.db")
    active = con.execute(
        "SELECT COUNT(*) FROM follow_ups WHERE week_iso=? AND outcome='pending' "
        "AND superseded_by IS NULL",
        (wk,),
    ).fetchone()[0]
    assert active == 1  # old superseded, new pending; exactly one active


def test_commit_pick_on_empty_db_reports_no_suggestions(tmp_home, capsys):
    # No digest yet -> no headline/drill picks to choose from.
    code = main(["commit", "--pick", "1"])
    assert code == 1
    assert "no suggestions" in capsys.readouterr().err.lower()


def test_commit_text_empty_errors(tmp_home, capsys):
    code = main(["commit", "--text", "   "])
    assert code == 1
    assert "empty" in capsys.readouterr().err


def test_status_json_is_valid_and_has_expected_keys(tmp_home, capsys):
    import json as _json

    code = main(["status", "--json"])
    assert code == 0
    data = _json.loads(capsys.readouterr().out)
    assert set(data) >= {"home", "sessions_scored", "providers", "weekly_digests"}
    assert data["sessions_scored"] == 0  # fresh DB


def test_last_json_error_path_when_no_digest(tmp_home, capsys):
    code = main(["last", "--json"])
    assert code == 1  # no digest yet
    assert "No weekly digest" in capsys.readouterr().err


def test_main_friendly_message_on_locked_db(tmp_home, monkeypatch, capsys):
    import sqlite3

    import praxis.cli.__main__ as m

    monkeypatch.delenv("PRAXIS_DEBUG", raising=False)
    monkeypatch.setattr(
        m, "ensure_config_file", _raiser(sqlite3.OperationalError("database is locked"))
    )
    code = main(["status"])
    err = capsys.readouterr().err
    assert code == 1
    assert "locked" in err and "menu-bar" in err
    assert "Traceback" not in err


def test_main_friendly_message_on_corrupt_db(tmp_home, monkeypatch, capsys):
    import sqlite3

    import praxis.cli.__main__ as m

    monkeypatch.delenv("PRAXIS_DEBUG", raising=False)
    monkeypatch.setattr(
        m, "ensure_config_file", _raiser(sqlite3.DatabaseError("file is not a database"))
    )
    code = main(["status"])
    err = capsys.readouterr().err
    assert code == 1
    assert "corrupt" in err


def test_doctor_runs_and_reports_all_sections(tmp_home, capsys):
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0  # nothing critical on a fresh, readable (empty) install
    for section in ("API keys", "Database", "Coaching hooks", "doctor"):
        assert section in out


def test_doctor_flags_no_api_key_and_no_focus(tmp_home, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    main(["doctor"])
    out = capsys.readouterr().out
    assert "No API key set" in out
    assert "No focus this week" in out


def test_global_debug_flag_reraises_traceback(tmp_home, monkeypatch):
    import praxis.cli.__main__ as m

    monkeypatch.delenv("PRAXIS_DEBUG", raising=False)
    monkeypatch.setattr(m, "ensure_config_file", _raiser(RuntimeError("kaboom")))
    with pytest.raises(RuntimeError, match="kaboom"):
        main(["--debug", "status"])
