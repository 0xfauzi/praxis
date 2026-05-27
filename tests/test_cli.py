"""Tests for the praxis CLI (US-047: `praxis follow-up`).

US-047 acceptance criteria:
  - praxis follow-up prints the most recent follow_ups row in human-readable form
  - Output includes commitment_text, baseline_value, measured_value (or '--'
    if pending), and outcome
  - Exits 0 when a follow-up exists, exits 3 with a clear message when none
    exist yet

Tests go through the argparse entry point (`praxis.cli.__main__.main`) so
the subparser registration is exercised end-to-end, not just the handler.
"""
from __future__ import annotations

from praxis.cli.__main__ import main
from praxis.follow_up import FollowUp
from praxis.storage.profile_store import ProfileStore


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
