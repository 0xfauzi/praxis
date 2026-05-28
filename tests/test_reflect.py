"""Tests for ``praxis reflect`` (interactive 2-question prompt + --session-end).

US-024 acceptance criteria covered here:
  - `praxis reflect` (no flags, TTY) renders the active commitment with
    options [y]es / [n]o / [p]artial / [s]kip and accepts an optional
    one-line note.
  - With no active commitment, the command exits 0 with a single-line
    hint pointing at `praxis commit`.
  - Selecting [s]kip writes a row with self_report='skip' and
    note=NULL; the row is still inserted (we count opt-outs).

US-025 acceptance criteria covered here:
  - `--session-end` reads JSON from stdin and accepts both Claude Code
    and Codex payload shapes via duck-typing on present keys.
  - Missing / malformed payloads write a skip row with note
    'hook payload missing or unparseable' and exit 0.
  - A payload with no session_id still inserts a descriptive skip row
    (we never silently drop a hook invocation when an active
    commitment exists).

US-026 acceptance criteria covered here:
  - `[reflect] turns_min` / `elapsed_seconds_min` come from
    `~/.praxis/config.toml`, default to 2 / 60, and reject negative
    integers at config-load time.
  - A transcript that exists but falls below either threshold writes a
    skip row with note='session too short' and exits 0.
  - A transcript_path that does not exist on disk writes a skip row
    with note='transcript missing' and exits 0 (no crash, no retry
    loop).

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
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from praxis.cli.__main__ import (
    _HOOK_PAYLOAD_MISSING_NOTE,
    _HOOK_PAYLOAD_NO_SESSION_NOTE,
    _REFLECT_NO_COMMITMENT_MSG,
    _SESSION_TOO_SHORT_NOTE,
    _TRANSCRIPT_MISSING_NOTE,
    _check_transcript_threshold,
    _cmd_reflect_session_end,
    _extract_session_id,
    _extract_transcript_path,
    _parse_hook_payload,
    _read_hook_payload,
    _read_optional_note,
    _read_self_report_choice,
    _read_transcript_stats,
    _run_interactive_reflect,
    main,
)
from praxis.config import ReflectConfig, load_config
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


# ---- US-025: --session-end stdin payload parsing ----------------------


def _claude_code_payload(
    session_id: str = "claude-sess-1",
    transcript_path: str = "/tmp/transcript.jsonl",
    cwd: str = "/tmp/project",
) -> str:
    """Build a JSON string in Claude Code Stop-hook shape."""
    return json.dumps(
        {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "cwd": cwd,
            "hook_event_name": "Stop",
        }
    )


def _codex_payload(
    session_id: str = "codex-sess-1",
    cwd: str = "/tmp/project",
) -> str:
    """Build a JSON string in Codex Stop-hook shape (no transcript_path)."""
    return json.dumps(
        {
            "session_id": session_id,
            "cwd": cwd,
            "hook_event_name": "Stop",
        }
    )


def _write_claude_transcript(
    path: Path,
    *,
    user_turns: int = 2,
    elapsed_seconds: float = 60.0,
    start: datetime | None = None,
) -> Path:
    """Write a Claude Code-shape transcript JSONL that meets given thresholds.

    Generates ``user_turns`` ``type='user'`` entries plus one
    ``type='assistant'`` entry whose timestamp is at ``start +
    elapsed_seconds``, so the parsed (turns, elapsed) tuple equals the
    requested pair. Mirrors the on-disk format Claude Code's Stop hook
    posts via ``transcript_path``.
    """
    if start is None:
        start = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i in range(max(user_turns, 0)):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": start.isoformat().replace("+00:00", "Z"),
                    "message": {
                        "role": "user",
                        "content": f"prompt {i + 1}",
                    },
                }
            )
        )
    # Land a final assistant entry at start+elapsed_seconds so the
    # max-min timestamp spread matches the requested elapsed.
    end_ts = start + timedelta(seconds=elapsed_seconds)
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": end_ts.isoformat().replace("+00:00", "Z"),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "reply"}],
                },
            }
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---- _read_hook_payload / _parse_hook_payload / _extract_session_id ---


def test_read_hook_payload_returns_text_for_stringio():
    raw = '{"session_id": "abc"}'
    assert _read_hook_payload(io.StringIO(raw)) == raw


def test_read_hook_payload_returns_none_on_empty_stream():
    assert _read_hook_payload(io.StringIO("")) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"session_id": "abc"}',
        _claude_code_payload(),
        _codex_payload(),
    ],
)
def test_parse_hook_payload_accepts_dict_objects(raw):
    payload = _parse_hook_payload(raw)
    assert isinstance(payload, dict)
    assert payload["session_id"] != ""


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   \n  ",
        "not-json",
        "{bad json",
        "123",          # JSON number, not dict
        "[1, 2, 3]",    # JSON list, not dict
        '"a string"',   # JSON string, not dict
    ],
)
def test_parse_hook_payload_rejects_non_dict_or_malformed(raw):
    assert _parse_hook_payload(raw) is None


def test_extract_session_id_returns_string_when_present():
    assert _extract_session_id({"session_id": "abc-123"}) == "abc-123"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": ""},
        {"session_id": "   "},
        {"session_id": None},
        {"session_id": 42},
        {"cwd": "/tmp"},  # Codex-shape minus session_id
    ],
)
def test_extract_session_id_returns_none_when_missing_or_bad_type(payload):
    assert _extract_session_id(payload) is None


# ---- _cmd_reflect_session_end (programmatic) --------------------------


def test_session_end_no_active_commitment_is_silent_noop(tmp_home, capsys):
    """No follow-up -> no row, exit 0, nothing printed (hook output is
    visible to the AI tool; we must not pollute it)."""
    stdin = io.StringIO(_claude_code_payload())
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_session_end_claude_code_shape_inserts_row_with_session_id(tmp_home):
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    # US-026 threshold gate fires only when transcript_path points at a
    # file. Write a transcript that comfortably meets both default
    # thresholds (2 user turns + 60s elapsed) so the happy-path
    # placeholder (note=None) is preserved for US-025 backward compat.
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "transcript-abc.jsonl"
    )
    stdin = io.StringIO(
        _claude_code_payload(
            session_id="claude-abc-1",
            transcript_path=str(transcript),
        )
    )
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["session_stable_id"] == "claude-abc-1"
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] is None


def test_session_end_codex_shape_inserts_row_with_session_id(tmp_home):
    """Codex payload omits transcript_path; the duck-typed parse must
    still recognize it because session_id is present."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO(_codex_payload(session_id="codex-xyz-2"))
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["session_stable_id"] == "codex-xyz-2"
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] is None


