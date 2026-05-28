"""Tests for ``praxis install-coach`` (US-028, US-029).

Covers tool detection + per-tool prompt/flag routing (US-028) and the
Claude Code settings.json merger (US-029). US-030 / US-031 add their
own tests when those branches replace the matching placeholder in
``install_for_tool``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis.cli.__main__ import main
from praxis.cli.install_coach import (
    ALL_TOOLS,
    CLAUDE_HOOK_COMMANDS,
    SENTINEL,
    TOOL_CLAUDE_CODE,
    TOOL_CODEX,
    TOOL_COPILOT,
    InstallCoachError,
    claude_settings_path,
    detect_all,
    detect_claude_code,
    detect_codex,
    detect_copilot,
    display_name,
    install_claude_code,
    run_install_coach,
)


# ---------------------------------------------------------------------------
# Detection: Claude Code (~/.claude/settings.json OR ~/.claude/projects/).
# ---------------------------------------------------------------------------


def test_detect_claude_code_false_on_clean_home(tmp_home):
    assert detect_claude_code() is False


def test_detect_claude_code_true_when_settings_json_exists(tmp_home):
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}", encoding="utf-8")
    assert detect_claude_code() is True


def test_detect_claude_code_true_when_projects_dir_exists(tmp_home):
    projects = tmp_home / ".claude" / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    assert detect_claude_code() is True


def test_detect_claude_code_honors_home_override(tmp_path):
    """Explicit home= overrides Path.home() so tests can isolate state."""
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    assert detect_claude_code(home=tmp_path) is True
    other = Path("/nonexistent/no-claude")
    assert detect_claude_code(home=other) is False


# ---------------------------------------------------------------------------
# Detection: Codex (~/.codex/).
# ---------------------------------------------------------------------------


def test_detect_codex_false_on_clean_home(tmp_home):
    # tmp_home is the user dir; .codex hasn't been created yet by any fixture
    # here, so detection must be False.
    assert (tmp_home / ".codex").exists() is False
    assert detect_codex() is False


def test_detect_codex_true_when_codex_dir_exists(tmp_home):
    (tmp_home / ".codex").mkdir(parents=True, exist_ok=True)
    assert detect_codex() is True


# ---------------------------------------------------------------------------
# Detection: Copilot (VS Code workspace storage with chat artifacts).
# ---------------------------------------------------------------------------


def test_detect_copilot_false_with_no_user_dirs(tmp_home):
    """Empty dir list -> nothing found."""
    assert detect_copilot(vscode_user_dirs=[]) is False


def test_detect_copilot_false_when_workspace_storage_missing(tmp_home, tmp_path):
    user_dir = tmp_path / "Code" / "User"
    user_dir.mkdir(parents=True)
    assert detect_copilot(vscode_user_dirs=[user_dir]) is False


def test_detect_copilot_true_with_chat_sessions(tmp_home, tmp_path):
    user_dir = tmp_path / "Code" / "User"
    chat = user_dir / "workspaceStorage" / "abc" / "chatSessions"
    chat.mkdir(parents=True)
    (chat / "session1.json").write_text("{}", encoding="utf-8")
    assert detect_copilot(vscode_user_dirs=[user_dir]) is True


def test_detect_copilot_true_with_state_vscdb(tmp_home, tmp_path):
    """Older Copilot format: SQLite store at workspace root."""
    user_dir = tmp_path / "Code" / "User"
    ws = user_dir / "workspaceStorage" / "abc"
    ws.mkdir(parents=True)
    (ws / "state.vscdb").write_bytes(b"\x00")
    assert detect_copilot(vscode_user_dirs=[user_dir]) is True


# ---------------------------------------------------------------------------
# detect_all + display_name sanity.
# ---------------------------------------------------------------------------


def test_detect_all_returns_in_canonical_order(tmp_home, monkeypatch):
    """Detected tools come back in the ALL_TOOLS order, not file-creation order."""
    (tmp_home / ".codex").mkdir(parents=True)
    (tmp_home / ".claude" / "projects").mkdir(parents=True)
    # Force Copilot off so the test focuses on ordering of the two file-rooted
    # detectors regardless of host VS Code data.
    monkeypatch.setattr(
        "praxis.cli.install_coach.detect_copilot", lambda: False
    )
    assert detect_all() == [TOOL_CLAUDE_CODE, TOOL_CODEX]


def test_detect_all_empty_on_clean_home(tmp_home, monkeypatch):
    monkeypatch.setattr(
        "praxis.cli.install_coach.detect_copilot", lambda: False
    )
    assert detect_all() == []


def test_display_names_present_for_every_known_tool():
    for tool in ALL_TOOLS:
        # display_name must return something non-empty and not the raw slug
        # (the slug uses a hyphen; the human-facing label uses words/spaces).
        label = display_name(tool)
        assert label
        if tool == TOOL_CLAUDE_CODE:
            assert label == "Claude Code"
        if tool == TOOL_CODEX:
            assert label == "Codex"
        if tool == TOOL_COPILOT:
            assert label == "Copilot"


# ---------------------------------------------------------------------------
# CLI behavior: detection -> prompt -> install_for_tool dispatch.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_install(monkeypatch):
    """Record every install_for_tool dispatch instead of doing real work."""
    installed: list[str] = []

    def _record(tool: str) -> None:
        installed.append(tool)

    monkeypatch.setattr(
        "praxis.cli.install_coach.install_for_tool", _record
    )
    return installed


@pytest.fixture
def no_copilot(monkeypatch):
    """Force Copilot detection off so host VS Code data does not leak in.

    Detection on this iteration must be reproducible regardless of which
    machine the test runs on. The other two detectors are home-rooted and
    therefore already isolated by ``tmp_home``.
    """
    monkeypatch.setattr(
        "praxis.cli.install_coach.detect_copilot", lambda: False
    )


def test_no_tools_detected_prints_message_and_exits_zero(
    tmp_home, capsys, no_copilot
):
    code = main(["install-coach"])
    out = capsys.readouterr().out
    assert code == 0
    assert "No supported AI tools detected. Pass --all to install anyway." in out


def test_yes_flag_skips_prompts_for_detected_tools(
    tmp_home, capsys, stub_install, no_copilot
):
    (tmp_home / ".claude" / "projects").mkdir(parents=True)
    (tmp_home / ".codex").mkdir(parents=True)
    code = main(["install-coach", "--yes"])
    capsys.readouterr()
    assert code == 0
    # Both detected tools were dispatched without input().
    assert stub_install == [TOOL_CLAUDE_CODE, TOOL_CODEX]


def test_default_prompt_proceeds_on_empty_input(
    tmp_home, monkeypatch, capsys, stub_install, no_copilot
):
    """An empty answer ('' i.e. the user hits Return) is treated as yes."""
    (tmp_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    code = main(["install-coach"])
    capsys.readouterr()
    assert code == 0
    assert stub_install == [TOOL_CLAUDE_CODE]


def test_explicit_no_skips_install(
    tmp_home, monkeypatch, capsys, stub_install, no_copilot
):
    (tmp_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code = main(["install-coach"])
    capsys.readouterr()
    assert code == 0
    assert stub_install == []


def test_prompt_uses_found_tool_format(
    tmp_home, monkeypatch, capsys, stub_install, no_copilot
):
    """The exact AC string: 'Found <Tool>. Install the Praxis coaching hook? [Y/n]:'."""
    (tmp_home / ".claude" / "projects").mkdir(parents=True)
    seen_prompts: list[str] = []

    def _input(prompt):
        seen_prompts.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", _input)
    code = main(["install-coach"])
    capsys.readouterr()
    assert code == 0
    assert len(seen_prompts) == 1
    assert "Found Claude Code." in seen_prompts[0]
    assert "Install the Praxis coaching hook? [Y/n]:" in seen_prompts[0]


def test_tool_flag_restricts_to_named_tool_only(
    tmp_home, capsys, stub_install, no_copilot
):
    """--tool overrides detection and dispatches only the named tool."""
    # Claude is detected, Codex is not -- but --tool codex says: do Codex only.
    (tmp_home / ".claude" / "projects").mkdir(parents=True)
    code = main(["install-coach", "--tool", "codex", "--yes"])
    capsys.readouterr()
    assert code == 0
    assert stub_install == [TOOL_CODEX]


def test_tool_flag_rejects_unknown_tool(tmp_home, capsys, no_copilot):
    code = main(["install-coach", "--tool", "bogus", "--yes"])
    err = capsys.readouterr().err
    assert code == 1
    assert "Unknown --tool 'bogus'" in err
    # The valid set is named so the user can self-correct.
    assert "claude-code" in err
    assert "codex" in err
    assert "copilot" in err


def test_all_flag_iterates_every_tool_regardless_of_detection(
    tmp_home, capsys, stub_install
):
    """--all bypasses detection: every tool is offered/installed."""
    # No detection state at all; --all + --yes -> every tool installed.
    code = main(["install-coach", "--all", "--yes"])
    capsys.readouterr()
    assert code == 0
    assert stub_install == list(ALL_TOOLS)


def test_all_and_tool_are_mutually_exclusive(tmp_home, capsys):
    """argparse should reject --all + --tool combined."""
    with pytest.raises(SystemExit) as exc_info:
        main(["install-coach", "--all", "--tool", "codex"])
    # argparse exits 2 for usage errors; any non-zero is acceptable.
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Programmatic API (run_install_coach) -- bypasses argparse so the
# orchestration is testable without main().
# ---------------------------------------------------------------------------


def test_run_install_coach_assume_yes_iterates_detected(
    tmp_home, capsys, stub_install, no_copilot
):
    (tmp_home / ".codex").mkdir(parents=True)
    code = run_install_coach(assume_yes=True)
    capsys.readouterr()
    assert code == 0
    assert stub_install == [TOOL_CODEX]


def test_run_install_coach_no_detection_returns_zero(
    tmp_home, capsys, stub_install, no_copilot
):
    code = run_install_coach()
    out = capsys.readouterr().out
    assert code == 0
    assert "No supported AI tools detected" in out
    assert stub_install == []


def test_prompt_eof_treated_as_no(
    tmp_home, monkeypatch, capsys, stub_install, no_copilot
):
    """If stdin closes mid-prompt, the user is treated as declining."""
    (tmp_home / ".claude" / "projects").mkdir(parents=True)

    def _eof(_prompt):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _eof)
    code = main(["install-coach"])
    capsys.readouterr()
    assert code == 0
    assert stub_install == []


# ---------------------------------------------------------------------------
# US-029: Claude Code hook installer (settings.json merge + atomic write).
# ---------------------------------------------------------------------------


def _read_settings(tmp_home: Path) -> dict:
    return json.loads(
        (tmp_home / ".claude" / "settings.json").read_text(encoding="utf-8")
    )


def test_install_claude_code_creates_settings_on_clean_home(tmp_home):
    """No file -> writes both SessionStart and Stop blocks with the sentinel."""
    assert (tmp_home / ".claude" / "settings.json").exists() is False

    path = install_claude_code()

    assert path == tmp_home / ".claude" / "settings.json"
    data = _read_settings(tmp_home)
    assert "hooks" in data
    for event, expected_cmd in CLAUDE_HOOK_COMMANDS.items():
        blocks = data["hooks"][event]
        assert isinstance(blocks, list) and len(blocks) == 1
        block = blocks[0]
        assert block["matcher"] == "*"
        assert block[SENTINEL] is True
        # Block has exactly one command-type hook with the expected command.
        inner = block["hooks"]
        assert isinstance(inner, list) and len(inner) == 1
        assert inner[0] == {"type": "command", "command": expected_cmd}


def test_install_claude_code_session_start_command(tmp_home):
    install_claude_code()
    data = _read_settings(tmp_home)
    cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == "praxis nudge --format claude-code"


def test_install_claude_code_stop_command(tmp_home):
    install_claude_code()
    data = _read_settings(tmp_home)
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == "praxis reflect --session-end --non-interactive-fallback"


def test_install_claude_code_creates_parent_directory(tmp_home):
    """Missing ~/.claude/ is created (not an error)."""
    claude_dir = tmp_home / ".claude"
    assert claude_dir.exists() is False
    install_claude_code()
    assert claude_dir.exists()
    assert (claude_dir / "settings.json").exists()


def test_install_claude_code_preserves_unrelated_top_level_keys(tmp_home):
    """Any top-level key that isn't 'hooks' must survive a merge."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"theme": "dark", "telemetry": False}), encoding="utf-8"
    )

    install_claude_code()

    data = _read_settings(tmp_home)
    assert data["theme"] == "dark"
    assert data["telemetry"] is False
    assert "hooks" in data


