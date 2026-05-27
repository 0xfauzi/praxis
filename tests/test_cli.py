"""Tests for the praxis CLI.

US-047 acceptance criteria (`praxis follow-up`):
  - praxis follow-up prints the most recent follow_ups row in human-readable form
  - Output includes commitment_text, baseline_value, measured_value (or '--'
    if pending), and outcome
  - Exits 0 when a follow-up exists, exits 3 with a clear message when none
    exist yet

US-074 acceptance criteria (`praxis week`):
  - `praxis week`, `--week <iso>`, `--dry-run`, `--frontier-only`,
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
  - `praxis week` / `praxis show` exit with code 3 and a clear message when
    the targeted ISO week has zero sessions (spec section 12.3)
  - The exit-2 gate (no API key) takes precedence over exit-3
  - `praxis scan` is the cron-driven data-mover and does NOT exit 3 on an
    empty window, so scheduled runs do not surface spurious failures

Tests go through the argparse entry point (`praxis.cli.__main__.main`) so
the subparser registration is exercised end-to-end, not just the handler.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
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

    The v0.2 surface (US-076) gates ``praxis week`` (current week) and
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
# US-074 - `praxis week` and its flags.
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


def test_week_subcommand_is_registered():
    """`praxis week` must exist as a subparser with the documented flags."""
    parser = build_parser()
    # Argparse raises SystemExit on parse errors; this success-case shape
    # exercises that every flag is recognized at the argparse layer.
    args = parser.parse_args(
        [
            "week",
            "--week",
            "2026-W21",
            "--dry-run",
            "--frontier-only",
            "--explain-judging",
            "--notify",
            "--write-html",
        ]
    )
    assert args.cmd == "week"
    assert args.week == "2026-W21"
    assert args.dry_run is True
    assert args.frontier_only is True
    assert args.explain_judging is True
    assert args.notify is True
    assert args.write_html is True


def test_week_with_data_renders_and_exits_zero(tmp_home, capsys, fake_api_key):
    """`praxis week` renders the digest masthead when sessions exist in the window."""
    _seed_score("sess-current", datetime.now(timezone.utc))
    code = main(["week"])
    out = capsys.readouterr().out
    assert code == 0
    # Masthead renders when there is data.
    assert "PRAXIS" in out


def test_week_iso_filters_to_target_week(tmp_home, capsys):
    """`--week 2026-W21` renders only sessions whose started_at falls inside that ISO week.

    Seeds three sessions across three different weeks; the digest's session
    count must reflect the single one that lives in 2026-W21.
    """
    # 2026-W21 spans Mon May 18 - Sun May 24, 2026.
    in_week = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    before_week = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    after_week = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    _seed_score("sess-in", in_week)
    _seed_score("sess-before", before_week)
    _seed_score("sess-after", after_week)

    code = main(["week", "--week", "2026-W21"])
    out = capsys.readouterr().out
    assert code == 0
    # The masthead's "N sessions in window" line reflects the snapshot
    # session_count after filtering to the requested week.
    assert "1 sessions in window" in out


def test_week_rejects_malformed_iso(tmp_home, capsys):
    """A malformed ISO-week string exits 1 with a clear error message."""
    code = main(["week", "--week", "not-a-week"])
    err = capsys.readouterr().err
    assert code == 1
    assert "YYYY-Www" in err


def test_week_write_html_creates_file(tmp_home, capsys):
    """--write-html writes to ~/.praxis/weeks/<iso>.html."""
    in_week = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    _seed_score("sess-html", in_week)
    code = main(["week", "--week", "2026-W21", "--write-html"])
    capsys.readouterr()  # flush captured output
    assert code == 0
    html_path = resolve_home() / "weeks" / "2026-W21.html"
    assert html_path.exists()
    assert "<html" in html_path.read_text(encoding="utf-8").lower()


def test_week_dry_run_does_not_create_html(tmp_home, capsys):
    """--dry-run without --write-html leaves no files behind.

    Seeds one current-week session so the run reaches the HTML-decision
    point (otherwise US-077's zero-session gate short-circuits to exit 3
    and the assertion would be vacuous).
    """
    _seed_score("sess-dry", datetime.now(timezone.utc))
    code = main(["week", "--dry-run"])
    capsys.readouterr()
    assert code == 0
    assert not (resolve_home() / "weeks").exists()


def test_week_explain_judging_prints_explainer(tmp_home, capsys, fake_api_key):
    """--explain-judging surfaces the pass-1 confidence block (or a clear stub).

    Seeds one current-week session so the digest renders past the
    US-077 zero-session gate and the explainer block is printed.
    """
    _seed_score("sess-explain", datetime.now(timezone.utc))
    code = main(["week", "--explain-judging"])
    out = capsys.readouterr().out
    assert code == 0
    assert "--explain-judging" in out


def test_week_explain_judging_notes_frontier_only(tmp_home, capsys, fake_api_key):
    """When both --frontier-only and --explain-judging are set, the explainer
    notes that pass-1 was skipped.

    Seeds one current-week session so the digest renders past the
    US-077 zero-session gate.
    """
    _seed_score("sess-frontier", datetime.now(timezone.utc))
    code = main(["week", "--frontier-only", "--explain-judging"])
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
    when = datetime.now(timezone.utc) - timedelta(hours=1)
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
    """Replace ``score_session`` with a deterministic stub returning 7.5.

    Mirrors test_orchestrator.fake_judge but locks the score so the
    re-score test can assert the row was overwritten by the new judge
    output (the seed row uses 4.0 so any change is detectable).
    """

    def _fake(session, prefer="claude"):  # noqa: ARG001
        return JudgeResult(
            dimension_scores={d.key: 7.5 for d in RUBRIC},
            rationale={d.key: "re-scored fixture" for d in RUBRIC},
            standout_moments=["re-score standout"],
            failure_modes=[],
            overall_note="re-scored",
            judge_model="fixture-frontier",
        )

    monkeypatch.setattr("praxis.scoring.aggregate.score_session", _fake)
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
    when = datetime.now(timezone.utc) - timedelta(days=20)
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
    _seed_score("h1", datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc))
    _seed_score("h2", datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc))
    _seed_score("h3", datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))

    code = main(["history"])
    out = capsys.readouterr().out
    assert code == 0
    assert "2026-W21" in out
    assert "2026-W19" in out
    # 2026-W21 has 2 sessions, 2026-W19 has 1; newest week appears first.
    assert out.index("2026-W21") < out.index("2026-W19")


def test_show_renders_past_week_and_exits_zero(tmp_home, capsys):
    """show <week_iso> renders the persisted snapshot for that week."""
    in_week = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    _seed_score("show-1", in_week)

    code = main(["show", "2026-W21"])
    out = capsys.readouterr().out
    assert code == 0
    # Masthead matches the week digest output.
    assert "PRAXIS" in out
    assert "1 sessions in window" in out


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
    """`praxis week` with no API keys exits 2 with a message naming both vars.

    tmp_home clears ANTHROPIC_API_KEY / OPENAI_API_KEY so this exercises
    the actual gate, not a stubbed version of it.
    """
    code = main(["week"])
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
    code = main(["week"])
    assert code != 2
    capsys.readouterr()


def test_week_with_only_openai_key_proceeds(tmp_home, capsys, monkeypatch):
    """OpenAI alone is also enough -- mirrors the Claude-only case.

    Exit 3 (no sessions in window) is acceptable; we only require that
    the API-key gate did not short-circuit the run with exit 2.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    code = main(["week"])
    assert code != 2
    capsys.readouterr()