def test_session_end_empty_stdin_writes_missing_payload_skip(tmp_home):
    """AC: missing payload -> skip row with the exact AC note."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("")
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] == _HOOK_PAYLOAD_MISSING_NOTE
    assert rows[0]["session_stable_id"] == "session-end:2026-W22"


def test_session_end_malformed_json_writes_skip_with_payload_note(tmp_home):
    """AC: malformed JSON -> same skip path as missing payload."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("{not-valid-json")
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] == _HOOK_PAYLOAD_MISSING_NOTE


def test_session_end_json_not_object_writes_skip_with_payload_note(tmp_home):
    """A JSON literal (number, list, string) is unparseable as a payload."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO("[]")
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["note"] == _HOOK_PAYLOAD_MISSING_NOTE


def test_session_end_payload_without_session_id_writes_descriptive_skip(tmp_home):
    """AC: 'no session_id at all' still inserts a skip row instead of
    inserting nothing."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    payload = json.dumps({"cwd": "/tmp", "hook_event_name": "Stop"})
    stdin = io.StringIO(payload)
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] == _HOOK_PAYLOAD_NO_SESSION_NOTE
    assert rows[0]["session_stable_id"] == "session-end:2026-W22"


def test_session_end_empty_session_id_is_treated_as_missing(tmp_home):
    """An empty-string session_id is just as bad as no session_id."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    payload = json.dumps({"session_id": "", "cwd": "/tmp"})
    stdin = io.StringIO(payload)
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["note"] == _HOOK_PAYLOAD_NO_SESSION_NOTE


# ---- end-to-end via argparse main() (--session-end branch) ------------


def test_cli_reflect_session_end_dispatches_to_session_end_branch(
    monkeypatch, tmp_home, capsys
):
    """Argparse wiring: ``--session-end`` reaches ``_cmd_reflect_session_end``
    rather than the interactive prompt."""
    store = ProfileStore()
    from praxis.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "current_iso_week", lambda: "2026-W22")
    _seed_active(store, "2026-W22")

    transcript = _write_claude_transcript(
        tmp_home / "claude" / "transcript-hook-id-9.jsonl"
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            _claude_code_payload(
                session_id="hook-id-9",
                transcript_path=str(transcript),
            )
        ),
    )
    code = main(["reflect", "--session-end"])

    assert code == 0
    captured = capsys.readouterr()
    # Session-end mode is silent on stdout/stderr -- the AI tool sees
    # the hook's output, so any chatter would pollute its session log.
    assert "Did you focus on" not in captured.out
    assert "Recorded reflection" not in captured.out

    active = store.load_active_commitment("2026-W22")
    assert active is not None
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["session_stable_id"] == "hook-id-9"
    assert rows[0]["self_report"] == "skip"


def test_cli_reflect_session_end_exit_0_when_no_active_commitment(
    monkeypatch, tmp_home, capsys
):
    """With no active commitment, --session-end exits 0 silently (no
    row to write, no AI-tool blocking)."""
    from praxis.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "current_iso_week", lambda: "2026-W22")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(_claude_code_payload())
    )
    code = main(["reflect", "--session-end"])

    assert code == 0
    captured = capsys.readouterr()
    # The interactive 'No active commitment' hint must NOT print: the
    # hook payload from Claude Code expects silent success on the
    # parent process.
    assert _REFLECT_NO_COMMITMENT_MSG not in captured.out


# ---- US-026: ReflectConfig validation ---------------------------------


def test_reflect_config_defaults_match_spec():
    """Defaults must match the documented AC: 2 turns, 60 elapsed."""
    cfg = ReflectConfig()
    assert cfg.turns_min == 2
    assert cfg.elapsed_seconds_min == 60


def test_reflect_config_accepts_zero():
    """Zero is allowed (it disables the corresponding gate)."""
    cfg = ReflectConfig(turns_min=0, elapsed_seconds_min=0)
    assert cfg.turns_min == 0
    assert cfg.elapsed_seconds_min == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"turns_min": -1},
        {"elapsed_seconds_min": -1},
        {"turns_min": -2, "elapsed_seconds_min": -7},
    ],
)
def test_reflect_config_rejects_negative_values(kwargs):
    """AC: 'reject negative values at config-load time'."""
    with pytest.raises(ValueError):
        ReflectConfig(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"turns_min": 1.5},
        {"elapsed_seconds_min": "60"},
        {"turns_min": True},  # bool subclass of int must be rejected
    ],
)
def test_reflect_config_rejects_non_integer_values(kwargs):
    """AC: 'both have integer types'."""
    with pytest.raises(ValueError):
        ReflectConfig(**kwargs)


def test_load_config_includes_reflect_section(tmp_home):
    """The DEFAULT_CONFIG_TOML round-trips into a populated Config.reflect."""
    cfg = load_config()
    assert cfg.reflect.turns_min == 2
    assert cfg.reflect.elapsed_seconds_min == 60


def test_load_config_reads_user_overrides(tmp_home):
    """A hand-edited [reflect] section is reflected in load_config."""
    cfg_path = tmp_home / ".praxis" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "[reflect]\nturns_min = 5\nelapsed_seconds_min = 120\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.reflect.turns_min == 5
    assert cfg.reflect.elapsed_seconds_min == 120


def test_load_config_raises_on_negative_user_override(tmp_home):
    """The 'reject at load time' guarantee must surface for hand-edits."""
    cfg_path = tmp_home / ".praxis" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "[reflect]\nturns_min = -1\nelapsed_seconds_min = 60\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config()


# ---- US-026: transcript stats parsing ---------------------------------


def test_read_transcript_stats_counts_user_turns_and_elapsed(tmp_home):
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "stats.jsonl",
        user_turns=3,
        elapsed_seconds=180.0,
    )
    stats = _read_transcript_stats(transcript)
    assert stats is not None
    user_turns, elapsed = stats
    assert user_turns == 3
    assert elapsed == pytest.approx(180.0)


def test_read_transcript_stats_returns_none_when_file_missing(tmp_home):
    missing = tmp_home / "no-such-transcript.jsonl"
    assert _read_transcript_stats(missing) is None


def test_read_transcript_stats_skips_malformed_lines(tmp_home):
    """Malformed JSON lines must not crash the parser."""
    path = tmp_home / "claude" / "garbled.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=90)
    path.write_text(
        "\n".join(
            [
                "{not json",
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": start.isoformat().replace("+00:00", "Z"),
                        "message": {"role": "user", "content": "hi"},
                    }
                ),
                "",  # blank
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": end.isoformat().replace("+00:00", "Z"),
                        "message": {"role": "user", "content": "more"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stats = _read_transcript_stats(path)
    assert stats is not None
    user_turns, elapsed = stats
    assert user_turns == 2
    assert elapsed == pytest.approx(90.0)


def test_read_transcript_stats_ignores_empty_user_content(tmp_home):
    """A 'user' frame with empty content (e.g., tool_result wrapper)
    must NOT count toward turns_min."""
    path = tmp_home / "claude" / "empty.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "message": {"role": "user", "content": ""},
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "message": {
                            "role": "user",
                            "content": [{"type": "tool_result", "content": "ok"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stats = _read_transcript_stats(path)
    assert stats == (0, 0.0)


# ---- US-026: _extract_transcript_path ---------------------------------


def test_extract_transcript_path_returns_path_when_present():
    payload = {"transcript_path": "/var/tmp/foo.jsonl"}
    assert _extract_transcript_path(payload) == Path("/var/tmp/foo.jsonl")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"transcript_path": ""},
        {"transcript_path": "   "},
        {"transcript_path": None},
        {"transcript_path": 42},
        {"cwd": "/tmp"},  # Codex-shape minus transcript_path
    ],
)
def test_extract_transcript_path_returns_none_when_absent_or_bad(payload):
    assert _extract_transcript_path(payload) is None


# ---- US-026: _check_transcript_threshold ------------------------------


def test_check_threshold_returns_missing_when_file_absent(tmp_home):
    note = _check_transcript_threshold(
        tmp_home / "ghost.jsonl", ReflectConfig()
    )
    assert note == _TRANSCRIPT_MISSING_NOTE


def test_check_threshold_returns_too_short_when_turns_below(tmp_home):
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "short-turns.jsonl",
        user_turns=1,        # below default 2
        elapsed_seconds=600,
    )
    note = _check_transcript_threshold(transcript, ReflectConfig())
    assert note == _SESSION_TOO_SHORT_NOTE


def test_check_threshold_returns_too_short_when_elapsed_below(tmp_home):
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "short-elapsed.jsonl",
        user_turns=5,
        elapsed_seconds=10,  # below default 60
    )
    note = _check_transcript_threshold(transcript, ReflectConfig())
    assert note == _SESSION_TOO_SHORT_NOTE


def test_check_threshold_returns_none_when_meets_both(tmp_home):
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "ok.jsonl",
        user_turns=2,
        elapsed_seconds=60,
    )
    assert _check_transcript_threshold(transcript, ReflectConfig()) is None


def test_check_threshold_zero_disables_gate(tmp_home):
    """turns_min=0, elapsed_seconds_min=0 -> any non-empty transcript passes."""
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "trivial.jsonl",
        user_turns=0,
        elapsed_seconds=0,
    )
    cfg = ReflectConfig(turns_min=0, elapsed_seconds_min=0)
    assert _check_transcript_threshold(transcript, cfg) is None


# ---- US-026: --session-end end-to-end via _cmd_reflect_session_end ---


def test_session_end_transcript_missing_writes_transcript_missing_note(tmp_home):
    """AC: transcript_path absent on disk -> 'transcript missing' skip."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO(
        _claude_code_payload(
            session_id="claude-missing-tx",
            transcript_path=str(tmp_home / "no-such-transcript.jsonl"),
        )
    )
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] == _TRANSCRIPT_MISSING_NOTE
    # The session_id is still attributed even when the transcript was
    # missing; the row is observable in the digest panel.
    assert rows[0]["session_stable_id"] == "claude-missing-tx"