def test_install_claude_code_preserves_unrelated_hook_events(tmp_home):
    """A user-authored hook at an event we don't touch is left intact."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    user_subagent = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "echo user-subagent"}],
    }
    settings.write_text(
        json.dumps({"hooks": {"SubagentStop": [user_subagent]}}),
        encoding="utf-8",
    )

    install_claude_code()

    data = _read_settings(tmp_home)
    # User's SubagentStop block is preserved byte-for-byte (post-roundtrip).
    assert data["hooks"]["SubagentStop"] == [user_subagent]


def test_install_claude_code_preserves_user_block_at_session_start(tmp_home):
    """A user's SessionStart block (no sentinel) is preserved alongside ours."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    user_block = {
        "matcher": "src/**",
        "hooks": [{"type": "command", "command": "echo user-on-start"}],
    }
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [user_block]}}),
        encoding="utf-8",
    )

    install_claude_code()

    data = _read_settings(tmp_home)
    session_blocks = data["hooks"]["SessionStart"]
    # User block first (unchanged), Praxis block appended.
    assert session_blocks[0] == user_block
    assert session_blocks[1][SENTINEL] is True
    assert session_blocks[1]["hooks"][0]["command"] == CLAUDE_HOOK_COMMANDS["SessionStart"]
    assert len(session_blocks) == 2


