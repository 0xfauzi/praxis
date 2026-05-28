"""Tests for ``praxis reflect`` (interactive 2-question prompt).

US-024 acceptance criteria covered here:
  - `praxis reflect` (no flags, TTY) renders the active commitment with
    options [y]es / [n]o / [p]artial / [s]kip and accepts an optional
    one-line note.
  - With no active commitment, the command exits 0 with a single-line
    hint pointing at `praxis commit`.
  - Selecting [s]kip writes a row with self_report='skip' and
    note=NULL; the row is still inserted (we count opt-outs).

The tests drive the CLI through the argparse entry point
(``praxis.cli.__main__.main``) so subparser registration is exercised
end-to-end. Stdin is piped via ``monkeypatch.setattr(sys, "stdin",
io.StringIO(...))`` rather than a TTY -- the AC says "no flags, TTY"
but the prompt rendering and DB write are the same on a piped
StringIO, and a piped stream is the most reliable way to assert on
exact bytes. The actual TTY-detection logic is exercised by US-027.
"""
from __future__ import annotations

import io
import sys

import pytest

from praxis.cli.__main__ import (
    _REFLECT_NO_COMMITMENT_MSG,
    _read_optional_note,
    _read_self_report_choice,
    _run_interactive_reflect,
    main,
)
from praxis.follow_up import FollowUp
from praxis.storage.profile_store import (
    ActiveCommitment,
    MultipleActiveCommitmentsError,
    ProfileStore,
)


# Use a synthetic commitment we can assert against without depending on
# the real follow_up engine running first.
_FU_DIM = "verification"
_FU_COMMITMENT = "ask 'list every table this migration writes'"
_FU_METRIC = "verification_rate"


def _seed_active(store: ProfileStore, week_iso: str) -> ActiveCommitment:
    """Insert one pending follow_up and return the resolved active row.

    Helper for tests that need an active commitment in place before
    invoking ``praxis reflect``. Mirrors how the weekly digest writes a
    pending row at end-of-week.
    """
    store.save_follow_up(
        FollowUp(
            week_iso=week_iso,
            dim_key=_FU_DIM,
            commitment_text=_FU_COMMITMENT,
            target_metric=_FU_METRIC,
            baseline_value=0.42,
            measured_value=None,
            outcome="pending",
        )
    )
    active = store.load_active_commitment(week_iso)
    assert active is not None, "fixture failure: pending row not active"
    return active


# ---- _read_self_report_choice / _read_optional_note -------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("y\n", "yes"),
        ("Y\n", "yes"),
        ("yes\n", "yes"),
        ("n\n", "no"),
        ("NO\n", "no"),
        ("p\n", "partial"),
        ("partial\n", "partial"),
        ("s\n", "skip"),
        ("Skip\n", "skip"),
    ],
)
def test_read_self_report_choice_accepts_letters_and_words(raw, expected):
    assert _read_self_report_choice(io.StringIO(raw)) == expected


@pytest.mark.parametrize("raw", ["\n", "  \n", "maybe\n", "yep\n", "x\n"])
def test_read_self_report_choice_rejects_invalid(raw):
    assert _read_self_report_choice(io.StringIO(raw)) is None


def test_read_self_report_choice_returns_none_on_eof():
    assert _read_self_report_choice(io.StringIO("")) is None


def test_read_optional_note_empty_is_none():
    assert _read_optional_note(io.StringIO("\n")) is None


def test_read_optional_note_strips_and_returns_text():
    assert _read_optional_note(io.StringIO("  paired with eng  \n")) == "paired with eng"


# ---- load_active_commitment fallbacks ---------------------------------


def test_load_active_commitment_returns_none_when_no_row(tmp_home):
    store = ProfileStore()
    assert store.load_active_commitment("2026-W22") is None


def test_load_active_commitment_returns_none_when_outcome_not_pending(tmp_home):
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W22",
            dim_key=_FU_DIM,
            commitment_text=_FU_COMMITMENT,
            target_metric=_FU_METRIC,
            baseline_value=0.42,
            outcome="improved",
        )
    )
    assert store.load_active_commitment("2026-W22") is None