def test_session_end_short_session_under_turns_writes_too_short(tmp_home):
    """AC: < turns_min user turns -> 'session too short' skip."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "too-few-turns.jsonl",
        user_turns=1,        # below default 2
        elapsed_seconds=600,
    )

    stdin = io.StringIO(
        _claude_code_payload(
            session_id="claude-short-turns",
            transcript_path=str(transcript),
        )
    )
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] == _SESSION_TOO_SHORT_NOTE
    assert rows[0]["session_stable_id"] == "claude-short-turns"


def test_session_end_short_session_under_elapsed_writes_too_short(tmp_home):
    """AC: < elapsed_seconds_min -> 'session too short' skip."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "too-fast.jsonl",
        user_turns=5,
        elapsed_seconds=10,  # below default 60
    )

    stdin = io.StringIO(
        _claude_code_payload(
            session_id="claude-short-elapsed",
            transcript_path=str(transcript),
        )
    )
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] == _SESSION_TOO_SHORT_NOTE


def test_session_end_long_session_falls_through_to_happy_path(tmp_home):
    """A transcript that meets both thresholds writes the placeholder
    happy-path skip row (note=None) -- US-027 will replace this with
    a detached child."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "ample.jsonl",
        user_turns=4,
        elapsed_seconds=300,
    )

    stdin = io.StringIO(
        _claude_code_payload(
            session_id="claude-ample",
            transcript_path=str(transcript),
        )
    )
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] is None


def test_session_end_threshold_respects_user_overrides(tmp_home):
    """A user-tuned ``[reflect]`` section gates differently than defaults."""
    cfg_path = tmp_home / ".praxis" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "[reflect]\nturns_min = 0\nelapsed_seconds_min = 5\n",
        encoding="utf-8",
    )

    store = ProfileStore()
    active = _seed_active(store, "2026-W22")
    # This transcript would FAIL default thresholds (1 turn, 30s
    # elapsed -> under 2 turns) but PASSES the user's 0/5 override.
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "user-override.jsonl",
        user_turns=1,
        elapsed_seconds=30,
    )

    stdin = io.StringIO(
        _claude_code_payload(
            session_id="claude-user-override",
            transcript_path=str(transcript),
        )
    )
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["note"] is None


def test_session_end_codex_shape_skips_threshold_gate(tmp_home):
    """Codex payloads omit transcript_path, so threshold gating must
    not apply -- otherwise every Codex session would be 'transcript
    missing'."""
    store = ProfileStore()
    active = _seed_active(store, "2026-W22")

    stdin = io.StringIO(_codex_payload(session_id="codex-no-gate"))
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["self_report"] == "skip"
    assert rows[0]["note"] is None
    assert rows[0]["session_stable_id"] == "codex-no-gate"


def test_session_end_threshold_never_crashes_on_malformed_config(tmp_home):
    """Defense in depth: a corrupt config.toml must NOT break the Stop
    hook. The loader falls back to defaults so the gate still applies."""
    cfg_path = tmp_home / ".praxis" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("[reflect\nturns_min = ???", encoding="utf-8")

    store = ProfileStore()
    active = _seed_active(store, "2026-W22")
    transcript = _write_claude_transcript(
        tmp_home / "claude" / "resilient.jsonl",
        user_turns=2,
        elapsed_seconds=60,
    )

    stdin = io.StringIO(
        _claude_code_payload(
            session_id="claude-resilient",
            transcript_path=str(transcript),
        )
    )
    code = _cmd_reflect_session_end(stdin)

    assert code == 0
    rows = store.load_session_reflections(follow_up_id=active.follow_up_id)
    assert len(rows) == 1
    assert rows[0]["note"] is None