def test_install_claude_code_is_idempotent(tmp_home):
    """Re-running the installer does not duplicate the Praxis blocks."""
    install_claude_code()
    install_claude_code()
    install_claude_code()
    data = _read_settings(tmp_home)
    for event in CLAUDE_HOOK_COMMANDS:
        blocks = data["hooks"][event]
        # Exactly one Praxis-managed block per event, even after three installs.
        managed = [b for b in blocks if b.get(SENTINEL) is True]
        assert len(managed) == 1, f"{event}: expected 1 managed block, got {len(managed)}"
        assert len(blocks) == 1


def test_install_claude_code_replaces_stale_managed_block(tmp_home):
    """An old Praxis block with an outdated command is replaced, not duplicated."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    stale = {
        "matcher": "*",
        SENTINEL: True,
        "hooks": [{"type": "command", "command": "praxis nudge --old-flag"}],
    }
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [stale]}}),
        encoding="utf-8",
    )

    install_claude_code()

    data = _read_settings(tmp_home)
    blocks = data["hooks"]["SessionStart"]
    assert len(blocks) == 1
    assert blocks[0]["hooks"][0]["command"] == CLAUDE_HOOK_COMMANDS["SessionStart"]


def test_install_claude_code_aborts_on_unparseable_json(tmp_home):
    """Unparseable JSON -> InstallCoachError + file untouched."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    bad_bytes = b"{this is not: json,,"
    settings.write_bytes(bad_bytes)

    with pytest.raises(InstallCoachError) as exc_info:
        install_claude_code()

    # File is left exactly as it was (no partial write, no overwrite).
    assert settings.read_bytes() == bad_bytes
    msg = str(exc_info.value)
    assert "not valid JSON" in msg
    # Message points the user at the file and at the recovery command.
    assert str(settings) in msg
    assert "praxis install-coach" in msg


def test_install_claude_code_aborts_when_top_level_not_object(tmp_home):
    """A JSON file whose top level is a list is rejected (would lose user data)."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(InstallCoachError) as exc_info:
        install_claude_code()

    assert "JSON object" in str(exc_info.value)
    assert settings.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_install_claude_code_aborts_when_hooks_not_object(tmp_home):
    """Existing 'hooks' key with a non-object value is a structural error."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": "broken"}), encoding="utf-8")

    with pytest.raises(InstallCoachError) as exc_info:
        install_claude_code()

    assert "'hooks'" in str(exc_info.value)
    # File untouched.
    assert json.loads(settings.read_text(encoding="utf-8")) == {"hooks": "broken"}