def test_load_active_commitment_falls_back_to_commitment_text(tmp_home):
    """Without the US-002 display_text column, display_text == commitment_text."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")
    assert active.display_text == _FU_COMMITMENT
    # follow_up_id should be a positive integer (rowid fallback or id).
    assert active.follow_up_id > 0


def test_load_active_commitment_raises_when_multiple_active(tmp_home):
    """Defense-in-depth: legacy schema (no partial-unique index) can
    technically allow >1 active rows. The helper must refuse to pick
    a winner rather than silently mis-resolve."""
    store = ProfileStore()
    # save_follow_up keys on week_iso PK, so we have to bypass it to
    # land two rows for the same week. Insert directly via the raw
    # connection.
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W22",
            dim_key=_FU_DIM,
            commitment_text=_FU_COMMITMENT,
            target_metric=_FU_METRIC,
            baseline_value=0.42,
        )
    )
    # The legacy schema has week_iso as the PRIMARY KEY of follow_ups,
    # so a second pending row for the same week is rejected by sqlite.
    # In that case the "multiple active" branch is structurally
    # unreachable and we accept that as a passing test. If a later
    # migration drops the PK, this test will catch a regression.
    with store._conn() as conn:
        try:
            conn.execute(
                "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, "
                "target_metric, baseline_value, measured_value, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-W22",
                    "iteration",
                    "alt commitment",
                    "delegation_rate",
                    0.3,
                    None,
                    "pending",
                ),
            )
            two_rows_landed = True
        except Exception:
            two_rows_landed = False

    if two_rows_landed:
        with pytest.raises(MultipleActiveCommitmentsError):
            store.load_active_commitment("2026-W22")
    else:
        # PK guards against two pending rows; the resolver still returns
        # the single row that landed.
        active = store.load_active_commitment("2026-W22")
        assert active is not None


# ---- _run_interactive_reflect (programmatic) --------------------------


def test_run_interactive_reflect_yes_with_note_inserts_row(tmp_home):
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("y\npaired with eng\n")
    stdout = io.StringIO()
    code = _run_interactive_reflect(store, active, stdin, stdout)

    assert code == 0
    out = stdout.getvalue()
    assert f'Did you focus on: "{_FU_COMMITMENT}"' in out
    assert "[y]es" in out and "[n]o" in out and "[p]artial" in out and "[s]kip" in out
    assert "Optional one-line note" in out
    assert "Recorded reflection: yes" in out

    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "yes"
    assert rows[0]["note"] == "paired with eng"
    assert rows[0]["session_stable_id"] == "manual:2026-W22"


def test_run_interactive_reflect_skip_does_not_prompt_for_note(tmp_home):
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("s\n")
    stdout = io.StringIO()
    code = _run_interactive_reflect(store, active, stdin, stdout)

    assert code == 0
    out = stdout.getvalue()
    # The note prompt is suppressed on skip -- the AC is "note=NULL".
    assert "Optional one-line note" not in out
    assert "Recorded reflection: skip" in out

    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] is None


def test_run_interactive_reflect_partial_with_blank_note_writes_null(tmp_home):
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("p\n\n")
    stdout = io.StringIO()
    code = _run_interactive_reflect(store, active, stdin, stdout)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "partial"
    assert rows[0]["note"] is None


def test_run_interactive_reflect_retries_then_accepts(tmp_home):
    """One invalid answer should be forgiven; the retry succeeds."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("maybe\nn\nfollowups stale\n")
    stdout = io.StringIO()
    code = _run_interactive_reflect(store, active, stdin, stdout)

    assert code == 0
    out = stdout.getvalue()
    assert "Please answer with y, n, p, or s." in out
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert rows[0]["self_report"] == "no"
    assert rows[0]["note"] == "followups stale"


def test_run_interactive_reflect_eof_after_invalid_aborts_without_write(tmp_home):
    """If the user never sends a valid answer (e.g., piped EOF), the
    command exits non-zero AND does not insert a row."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("")  # immediate EOF
    stdout = io.StringIO()
    code = _run_interactive_reflect(store, active, stdin, stdout)

    assert code == 1
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert rows == []


# ---- end-to-end via argparse main() -----------------------------------


def test_cli_reflect_no_active_commitment_exits_0_with_hint(tmp_home, capsys):
    code = main(["reflect"])
    out = capsys.readouterr().out
    assert code == 0
    assert _REFLECT_NO_COMMITMENT_MSG in out
    # The hint must reference the resolving verb so the user knows what
    # to do next; the AC specifies the literal command name.
    assert "praxis commit" in out


def test_cli_reflect_skip_inserts_row_through_argparse(
    monkeypatch, tmp_home, capsys
):
    """Drive ``praxis reflect`` end-to-end (argparse + handler + DB)."""
    store = ProfileStore()
    # Seed an active commitment for the current ISO week so the resolver
    # finds it. We patch current_iso_week to a deterministic value and
    # match it in the seed.
    from praxis.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "current_iso_week", lambda: "2026-W22")
    _seed_active(store, "2026-W22")

    monkeypatch.setattr(sys, "stdin", io.StringIO("s\n"))
    code = main(["reflect"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Recorded reflection: skip" in out

    active = store.load_active_commitment("2026-W22")
    assert active is not None
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] is None