def test_week_iso_bypasses_api_key_check(tmp_home, capsys):
    """`praxis week --week <iso>` is read-only; it must run without API keys.

    With no seeded data the past-week branch exits 3 (no sessions in
    window), which is the correct US-077 behavior. The test only asserts
    the API-key gate (exit 2) did not fire.
    """
    code = main(["week", "--week", "2026-W21"])
    capsys.readouterr()
    # No keys set, but the past-week branch never calls the judge.
    assert code != 2


def test_week_dry_run_bypasses_api_key_check(tmp_home, capsys):
    """`praxis week --dry-run` is read-only; it must run without API keys.

    With no seeded data the dry-run branch exits 3 (no sessions in
    window). The test only asserts the API-key gate did not fire.
    """
    code = main(["week", "--dry-run"])
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


def test_week_current_window_with_no_sessions_exits_3(
    tmp_home, capsys, fake_api_key
):
    """`praxis week` on an empty DB exits 3 with a clear message.

    With an API key set the exit-2 gate is bypassed; the exit-3 gate
    fires because the snapshot's session_count is zero. The message
    must identify what is missing so the user knows what to do next.
    """
    code = main(["week"])
    err = capsys.readouterr().err
    assert code == 3
    assert "No sessions found" in err


def test_week_past_week_with_no_sessions_exits_3(tmp_home, capsys):
    """`praxis week --week <iso>` exits 3 when zero rows fall in that week.

    No API key is required (the past-week branch is read-only) so this
    exercises the exit-3 gate in isolation from the exit-2 gate.
    """
    code = main(["week", "--week", "2026-W21"])
    err = capsys.readouterr().err
    assert code == 3
    # The message names the targeted ISO week so the user knows which
    # window was searched.
    assert "2026-W21" in err
    assert "No sessions found" in err


def test_week_dry_run_with_no_sessions_exits_3(tmp_home, capsys):
    """`praxis week --dry-run` exits 3 when the current week has no data.

    --dry-run is read-only (skips the exit-2 gate), so we land directly
    on the exit-3 gate when the DB is empty.
    """
    code = main(["week", "--dry-run"])
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
    code = main(["week"])
    err = capsys.readouterr().err
    assert code == 2
    assert "ANTHROPIC_API_KEY" in err


def test_week_with_seeded_session_does_not_exit_3(
    tmp_home, capsys, fake_api_key
):
    """Positive case: a seeded session in the current window yields exit 0."""
    _seed_score("sess-positive", datetime.now(timezone.utc))
    code = main(["week"])
    out = capsys.readouterr().out
    assert code == 0
    assert "PRAXIS" in out


def test_scan_with_no_sessions_does_not_exit_3(
    tmp_home, capsys, fake_api_key
):
    """`praxis scan` is the cron-driven data-mover, not a digest renderer.

    With zero sessions in the window scan still exits 0 (so the launchd /
    systemd / Task Scheduler job does not surface a spurious failure);
    the exit-3 contract is for ``praxis week`` / ``praxis show``.
    """
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Scanned" in out