def test_install_claude_code_empty_file_treated_as_fresh(tmp_home):
    """A pre-existing empty (or whitespace-only) file is treated like {}."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("   \n\t  \n", encoding="utf-8")

    install_claude_code()

    data = _read_settings(tmp_home)
    assert "hooks" in data
    for event in CLAUDE_HOOK_COMMANDS:
        assert any(b.get(SENTINEL) is True for b in data["hooks"][event])


def test_install_claude_code_atomic_write_leaves_no_tmp_file(tmp_home):
    """On success, no leftover .tmp/.partial files in ~/.claude/."""
    install_claude_code()
    install_claude_code()  # second run also keeps things tidy
    claude_dir = tmp_home / ".claude"
    leftovers = [p.name for p in claude_dir.iterdir() if p.name != "settings.json"]
    assert leftovers == []


def test_install_claude_code_home_override(tmp_path):
    """Explicit home= overrides Path.home() for direct callers."""
    other = tmp_path / "alt"
    other.mkdir()
    path = install_claude_code(home=other)
    assert path == other / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "hooks" in data


def test_install_claude_code_output_is_pretty_printed(tmp_home):
    """The file is written with newlines (indent=2) so users can hand-edit it."""
    install_claude_code()
    raw = (tmp_home / ".claude" / "settings.json").read_text(encoding="utf-8")
    # Pretty-print check: newlines exist, not a single dense line.
    assert raw.count("\n") > 5
    # And file ends with a trailing newline so editors/git are happy.
    assert raw.endswith("\n")


def test_claude_settings_path_uses_path_home(tmp_home):
    """claude_settings_path() respects Path.home() (i.e. tmp_home fixture)."""
    assert claude_settings_path() == tmp_home / ".claude" / "settings.json"


# ---------------------------------------------------------------------------
# CLI integration for the Claude Code branch (US-029 wired through main()).
# ---------------------------------------------------------------------------


def test_cli_install_coach_claude_writes_settings_and_prints_path(
    tmp_home, capsys, no_copilot
):
    """`praxis install-coach --tool claude-code --yes` writes the file."""
    code = main(["install-coach", "--tool", "claude-code", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    settings_path = tmp_home / ".claude" / "settings.json"
    assert settings_path.exists()
    assert str(settings_path) in out
    # Confirm the file has the expected shape.
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"][0][SENTINEL] is True
    assert data["hooks"]["Stop"][0][SENTINEL] is True


def test_cli_install_coach_claude_unparseable_prints_error_continues(
    tmp_home, capsys, no_copilot
):
    """Unparseable settings.json prints to stderr and CLI still exits 0."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("not-json", encoding="utf-8")

    code = main(["install-coach", "--tool", "claude-code", "--yes"])
    captured = capsys.readouterr()
    # AC: the installer aborts with a clear error; we exit 0 because the
    # outer install-coach command treats a bad config as recoverable for
    # other tools.
    assert code == 0
    assert "not valid JSON" in captured.err
    # And we never overwrote the bad file.
    assert settings.read_text(encoding="utf-8") == "not-json"


def test_cli_install_coach_default_path_writes_when_claude_detected(
    tmp_home, monkeypatch, capsys, no_copilot
):
    """With Claude Code detected, default flow (auto-yes via --yes) installs it."""
    # Detection trigger: presence of ~/.claude/projects/ is enough.
    (tmp_home / ".claude" / "projects").mkdir(parents=True)

    code = main(["install-coach", "--yes"])
    capsys.readouterr()
    assert code == 0
    assert (tmp_home / ".claude" / "settings.json").exists()
