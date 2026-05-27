"""Tests for the praxis CLI (US-047: `praxis follow-up`, US-074: `praxis week`).

US-047 acceptance criteria:
  - praxis follow-up prints the most recent follow_ups row in human-readable form
  - Output includes commitment_text, baseline_value, measured_value (or '--'
    if pending), and outcome
  - Exits 0 when a follow-up exists, exits 3 with a clear message when none
    exist yet

US-074 acceptance criteria:
  - `praxis week`, `--week <iso>`, `--dry-run`, `--frontier-only`,
    `--explain-judging`, `--notify`, and `--write-html` are wired to
    the orchestrator with documented behavior
  - `--week` accepts ISO-week strings like 2026-W21 and renders a past
    week's data

Tests go through the argparse entry point (`praxis.cli.__main__.main`) so
the subparser registration is exercised end-to-end, not just the handler.
"""
from __future__ import annotations

from datetime import datetime, timezone

from praxis.cli.__main__ import build_parser, main
from praxis.follow_up import FollowUp
from praxis.scoring.aggregate import SessionScore
from praxis.scoring.features import SessionFeatures
from praxis.scoring.judge import JudgeResult
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


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


def test_week_with_no_data_exits_zero(tmp_home, capsys):
    """`praxis week` on an empty machine still renders and exits cleanly."""
    code = main(["week"])
    out = capsys.readouterr().out
    assert code == 0
    # Masthead renders even with zero sessions.
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
    """--dry-run without --write-html leaves no files behind."""
    code = main(["week", "--dry-run"])
    capsys.readouterr()
    assert code == 0
    assert not (resolve_home() / "weeks").exists()


def test_week_explain_judging_prints_explainer(tmp_home, capsys):
    """--explain-judging surfaces the pass-1 confidence block (or a clear stub)."""
    code = main(["week", "--explain-judging"])
    out = capsys.readouterr().out
    assert code == 0
    assert "--explain-judging" in out


def test_week_explain_judging_notes_frontier_only(tmp_home, capsys):
    """When both --frontier-only and --explain-judging are set, the explainer
    notes that pass-1 was skipped."""
    code = main(["week", "--frontier-only", "--explain-judging"])
    out = capsys.readouterr().out
    assert code == 0
    assert "pass-1 skipped" in out
