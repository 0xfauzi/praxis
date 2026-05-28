"""Tests for ``praxis install-coach`` / ``uninstall-coach`` (US-028-032).

Covers tool detection + per-tool prompt/flag routing (US-028), the
Claude Code settings.json merger (US-029), the Codex hooks.json
installer (US-030), the Copilot markdown-block injector + optional
user-level prompt-file/settings.json patcher (US-031), and the
symmetric uninstall-coach surface that strips only Praxis-authored
content (US-032).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis.cli.__main__ import main
from praxis.cli.install_coach import (
    ALL_TOOLS,
    CLAUDE_HOOK_COMMANDS,
    CODEX_HOOK_COMMANDS,
    COPILOT_INSTRUCTION_FILENAME,
    COPILOT_MARKER_BEGIN,
    COPILOT_MARKER_END,
    COPILOT_SETTINGS_KEY,
    SENTINEL,
    TOOL_CLAUDE_CODE,
    TOOL_CODEX,
    TOOL_COPILOT,
    InstallCoachError,
    claude_settings_path,
    codex_hooks_path,
    copilot_workspace_path,
    detect_all,
    detect_claude_code,
    detect_codex,
    detect_copilot,
    detect_managed_claude_code,
    detect_managed_codex,
    detect_managed_copilot,
    detect_managed_tools,
    display_name,
    install_claude_code,
    install_codex,
    install_copilot_user_level,
    install_copilot_workspace,
    run_install_coach,
    run_uninstall_coach,
    uninstall_claude_code,
    uninstall_codex,
    uninstall_copilot_user_level,
    uninstall_copilot_workspace,
    vscode_user_dir,
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

    def _record(tool: str, **_kwargs) -> None:
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


# ---------------------------------------------------------------------------
# US-030: Codex CLI hook installer (~/.codex/hooks.json flat-list shape).
# ---------------------------------------------------------------------------


def _read_codex_hooks(tmp_home: Path) -> dict:
    return json.loads(
        (tmp_home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )


def test_install_codex_creates_hooks_file_on_clean_codex_dir(tmp_home):
    """No file -> writes SessionStart + Stop entries with the sentinel."""
    (tmp_home / ".codex").mkdir(parents=True)
    assert (tmp_home / ".codex" / "hooks.json").exists() is False

    path = install_codex()

    assert path == tmp_home / ".codex" / "hooks.json"
    data = _read_codex_hooks(tmp_home)
    assert isinstance(data["hooks"], list)
    events = [entry["event"] for entry in data["hooks"]]
    assert "SessionStart" in events
    assert "Stop" in events
    for entry in data["hooks"]:
        assert entry[SENTINEL] is True


def test_install_codex_session_start_command(tmp_home):
    (tmp_home / ".codex").mkdir(parents=True)
    install_codex()
    data = _read_codex_hooks(tmp_home)
    cmds = {entry["event"]: entry["command"] for entry in data["hooks"]}
    assert cmds["SessionStart"] == "praxis nudge --format codex"


def test_install_codex_stop_command(tmp_home):
    (tmp_home / ".codex").mkdir(parents=True)
    install_codex()
    data = _read_codex_hooks(tmp_home)
    cmds = {entry["event"]: entry["command"] for entry in data["hooks"]}
    assert (
        cmds["Stop"] == "praxis reflect --session-end --non-interactive-fallback"
    )


def test_install_codex_creates_parent_directory(tmp_home):
    """If ~/.codex/ is absent the installer creates it via mkdir(parents=True)."""
    codex_dir = tmp_home / ".codex"
    assert codex_dir.exists() is False
    install_codex()
    assert codex_dir.exists()
    assert (codex_dir / "hooks.json").exists()


def test_install_codex_is_idempotent(tmp_home):
    """Re-running the installer does not duplicate Praxis-managed entries."""
    (tmp_home / ".codex").mkdir(parents=True)
    install_codex()
    install_codex()
    install_codex()
    data = _read_codex_hooks(tmp_home)
    managed = [e for e in data["hooks"] if e.get(SENTINEL) is True]
    # Exactly one SessionStart + one Stop managed entry after three installs.
    assert len(managed) == 2
    events = sorted(e["event"] for e in managed)
    assert events == ["SessionStart", "Stop"]


def test_install_codex_replaces_stale_managed_entries(tmp_home):
    """An old Praxis entry with an outdated command is replaced, not duplicated."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    stale = {
        "event": "SessionStart",
        "command": "praxis nudge --old-flag",
        SENTINEL: True,
    }
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": [stale]}), encoding="utf-8"
    )

    install_codex()

    data = _read_codex_hooks(tmp_home)
    # The stale entry is gone; a fresh SessionStart + Stop are present.
    session_entries = [e for e in data["hooks"] if e["event"] == "SessionStart"]
    assert len(session_entries) == 1
    assert session_entries[0]["command"] == CODEX_HOOK_COMMANDS["SessionStart"]
    assert session_entries[0][SENTINEL] is True


def test_install_codex_preserves_user_authored_entries(tmp_home):
    """A user-authored entry (no sentinel) at any event is left intact."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    user_entry = {"event": "SessionStart", "command": "echo user-on-start"}
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": [user_entry]}), encoding="utf-8"
    )

    install_codex()

    data = _read_codex_hooks(tmp_home)
    # User entry survives (post-roundtrip) and our managed entries are appended.
    assert user_entry in data["hooks"]
    managed = [e for e in data["hooks"] if e.get(SENTINEL) is True]
    assert len(managed) == 2


def test_install_codex_preserves_unrelated_top_level_keys(tmp_home):
    """Any top-level key that isn't 'hooks' must survive a merge."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": [], "model": "gpt-5", "approvals": "manual"}),
        encoding="utf-8",
    )

    install_codex()

    data = _read_codex_hooks(tmp_home)
    assert data["model"] == "gpt-5"
    assert data["approvals"] == "manual"
    assert isinstance(data["hooks"], list)


def test_install_codex_aborts_on_unparseable_json(tmp_home):
    """Unparseable JSON -> InstallCoachError + file untouched."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    bad_bytes = b"{this is not: json,,"
    (codex_dir / "hooks.json").write_bytes(bad_bytes)

    with pytest.raises(InstallCoachError) as exc_info:
        install_codex()

    assert (codex_dir / "hooks.json").read_bytes() == bad_bytes
    msg = str(exc_info.value)
    assert "not valid JSON" in msg
    assert str(codex_dir / "hooks.json") in msg
    assert "praxis install-coach" in msg


def test_install_codex_aborts_when_top_level_not_object(tmp_home):
    """A JSON file whose top level is a list is rejected (would lose user data)."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(InstallCoachError) as exc_info:
        install_codex()

    assert "JSON object" in str(exc_info.value)
    assert (codex_dir / "hooks.json").read_text(encoding="utf-8") == "[1, 2, 3]"


def test_install_codex_aborts_when_hooks_not_list(tmp_home):
    """Existing 'hooks' key with a non-array value is a structural error."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": "broken"}), encoding="utf-8"
    )

    with pytest.raises(InstallCoachError) as exc_info:
        install_codex()

    assert "'hooks'" in str(exc_info.value)
    assert "JSON array" in str(exc_info.value)
    # File untouched.
    assert json.loads(
        (codex_dir / "hooks.json").read_text(encoding="utf-8")
    ) == {"hooks": "broken"}


def test_install_codex_empty_file_treated_as_fresh(tmp_home):
    """A pre-existing empty (or whitespace-only) file is treated like {"hooks": []}."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text("   \n\t  \n", encoding="utf-8")

    install_codex()

    data = _read_codex_hooks(tmp_home)
    managed = [e for e in data["hooks"] if e.get(SENTINEL) is True]
    assert len(managed) == 2


def test_install_codex_atomic_write_leaves_no_tmp_file(tmp_home):
    """On success, no leftover .tmp/.partial files in ~/.codex/."""
    install_codex()
    install_codex()
    codex_dir = tmp_home / ".codex"
    leftovers = [p.name for p in codex_dir.iterdir() if p.name != "hooks.json"]
    assert leftovers == []


def test_install_codex_home_override(tmp_path):
    """Explicit home= overrides Path.home() for direct callers."""
    other = tmp_path / "alt"
    other.mkdir()
    path = install_codex(home=other)
    assert path == other / ".codex" / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data["hooks"], list)
    managed = [e for e in data["hooks"] if e.get(SENTINEL) is True]
    assert len(managed) == 2


def test_install_codex_permission_denied_raises_clear_error(tmp_home, monkeypatch):
    """PermissionError from the atomic write -> InstallCoachError + no partial file.

    Patches ``_atomic_write_json`` to raise so the test is portable across
    platforms (chmod semantics on macOS/Linux vs Windows differ; the
    contract under test is the error-mapping, not the OS detail).
    """
    (tmp_home / ".codex").mkdir(parents=True)

    def _raise(_path, _data):
        raise PermissionError("permission denied")

    monkeypatch.setattr(
        "praxis.cli.install_coach._atomic_write_json", _raise
    )

    with pytest.raises(InstallCoachError) as exc_info:
        install_codex()

    msg = str(exc_info.value)
    assert "permission denied" in msg.lower()
    assert str(tmp_home / ".codex") in msg
    assert "praxis install-coach" in msg
    # No file was created on disk (the patched write never persisted anything).
    assert (tmp_home / ".codex" / "hooks.json").exists() is False


def test_codex_hooks_path_uses_path_home(tmp_home):
    """codex_hooks_path() respects Path.home() (i.e. tmp_home fixture)."""
    assert codex_hooks_path() == tmp_home / ".codex" / "hooks.json"


# ---------------------------------------------------------------------------
# CLI integration for the Codex branch (US-030 wired through main()).
# ---------------------------------------------------------------------------


def test_cli_install_coach_codex_writes_hooks_and_prints_path(
    tmp_home, capsys, no_copilot
):
    """`praxis install-coach --tool codex --yes` writes the hooks file."""
    code = main(["install-coach", "--tool", "codex", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    hooks_path = tmp_home / ".codex" / "hooks.json"
    assert hooks_path.exists()
    assert str(hooks_path) in out
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    managed = [e for e in data["hooks"] if e.get(SENTINEL) is True]
    assert len(managed) == 2


def test_cli_install_coach_codex_unparseable_prints_error_continues(
    tmp_home, capsys, no_copilot
):
    """Unparseable hooks.json prints to stderr and CLI still exits 0."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text("not-json", encoding="utf-8")

    code = main(["install-coach", "--tool", "codex", "--yes"])
    captured = capsys.readouterr()
    # AC: the installer aborts with a clear error; the outer install-coach
    # treats a bad config as recoverable for other tools and exits 0.
    assert code == 0
    assert "not valid JSON" in captured.err
    # And we never overwrote the bad file.
    assert (codex_dir / "hooks.json").read_text(encoding="utf-8") == "not-json"


def test_cli_install_coach_default_path_writes_when_codex_detected(
    tmp_home, capsys, no_copilot
):
    """With Codex detected, default flow (auto-yes via --yes) installs it."""
    (tmp_home / ".codex").mkdir(parents=True)

    code = main(["install-coach", "--yes"])
    capsys.readouterr()
    assert code == 0
    assert (tmp_home / ".codex" / "hooks.json").exists()


# ---------------------------------------------------------------------------
# US-031: Copilot file injection (workspace markdown + optional user-level
# prompt-file and settings.json patch).
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cwd(monkeypatch, tmp_path):
    """Redirect Path.cwd() so the dispatch layer writes inside tmp_path."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_copilot_workspace_path_uses_cwd(tmp_path, monkeypatch):
    """copilot_workspace_path() falls back to Path.cwd() when cwd= is None."""
    monkeypatch.chdir(tmp_path)
    assert copilot_workspace_path() == tmp_path / ".github" / "copilot-instructions.md"


def test_copilot_workspace_path_respects_explicit_cwd(tmp_path):
    other = tmp_path / "alt"
    other.mkdir()
    assert (
        copilot_workspace_path(cwd=other)
        == other / ".github" / "copilot-instructions.md"
    )


def test_install_copilot_workspace_creates_file_on_clean_cwd(tmp_path):
    """No file -> writes the marker-bounded Praxis block."""
    assert not (tmp_path / ".github" / "copilot-instructions.md").exists()

    path = install_copilot_workspace(cwd=tmp_path)

    assert path == tmp_path / ".github" / "copilot-instructions.md"
    content = path.read_text(encoding="utf-8")
    assert COPILOT_MARKER_BEGIN in content
    assert COPILOT_MARKER_END in content
    assert "praxis commit" in content


def test_install_copilot_workspace_creates_github_dir(tmp_path):
    """A missing .github/ is created by the installer."""
    assert not (tmp_path / ".github").exists()
    install_copilot_workspace(cwd=tmp_path)
    assert (tmp_path / ".github").is_dir()


def test_install_copilot_workspace_block_content_is_idempotent(tmp_path):
    """Re-running the installer twice produces the same bytes."""
    install_copilot_workspace(cwd=tmp_path)
    first = (tmp_path / ".github" / "copilot-instructions.md").read_bytes()
    install_copilot_workspace(cwd=tmp_path)
    second = (tmp_path / ".github" / "copilot-instructions.md").read_bytes()
    assert first == second


def test_install_copilot_workspace_replaces_existing_managed_block(tmp_path):
    """An existing praxisManaged block is replaced, not duplicated."""
    target_dir = tmp_path / ".github"
    target_dir.mkdir()
    target = target_dir / "copilot-instructions.md"
    target.write_text(
        f"{COPILOT_MARKER_BEGIN}\nOLD STALE CONTENT\n{COPILOT_MARKER_END}\n",
        encoding="utf-8",
    )

    install_copilot_workspace(cwd=tmp_path)

    content = target.read_text(encoding="utf-8")
    assert "OLD STALE CONTENT" not in content
    # Exactly one begin/end marker pair
    assert content.count(COPILOT_MARKER_BEGIN) == 1
    assert content.count(COPILOT_MARKER_END) == 1


def test_install_copilot_workspace_preserves_surrounding_content_byte_level(
    tmp_path,
):
    """Content outside the markers is preserved byte-for-byte (fixture test).

    The AC explicitly calls for a byte-level preservation assertion; this
    is that fixture. The leading and trailing slices of the file must be
    bit-identical after the install, only the bytes between (and
    including) the markers change.
    """
    target_dir = tmp_path / ".github"
    target_dir.mkdir()
    target = target_dir / "copilot-instructions.md"
    leading = (
        "# Project Instructions\n"
        "\n"
        "Be concise. Use type hints.\n"
        "Prefer composition over inheritance.\n"
        "\n"
    )
    trailing = (
        "\n"
        "## Style Guide\n"
        "- 80-char line limit\n"
        "- Two newlines between functions\n"
    )
    block = f"{COPILOT_MARKER_BEGIN}\nOLD STALE CONTENT\n{COPILOT_MARKER_END}"
    original_bytes = (leading + block + trailing).encode("utf-8")
    target.write_bytes(original_bytes)

    install_copilot_workspace(cwd=tmp_path)

    final_bytes = target.read_bytes()
    final = final_bytes.decode("utf-8")
    # Byte-level: leading slice unchanged.
    assert final.startswith(leading), "leading content was modified"
    # Byte-level: trailing slice unchanged.
    assert final.endswith(trailing), "trailing content was modified"
    # Block content was replaced (no stale content remains).
    assert "OLD STALE CONTENT" not in final
    # New praxis block is present and well-formed.
    assert "<!-- praxisManaged:begin -->" in final
    assert "<!-- praxisManaged:end -->" in final
    assert "praxis commit" in final


def test_install_copilot_workspace_appends_when_no_block(tmp_path):
    """Existing file without a praxisManaged block -> append with a blank line."""
    target_dir = tmp_path / ".github"
    target_dir.mkdir()
    target = target_dir / "copilot-instructions.md"
    user_content = "# User Instructions\n\nUse semantic commit messages.\n"
    target.write_text(user_content, encoding="utf-8")

    install_copilot_workspace(cwd=tmp_path)

    final = target.read_text(encoding="utf-8")
    # User content preserved at the top.
    assert final.startswith(user_content)
    # Praxis block appended.
    assert COPILOT_MARKER_BEGIN in final
    assert COPILOT_MARKER_END in final
    # There's a blank-line separator between user content and the block.
    assert "\n\n<!-- praxisManaged:begin -->" in final


def test_install_copilot_workspace_appends_to_file_without_trailing_newline(
    tmp_path,
):
    """A file without a trailing newline gets one inserted before the block."""
    target_dir = tmp_path / ".github"
    target_dir.mkdir()
    target = target_dir / "copilot-instructions.md"
    target.write_text("No trailing newline.", encoding="utf-8")

    install_copilot_workspace(cwd=tmp_path)

    final = target.read_text(encoding="utf-8")
    assert final.startswith("No trailing newline.")
    # Block is separated from user content by at least one blank line.
    assert "\n\n<!-- praxisManaged:begin -->" in final


def test_install_copilot_workspace_atomic_write_leaves_no_tmp_file(tmp_path):
    """On success, no leftover .tmp files in <cwd>/.github/."""
    install_copilot_workspace(cwd=tmp_path)
    install_copilot_workspace(cwd=tmp_path)
    github_dir = tmp_path / ".github"
    leftovers = [
        p.name for p in github_dir.iterdir() if p.name != "copilot-instructions.md"
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# vscode_user_dir() per-platform resolution.
# ---------------------------------------------------------------------------


def test_vscode_user_dir_darwin(tmp_path, monkeypatch):
    """macOS path is ~/Library/Application Support/Code/User."""
    monkeypatch.setattr(
        "praxis.cli.install_coach.platform.system", lambda: "Darwin"
    )
    path = vscode_user_dir(home=tmp_path)
    assert path == tmp_path / "Library" / "Application Support" / "Code" / "User"


def test_vscode_user_dir_linux(tmp_path, monkeypatch):
    """Linux path is ~/.config/Code/User."""
    monkeypatch.setattr(
        "praxis.cli.install_coach.platform.system", lambda: "Linux"
    )
    path = vscode_user_dir(home=tmp_path)
    assert path == tmp_path / ".config" / "Code" / "User"


def test_vscode_user_dir_windows_uses_appdata(tmp_path, monkeypatch):
    """Windows path honors APPDATA env var."""
    monkeypatch.setattr(
        "praxis.cli.install_coach.platform.system", lambda: "Windows"
    )
    fake_appdata = tmp_path / "fake_appdata"
    fake_appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    path = vscode_user_dir(home=tmp_path)
    assert path == fake_appdata / "Code" / "User"


def test_vscode_user_dir_windows_falls_back_when_appdata_unset(
    tmp_path, monkeypatch
):
    """Windows without APPDATA falls back to ~/AppData/Roaming/Code/User."""
    monkeypatch.setattr(
        "praxis.cli.install_coach.platform.system", lambda: "Windows"
    )
    monkeypatch.delenv("APPDATA", raising=False)
    path = vscode_user_dir(home=tmp_path)
    assert path == tmp_path / "AppData" / "Roaming" / "Code" / "User"


# ---------------------------------------------------------------------------
# install_copilot_user_level: prompt file + settings.json patcher.
# ---------------------------------------------------------------------------


def _darwin_user_dir(home: Path) -> Path:
    return home / "Library" / "Application Support" / "Code" / "User"


@pytest.fixture
def force_darwin(monkeypatch):
    """Pin platform.system() to 'Darwin' so user-level paths are deterministic."""
    monkeypatch.setattr(
        "praxis.cli.install_coach.platform.system", lambda: "Darwin"
    )


def test_install_copilot_user_level_writes_prompt_file_and_patches_settings(
    tmp_home, force_darwin
):
    prompt_path, settings_path = install_copilot_user_level()

    user_dir = _darwin_user_dir(tmp_home)
    expected_prompt = user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME
    expected_settings = user_dir / "settings.json"
    assert prompt_path == expected_prompt
    assert settings_path == expected_settings
    assert prompt_path.exists()
    # The prompt file content includes the praxis-managed block.
    prompt_content = prompt_path.read_text(encoding="utf-8")
    assert COPILOT_MARKER_BEGIN in prompt_content
    assert COPILOT_MARKER_END in prompt_content
    # Settings file enables the prompts directory.
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    locations = data[COPILOT_SETTINGS_KEY]
    assert isinstance(locations, dict)
    assert locations[str(user_dir / "prompts")] is True


def test_install_copilot_user_level_creates_parent_dirs(tmp_home, force_darwin):
    """vscode user dir + prompts subdir are created if absent."""
    user_dir = _darwin_user_dir(tmp_home)
    assert not user_dir.exists()
    install_copilot_user_level()
    assert (user_dir / "prompts").is_dir()
    assert (user_dir / "settings.json").exists()


def test_install_copilot_user_level_preserves_existing_settings(
    tmp_home, force_darwin
):
    """Unrelated top-level keys + existing chat.instructionsFilesLocations entries survive."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    existing = {
        "editor.fontSize": 14,
        "files.autoSave": "onFocusChange",
        COPILOT_SETTINGS_KEY: {
            "/Users/other/prompts": True,
        },
    }
    (user_dir / "settings.json").write_text(
        json.dumps(existing), encoding="utf-8"
    )

    install_copilot_user_level()

    data = json.loads(
        (user_dir / "settings.json").read_text(encoding="utf-8")
    )
    assert data["editor.fontSize"] == 14
    assert data["files.autoSave"] == "onFocusChange"
    locations = data[COPILOT_SETTINGS_KEY]
    # The existing entry is preserved.
    assert locations["/Users/other/prompts"] is True
    # And the praxis prompts directory is added.
    assert locations[str(user_dir / "prompts")] is True


def test_install_copilot_user_level_is_idempotent(tmp_home, force_darwin):
    """Re-running does not duplicate the prompts-dir entry."""
    install_copilot_user_level()
    install_copilot_user_level()
    install_copilot_user_level()

    user_dir = _darwin_user_dir(tmp_home)
    data = json.loads(
        (user_dir / "settings.json").read_text(encoding="utf-8")
    )
    locations = data[COPILOT_SETTINGS_KEY]
    # Exactly one entry pointing at the praxis prompts dir.
    matching = [k for k in locations if k == str(user_dir / "prompts")]
    assert matching == [str(user_dir / "prompts")]


def test_install_copilot_user_level_aborts_on_unparseable_settings(
    tmp_home, force_darwin
):
    """Unparseable user settings.json -> InstallCoachError + file untouched."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    bad_bytes = b"{not valid json,,"
    (user_dir / "settings.json").write_bytes(bad_bytes)

    with pytest.raises(InstallCoachError) as exc_info:
        install_copilot_user_level()

    assert (user_dir / "settings.json").read_bytes() == bad_bytes
    msg = str(exc_info.value)
    assert "not valid JSON" in msg
    assert str(user_dir / "settings.json") in msg
    assert "praxis install-coach" in msg


def test_install_copilot_user_level_aborts_when_settings_top_level_not_object(
    tmp_home, force_darwin
):
    """Top-level JSON array is structurally invalid -> InstallCoachError."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    (user_dir / "settings.json").write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(InstallCoachError) as exc_info:
        install_copilot_user_level()

    assert "JSON object" in str(exc_info.value)
    assert (
        (user_dir / "settings.json").read_text(encoding="utf-8") == "[1, 2]"
    )


def test_install_copilot_user_level_empty_settings_treated_as_fresh(
    tmp_home, force_darwin
):
    """Whitespace-only settings.json is treated as {}."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    (user_dir / "settings.json").write_text("   \n\t  \n", encoding="utf-8")

    install_copilot_user_level()

    data = json.loads(
        (user_dir / "settings.json").read_text(encoding="utf-8")
    )
    assert COPILOT_SETTINGS_KEY in data


def test_install_copilot_user_level_overwrites_non_dict_locations(
    tmp_home, force_darwin
):
    """When 'chat.instructionsFilesLocations' is not a dict, install replaces it.

    A user with a malformed entry (e.g., a list or string) shouldn't cause
    a crash; install replaces it with a proper dict containing the praxis
    prompts dir entry.
    """
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    (user_dir / "settings.json").write_text(
        json.dumps({COPILOT_SETTINGS_KEY: ["unexpected", "list"]}),
        encoding="utf-8",
    )

    install_copilot_user_level()

    data = json.loads(
        (user_dir / "settings.json").read_text(encoding="utf-8")
    )
    locations = data[COPILOT_SETTINGS_KEY]
    assert isinstance(locations, dict)
    assert locations[str(user_dir / "prompts")] is True


def test_install_copilot_user_level_home_override(tmp_path, force_darwin):
    """Explicit home= overrides Path.home() for direct callers."""
    other = tmp_path / "alt-home"
    other.mkdir()
    prompt_path, settings_path = install_copilot_user_level(home=other)
    assert (
        prompt_path == _darwin_user_dir(other) / "prompts" / COPILOT_INSTRUCTION_FILENAME
    )
    assert settings_path == _darwin_user_dir(other) / "settings.json"


# ---------------------------------------------------------------------------
# CLI integration for the Copilot branch.
# ---------------------------------------------------------------------------


def test_cli_install_coach_copilot_workspace_with_yes(
    tmp_home, tmp_cwd, capsys, no_copilot
):
    """`praxis install-coach --tool copilot --yes` writes only the workspace surface.

    Per AC: --yes does NOT opt into the user-level surface (explicit
    confirmation required).
    """
    code = main(["install-coach", "--tool", "copilot", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    workspace_file = tmp_cwd / ".github" / "copilot-instructions.md"
    assert workspace_file.exists()
    # The user-level surface was NOT touched because --yes does not opt in.
    # On macOS the path would be tmp_home/Library/Application Support/Code/User
    user_dir_mac = tmp_home / "Library" / "Application Support" / "Code" / "User"
    user_dir_linux = tmp_home / ".config" / "Code" / "User"
    assert not (user_dir_mac / "prompts" / COPILOT_INSTRUCTION_FILENAME).exists()
    assert not (user_dir_linux / "prompts" / COPILOT_INSTRUCTION_FILENAME).exists()
    # Output mentions the workspace file written.
    assert str(workspace_file) in out


def test_cli_install_coach_copilot_user_level_with_explicit_yes(
    tmp_home, tmp_cwd, monkeypatch, capsys, force_darwin, no_copilot
):
    """Interactive flow that confirms both prompts writes both surfaces."""
    replies = iter(["y", "y", "y"])  # top-level, workspace, user-level

    def _input(_prompt):
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)

    code = main(["install-coach", "--tool", "copilot"])
    capsys.readouterr()
    assert code == 0
    assert (tmp_cwd / ".github" / "copilot-instructions.md").exists()
    user_dir = _darwin_user_dir(tmp_home)
    assert (user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME).exists()
    assert (user_dir / "settings.json").exists()


def test_cli_install_coach_copilot_decline_user_level_skips_it(
    tmp_home, tmp_cwd, monkeypatch, capsys, force_darwin, no_copilot
):
    """Declining the user-level prompt skips both user-level paths."""
    replies = iter(["y", "y", "n"])  # top-level Y, workspace Y, user-level N

    def _input(_prompt):
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)

    code = main(["install-coach", "--tool", "copilot"])
    capsys.readouterr()
    assert code == 0
    assert (tmp_cwd / ".github" / "copilot-instructions.md").exists()
    user_dir = _darwin_user_dir(tmp_home)
    assert not (user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME).exists()
    assert not (user_dir / "settings.json").exists()


def test_cli_install_coach_copilot_decline_workspace_does_not_write_workspace(
    tmp_home, tmp_cwd, monkeypatch, capsys, force_darwin, no_copilot
):
    """Declining the workspace prompt leaves <cwd>/.github untouched."""
    replies = iter(["y", "n", "n"])  # top-level Y, workspace N, user-level N

    def _input(_prompt):
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)

    code = main(["install-coach", "--tool", "copilot"])
    capsys.readouterr()
    assert code == 0
    assert not (tmp_cwd / ".github").exists()


def test_cli_install_coach_copilot_workspace_prompt_mentions_cwd_path(
    tmp_home, tmp_cwd, monkeypatch, capsys, force_darwin, no_copilot
):
    """The workspace prompt names the absolute target path."""
    seen: list[str] = []
    # Say YES to the top-level so we reach the workspace prompt; then NO
    # to the workspace and user-level prompts so we don't actually write.
    replies = iter(["y", "n", "n"])

    def _input(prompt):
        seen.append(prompt)
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)

    code = main(["install-coach", "--tool", "copilot"])
    capsys.readouterr()
    assert code == 0
    # We expect: top-level Found Copilot. + workspace prompt + user-level prompt.
    assert any("Found Copilot." in p for p in seen)
    workspace_target = tmp_cwd / ".github" / "copilot-instructions.md"
    assert any(str(workspace_target) in p for p in seen)


def test_cli_install_coach_copilot_user_level_prompt_default_no(
    tmp_home, tmp_cwd, monkeypatch, capsys, force_darwin, no_copilot
):
    """Hitting Enter at the user-level prompt declines (default-N)."""
    # Top-level Y, workspace Y, user-level "" (default-N => skipped).
    replies = iter(["y", "y", ""])

    def _input(_prompt):
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)

    code = main(["install-coach", "--tool", "copilot"])
    capsys.readouterr()
    assert code == 0
    user_dir = _darwin_user_dir(tmp_home)
    assert not (user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME).exists()
    assert not (user_dir / "settings.json").exists()


def test_cli_install_coach_copilot_user_level_prompt_default_yes_for_workspace(
    tmp_home, tmp_cwd, monkeypatch, capsys, force_darwin, no_copilot
):
    """Empty input at workspace prompt is treated as YES (default-Y)."""
    # Top-level "" => default Y, workspace "" => default Y, user-level "" => default N.
    replies = iter(["", "", ""])

    def _input(_prompt):
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)

    code = main(["install-coach", "--tool", "copilot"])
    capsys.readouterr()
    assert code == 0
    # Workspace surface WAS written.
    assert (tmp_cwd / ".github" / "copilot-instructions.md").exists()


def test_cli_install_coach_copilot_user_level_unparseable_prints_error_continues(
    tmp_home, tmp_cwd, monkeypatch, capsys, force_darwin, no_copilot
):
    """Unparseable user settings.json -> stderr message, CLI still exits 0."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    (user_dir / "settings.json").write_text("not-json", encoding="utf-8")

    replies = iter(["y", "y", "y"])  # top-level Y, workspace Y, user-level Y

    def _input(_prompt):
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)

    code = main(["install-coach", "--tool", "copilot"])
    captured = capsys.readouterr()
    assert code == 0
    assert "not valid JSON" in captured.err
    # The user-level settings.json was NOT overwritten.
    assert (user_dir / "settings.json").read_text(encoding="utf-8") == "not-json"
    # The workspace surface still landed (independent of user-level failure).
    assert (tmp_cwd / ".github" / "copilot-instructions.md").exists()


# ===========================================================================
# US-032: ``praxis uninstall-coach`` -- symmetric teardown.
# ===========================================================================


# ---------------------------------------------------------------------------
# Per-tool uninstall: Claude Code.
# ---------------------------------------------------------------------------


def test_uninstall_claude_code_returns_false_on_clean_home(tmp_home):
    """No settings.json file -> nothing to remove, no writes."""
    assert (tmp_home / ".claude" / "settings.json").exists() is False
    assert uninstall_claude_code() is False
    # And we did not create the file as a side effect.
    assert (tmp_home / ".claude" / "settings.json").exists() is False


def test_uninstall_claude_code_after_install_returns_true(tmp_home):
    install_claude_code()
    assert uninstall_claude_code() is True


def test_uninstall_claude_code_deletes_file_when_only_praxis_content(tmp_home):
    """If the file contained only Praxis hooks, it is deleted entirely.

    This is the "deep-equal to fresh install" property: a clean machine
    has no settings.json, so after install + uninstall the file should
    not exist either.
    """
    install_claude_code()
    assert (tmp_home / ".claude" / "settings.json").exists()
    uninstall_claude_code()
    assert (tmp_home / ".claude" / "settings.json").exists() is False


def test_uninstall_claude_code_preserves_unrelated_top_level_keys(tmp_home):
    """Top-level keys outside ``hooks`` survive the uninstall."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"theme": "dark", "telemetry": False}), encoding="utf-8"
    )
    install_claude_code()
    uninstall_claude_code()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"theme": "dark", "telemetry": False}


def test_uninstall_claude_code_preserves_user_blocks_at_managed_event(tmp_home):
    """A user-authored block alongside ours at SessionStart is preserved."""
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
    install_claude_code()  # appends our managed block alongside user_block
    uninstall_claude_code()

    data = json.loads(settings.read_text(encoding="utf-8"))
    # User's SessionStart block is preserved exactly.
    assert data["hooks"]["SessionStart"] == [user_block]


def test_uninstall_claude_code_preserves_user_events(tmp_home):
    """Hook events we never managed (e.g., SubagentStop) are untouched."""
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
    uninstall_claude_code()

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"]["SubagentStop"] == [user_subagent]
    # And our managed events were cleaned up.
    assert "SessionStart" not in data["hooks"]
    assert "Stop" not in data["hooks"]


def test_uninstall_claude_code_idempotent(tmp_home):
    """A second uninstall after the first is a no-op (False, no writes)."""
    install_claude_code()
    assert uninstall_claude_code() is True
    assert uninstall_claude_code() is False
    # Subsequent calls remain no-ops too.
    assert uninstall_claude_code() is False


def test_uninstall_claude_code_aborts_on_unparseable_json(tmp_home):
    """Unparseable JSON -> InstallCoachError + file untouched."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    bad_bytes = b"{this is not: json,,"
    settings.write_bytes(bad_bytes)

    with pytest.raises(InstallCoachError) as exc_info:
        uninstall_claude_code()

    assert settings.read_bytes() == bad_bytes
    msg = str(exc_info.value)
    assert "not valid JSON" in msg
    assert str(settings) in msg
    assert "praxis uninstall-coach" in msg


def test_uninstall_claude_code_aborts_when_top_level_not_object(tmp_home):
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(InstallCoachError) as exc_info:
        uninstall_claude_code()

    assert "JSON object" in str(exc_info.value)
    assert settings.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_uninstall_claude_code_no_op_when_hooks_missing(tmp_home):
    """A settings.json that has unrelated keys but no hooks -> False, no writes."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    original_bytes = settings.read_bytes()

    assert uninstall_claude_code() is False
    # File unchanged byte-for-byte.
    assert settings.read_bytes() == original_bytes


def test_uninstall_claude_code_home_override(tmp_path):
    """Explicit home= overrides Path.home() for direct callers."""
    other = tmp_path / "alt"
    other.mkdir()
    install_claude_code(home=other)
    assert uninstall_claude_code(home=other) is True
    assert (other / ".claude" / "settings.json").exists() is False


def test_uninstall_claude_code_atomic_write_leaves_no_tmp_file(tmp_home):
    """When the file is partially cleaned (not deleted), no stray tmp file remains."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"theme": "dark"}), encoding="utf-8"
    )
    install_claude_code()
    uninstall_claude_code()
    claude_dir = tmp_home / ".claude"
    leftovers = [
        p.name for p in claude_dir.iterdir() if p.name != "settings.json"
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Per-tool uninstall: Codex.
# ---------------------------------------------------------------------------


def test_uninstall_codex_returns_false_on_clean_home(tmp_home):
    """No hooks.json -> False, no writes."""
    assert (tmp_home / ".codex" / "hooks.json").exists() is False
    assert uninstall_codex() is False
    assert (tmp_home / ".codex" / "hooks.json").exists() is False


def test_uninstall_codex_after_install_returns_true(tmp_home):
    install_codex()
    assert uninstall_codex() is True


def test_uninstall_codex_deletes_file_when_only_praxis_content(tmp_home):
    install_codex()
    assert (tmp_home / ".codex" / "hooks.json").exists()
    uninstall_codex()
    assert (tmp_home / ".codex" / "hooks.json").exists() is False


def test_uninstall_codex_preserves_user_authored_entries(tmp_home):
    """User-authored entries (no sentinel) survive uninstall."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    user_entry = {"event": "SessionStart", "command": "echo user-on-start"}
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": [user_entry]}), encoding="utf-8"
    )
    install_codex()
    uninstall_codex()

    data = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
    assert data["hooks"] == [user_entry]


def test_uninstall_codex_preserves_unrelated_top_level_keys(tmp_home):
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": [], "model": "gpt-5"}), encoding="utf-8"
    )
    install_codex()
    uninstall_codex()

    data = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
    # 'hooks' key is removed since it would be empty; other keys preserved.
    assert data == {"model": "gpt-5"}


def test_uninstall_codex_idempotent(tmp_home):
    install_codex()
    assert uninstall_codex() is True
    assert uninstall_codex() is False
    assert uninstall_codex() is False


def test_uninstall_codex_aborts_on_unparseable_json(tmp_home):
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    bad_bytes = b"{this is not: json,,"
    (codex_dir / "hooks.json").write_bytes(bad_bytes)

    with pytest.raises(InstallCoachError) as exc_info:
        uninstall_codex()

    assert (codex_dir / "hooks.json").read_bytes() == bad_bytes
    msg = str(exc_info.value)
    assert "not valid JSON" in msg
    assert "praxis uninstall-coach" in msg


def test_uninstall_codex_aborts_when_top_level_not_object(tmp_home):
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(InstallCoachError) as exc_info:
        uninstall_codex()

    assert "JSON object" in str(exc_info.value)
    assert (codex_dir / "hooks.json").read_text(encoding="utf-8") == "[1, 2, 3]"


def test_uninstall_codex_no_op_when_hooks_missing(tmp_home):
    """hooks.json with unrelated keys but no 'hooks' key -> False, no writes."""
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps({"model": "gpt-5"}), encoding="utf-8"
    )
    original_bytes = (codex_dir / "hooks.json").read_bytes()
    assert uninstall_codex() is False
    assert (codex_dir / "hooks.json").read_bytes() == original_bytes


def test_uninstall_codex_home_override(tmp_path):
    other = tmp_path / "alt"
    other.mkdir()
    install_codex(home=other)
    assert uninstall_codex(home=other) is True
    assert (other / ".codex" / "hooks.json").exists() is False


# ---------------------------------------------------------------------------
# Per-tool uninstall: Copilot workspace surface.
# ---------------------------------------------------------------------------


def test_uninstall_copilot_workspace_returns_false_on_absent_file(tmp_path):
    assert not (tmp_path / ".github" / "copilot-instructions.md").exists()
    assert uninstall_copilot_workspace(cwd=tmp_path) is False
    # Did not create the file as a side effect.
    assert not (tmp_path / ".github" / "copilot-instructions.md").exists()


def test_uninstall_copilot_workspace_returns_false_when_no_markers(tmp_path):
    """File exists but has no praxisManaged block -> False, no writes."""
    target_dir = tmp_path / ".github"
    target_dir.mkdir()
    target = target_dir / "copilot-instructions.md"
    user_content = "# User Instructions\n\nNo praxis content here.\n"
    target.write_text(user_content, encoding="utf-8")
    original_bytes = target.read_bytes()

    assert uninstall_copilot_workspace(cwd=tmp_path) is False
    # File untouched byte-for-byte.
    assert target.read_bytes() == original_bytes


def test_uninstall_copilot_workspace_deletes_file_when_only_praxis_block(
    tmp_path,
):
    """A file containing only the praxis block is deleted on uninstall."""
    install_copilot_workspace(cwd=tmp_path)
    target = tmp_path / ".github" / "copilot-instructions.md"
    assert target.exists()

    assert uninstall_copilot_workspace(cwd=tmp_path) is True
    assert not target.exists()


def test_uninstall_copilot_workspace_preserves_surrounding_content_byte_level(
    tmp_path,
):
    """User content outside the markers must be preserved byte-for-byte.

    The leading and trailing slices of the file must be bit-identical
    after uninstall; only the block (and the preceding blank-line
    separator) is removed.
    """
    target_dir = tmp_path / ".github"
    target_dir.mkdir()
    target = target_dir / "copilot-instructions.md"
    leading = (
        "# Project Instructions\n"
        "\n"
        "Be concise. Use type hints.\n"
    )
    trailing = (
        "\n"
        "## Style Guide\n"
        "- 80-char line limit\n"
    )
    target.write_text(leading + trailing, encoding="utf-8")

    install_copilot_workspace(cwd=tmp_path)
    # Sanity: install inserted our block.
    assert COPILOT_MARKER_BEGIN in target.read_text(encoding="utf-8")

    uninstall_copilot_workspace(cwd=tmp_path)

    final = target.read_text(encoding="utf-8")
    # Praxis content is fully gone.
    assert COPILOT_MARKER_BEGIN not in final
    assert COPILOT_MARKER_END not in final
    assert "praxis commit" not in final
    # The original surrounding bytes are preserved.
    assert leading in final
    assert trailing in final


def test_uninstall_copilot_workspace_idempotent(tmp_path):
    install_copilot_workspace(cwd=tmp_path)
    assert uninstall_copilot_workspace(cwd=tmp_path) is True
    assert uninstall_copilot_workspace(cwd=tmp_path) is False
    assert uninstall_copilot_workspace(cwd=tmp_path) is False


def test_uninstall_copilot_workspace_preserves_user_block_at_start(tmp_path):
    """An existing managed block at start of file removes the block + trailing newline."""
    target_dir = tmp_path / ".github"
    target_dir.mkdir()
    target = target_dir / "copilot-instructions.md"
    # Praxis block at start, user content after.
    install_copilot_workspace(cwd=tmp_path)
    block_content = target.read_text(encoding="utf-8")
    # Append some user-authored content.
    target.write_text(
        block_content + "## User notes\nBe nice.\n", encoding="utf-8"
    )

    assert uninstall_copilot_workspace(cwd=tmp_path) is True

    final = target.read_text(encoding="utf-8")
    assert COPILOT_MARKER_BEGIN not in final
    assert "User notes" in final
    assert "Be nice." in final


# ---------------------------------------------------------------------------
# Per-tool uninstall: Copilot user-level (prompt file + settings.json).
# ---------------------------------------------------------------------------


def test_uninstall_copilot_user_level_returns_false_on_clean_home(
    tmp_home, force_darwin
):
    """No prompt file and no settings entry -> False, no writes."""
    user_dir = _darwin_user_dir(tmp_home)
    assert not user_dir.exists()
    assert uninstall_copilot_user_level() is False
    assert not user_dir.exists()


def test_uninstall_copilot_user_level_after_install_returns_true(
    tmp_home, force_darwin
):
    install_copilot_user_level()
    assert uninstall_copilot_user_level() is True


def test_uninstall_copilot_user_level_deletes_prompt_file(
    tmp_home, force_darwin
):
    install_copilot_user_level()
    user_dir = _darwin_user_dir(tmp_home)
    prompt_path = user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME
    assert prompt_path.exists()

    uninstall_copilot_user_level()
    assert not prompt_path.exists()


def test_uninstall_copilot_user_level_removes_settings_entry(
    tmp_home, force_darwin
):
    """The praxis prompts-dir entry is dropped from chat.instructionsFilesLocations."""
    install_copilot_user_level()
    user_dir = _darwin_user_dir(tmp_home)
    settings_path = user_dir / "settings.json"

    uninstall_copilot_user_level()

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    # Our key is gone; the locations key is removed entirely since it became empty.
    assert COPILOT_SETTINGS_KEY not in data


def test_uninstall_copilot_user_level_preserves_other_locations(
    tmp_home, force_darwin
):
    """Existing entries in chat.instructionsFilesLocations survive uninstall."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    existing = {
        COPILOT_SETTINGS_KEY: {"/Users/other/prompts": True},
    }
    (user_dir / "settings.json").write_text(
        json.dumps(existing), encoding="utf-8"
    )
    install_copilot_user_level()
    uninstall_copilot_user_level()

    data = json.loads((user_dir / "settings.json").read_text(encoding="utf-8"))
    # Our entry gone; the other one remains.
    locations = data[COPILOT_SETTINGS_KEY]
    assert locations == {"/Users/other/prompts": True}


def test_uninstall_copilot_user_level_preserves_other_settings_keys(
    tmp_home, force_darwin
):
    """Unrelated settings (editor, files, etc.) survive uninstall."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    existing = {
        "editor.fontSize": 14,
        "files.autoSave": "onFocusChange",
    }
    (user_dir / "settings.json").write_text(
        json.dumps(existing), encoding="utf-8"
    )
    install_copilot_user_level()
    uninstall_copilot_user_level()

    data = json.loads((user_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["editor.fontSize"] == 14
    assert data["files.autoSave"] == "onFocusChange"
    assert COPILOT_SETTINGS_KEY not in data


def test_uninstall_copilot_user_level_keeps_settings_file_when_empty(
    tmp_home, force_darwin
):
    """settings.json is NOT deleted even when our entry was the only key.

    Reason: the user-level VS Code settings file may be expected to
    exist by other tooling; deleting it is too presumptuous.
    """
    install_copilot_user_level()
    user_dir = _darwin_user_dir(tmp_home)
    settings_path = user_dir / "settings.json"
    assert settings_path.exists()

    uninstall_copilot_user_level()

    assert settings_path.exists()
    # And the file is valid JSON: just {} or close to it.
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_uninstall_copilot_user_level_idempotent(tmp_home, force_darwin):
    install_copilot_user_level()
    assert uninstall_copilot_user_level() is True
    assert uninstall_copilot_user_level() is False
    assert uninstall_copilot_user_level() is False


def test_uninstall_copilot_user_level_aborts_on_unparseable_settings(
    tmp_home, force_darwin
):
    """Unparseable user settings.json -> InstallCoachError + settings untouched."""
    user_dir = _darwin_user_dir(tmp_home)
    user_dir.mkdir(parents=True)
    bad_bytes = b"{not valid json,,"
    (user_dir / "settings.json").write_bytes(bad_bytes)
    # Need to have *something* to uninstall, otherwise we short-circuit.
    # Create the prompt file so the function proceeds to read settings.json.
    (user_dir / "prompts").mkdir(parents=True)
    (user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME).write_text(
        "praxis prompt", encoding="utf-8"
    )

    with pytest.raises(InstallCoachError) as exc_info:
        uninstall_copilot_user_level()

    msg = str(exc_info.value)
    assert "not valid JSON" in msg
    assert "praxis uninstall-coach" in msg
    # File is untouched on disk.
    assert (user_dir / "settings.json").read_bytes() == bad_bytes


def test_uninstall_copilot_user_level_home_override(tmp_path, force_darwin):
    other = tmp_path / "alt-home"
    other.mkdir()
    install_copilot_user_level(home=other)
    assert uninstall_copilot_user_level(home=other) is True


# ---------------------------------------------------------------------------
# detect_managed_* helpers (drive default uninstall flow).
# ---------------------------------------------------------------------------


def test_detect_managed_claude_code_false_when_settings_absent(tmp_home):
    assert detect_managed_claude_code() is False


def test_detect_managed_claude_code_false_when_only_user_blocks(tmp_home):
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"matcher": "*", "hooks": []}]}}
        ),
        encoding="utf-8",
    )
    assert detect_managed_claude_code() is False


def test_detect_managed_claude_code_true_after_install(tmp_home):
    install_claude_code()
    assert detect_managed_claude_code() is True


def test_detect_managed_codex_false_when_hooks_absent(tmp_home):
    assert detect_managed_codex() is False


def test_detect_managed_codex_false_when_only_user_entries(tmp_home):
    codex_dir = tmp_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": [{"event": "SessionStart", "command": "echo"}]}),
        encoding="utf-8",
    )
    assert detect_managed_codex() is False


def test_detect_managed_codex_true_after_install(tmp_home):
    install_codex()
    assert detect_managed_codex() is True


def test_detect_managed_copilot_false_when_nothing_present(
    tmp_home, tmp_cwd, force_darwin
):
    assert detect_managed_copilot() is False


def test_detect_managed_copilot_true_when_workspace_block_present(
    tmp_home, tmp_cwd, force_darwin
):
    install_copilot_workspace()
    assert detect_managed_copilot() is True


def test_detect_managed_copilot_true_when_user_prompt_file_present(
    tmp_home, tmp_cwd, force_darwin
):
    install_copilot_user_level()
    assert detect_managed_copilot() is True


def test_detect_managed_tools_returns_canonical_order(
    tmp_home, tmp_cwd, force_darwin
):
    install_codex()
    install_claude_code()
    install_copilot_workspace()
    assert detect_managed_tools() == [
        TOOL_CLAUDE_CODE,
        TOOL_CODEX,
        TOOL_COPILOT,
    ]


def test_detect_managed_tools_empty_on_clean_home(
    tmp_home, tmp_cwd, force_darwin
):
    assert detect_managed_tools() == []


# ---------------------------------------------------------------------------
# Deep-equal symmetry: install -> uninstall -> install reproduces the bytes
# of a fresh install on a clean machine.
# ---------------------------------------------------------------------------


def test_symmetry_claude_install_uninstall_install(tmp_home):
    """install -> uninstall -> install produces deep-equal config (Claude Code)."""
    install_claude_code()
    settings = tmp_home / ".claude" / "settings.json"
    fresh_bytes = settings.read_bytes()
    uninstall_claude_code()
    # After uninstall the file is gone (clean-machine-equivalent state).
    assert not settings.exists()
    install_claude_code()
    # Bytes match the original fresh install exactly.
    assert settings.read_bytes() == fresh_bytes


def test_symmetry_codex_install_uninstall_install(tmp_home):
    install_codex()
    hooks = tmp_home / ".codex" / "hooks.json"
    fresh_bytes = hooks.read_bytes()
    uninstall_codex()
    assert not hooks.exists()
    install_codex()
    assert hooks.read_bytes() == fresh_bytes


def test_symmetry_copilot_workspace_install_uninstall_install(tmp_path):
    install_copilot_workspace(cwd=tmp_path)
    target = tmp_path / ".github" / "copilot-instructions.md"
    fresh_bytes = target.read_bytes()
    uninstall_copilot_workspace(cwd=tmp_path)
    assert not target.exists()
    install_copilot_workspace(cwd=tmp_path)
    assert target.read_bytes() == fresh_bytes


def test_symmetry_copilot_user_level_install_uninstall_install(
    tmp_home, force_darwin
):
    install_copilot_user_level()
    user_dir = _darwin_user_dir(tmp_home)
    prompt_path = user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME
    settings_path = user_dir / "settings.json"
    fresh_prompt = prompt_path.read_bytes()
    fresh_settings = json.loads(settings_path.read_text(encoding="utf-8"))

    uninstall_copilot_user_level()
    assert not prompt_path.exists()
    # settings.json kept (per the "user-level VS Code settings file" rule).
    assert settings_path.exists()

    install_copilot_user_level()
    # The prompt file matches the fresh install bytes.
    assert prompt_path.read_bytes() == fresh_prompt
    # The settings dict is deep-equal to the fresh install.
    assert json.loads(settings_path.read_text(encoding="utf-8")) == fresh_settings


def test_symmetry_claude_preserves_user_content_across_round_trip(tmp_home):
    """User content survives install -> uninstall -> install unchanged."""
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    user_subagent = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "echo user-subagent"}],
    }
    user_state = {
        "theme": "dark",
        "hooks": {"SubagentStop": [user_subagent]},
    }
    settings.write_text(json.dumps(user_state), encoding="utf-8")

    install_claude_code()
    uninstall_claude_code()
    # User content remains intact post-uninstall.
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["hooks"]["SubagentStop"] == [user_subagent]


# ---------------------------------------------------------------------------
# CLI integration: ``praxis uninstall-coach``.
# ---------------------------------------------------------------------------


def test_cli_uninstall_coach_nothing_to_uninstall_when_never_installed(
    tmp_home, tmp_cwd, capsys, force_darwin, no_copilot
):
    """Clean machine: 'Nothing to uninstall.' printed, exit 0, no file writes."""
    code = main(["uninstall-coach"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Nothing to uninstall." in out
    # No files were created as a side effect.
    assert not (tmp_home / ".claude" / "settings.json").exists()
    assert not (tmp_home / ".codex" / "hooks.json").exists()
    assert not (tmp_cwd / ".github" / "copilot-instructions.md").exists()


def test_cli_uninstall_coach_removes_claude_with_tool_flag(
    tmp_home, capsys, no_copilot
):
    install_claude_code()
    assert (tmp_home / ".claude" / "settings.json").exists()

    code = main(["uninstall-coach", "--tool", "claude-code", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Removed Claude Code coaching hook." in out
    assert not (tmp_home / ".claude" / "settings.json").exists()


def test_cli_uninstall_coach_removes_codex_with_tool_flag(
    tmp_home, capsys, no_copilot
):
    install_codex()
    assert (tmp_home / ".codex" / "hooks.json").exists()

    code = main(["uninstall-coach", "--tool", "codex", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Removed Codex coaching hook." in out
    assert not (tmp_home / ".codex" / "hooks.json").exists()


def test_cli_uninstall_coach_removes_copilot_with_tool_flag(
    tmp_home, tmp_cwd, capsys, force_darwin, no_copilot
):
    install_copilot_workspace()
    install_copilot_user_level()

    code = main(["uninstall-coach", "--tool", "copilot", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    # Both surfaces report removal.
    assert "Removed Copilot workspace surface." in out
    assert "Removed Copilot user-level surface." in out
    # Files are gone.
    assert not (tmp_cwd / ".github" / "copilot-instructions.md").exists()
    user_dir = _darwin_user_dir(tmp_home)
    assert not (user_dir / "prompts" / COPILOT_INSTRUCTION_FILENAME).exists()


def test_cli_uninstall_coach_default_flow_only_prompts_managed_tools(
    tmp_home, capsys, no_copilot
):
    """Default flow only prompts for tools that actually have Praxis content."""
    install_claude_code()
    # Codex was never installed; default flow should not prompt for it.

    code = main(["uninstall-coach", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Removed Claude Code coaching hook." in out
    # Codex confirmation message should NOT appear since it wasn't detected.
    assert "Removed Codex coaching hook." not in out


def test_cli_uninstall_coach_prompts_per_tool(
    tmp_home, monkeypatch, capsys, no_copilot
):
    """Without --yes, the per-tool prompt is shown for each managed tool."""
    install_claude_code()
    seen: list[str] = []

    def _input(prompt):
        seen.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", _input)
    code = main(["uninstall-coach"])
    capsys.readouterr()
    assert code == 0
    assert len(seen) == 1
    assert "Found Praxis coaching hook in Claude Code." in seen[0]
    assert "Remove? [Y/n]:" in seen[0]


def test_cli_uninstall_coach_prompt_default_yes(
    tmp_home, monkeypatch, capsys, no_copilot
):
    """Empty input at the uninstall prompt is treated as YES (default-Y)."""
    install_claude_code()
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    code = main(["uninstall-coach"])
    capsys.readouterr()
    assert code == 0
    assert not (tmp_home / ".claude" / "settings.json").exists()


def test_cli_uninstall_coach_explicit_no_skips_removal(
    tmp_home, monkeypatch, capsys, no_copilot
):
    install_claude_code()
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code = main(["uninstall-coach"])
    out = capsys.readouterr().out
    assert code == 0
    # The praxis content is still there because the user declined removal.
    assert (tmp_home / ".claude" / "settings.json").exists()
    # And since no removal occurred, "Nothing to uninstall." is printed.
    assert "Nothing to uninstall." in out


def test_cli_uninstall_coach_eof_treated_as_no(
    tmp_home, monkeypatch, capsys, no_copilot
):
    install_claude_code()

    def _eof(_prompt):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _eof)
    code = main(["uninstall-coach"])
    capsys.readouterr()
    assert code == 0
    # User implicitly declined; file should still be there.
    assert (tmp_home / ".claude" / "settings.json").exists()


def test_cli_uninstall_coach_yes_flag_skips_prompts(
    tmp_home, monkeypatch, capsys, no_copilot
):
    install_claude_code()
    install_codex()

    def _input(_prompt):
        raise AssertionError("--yes should skip prompts")

    monkeypatch.setattr("builtins.input", _input)
    code = main(["uninstall-coach", "--yes"])
    capsys.readouterr()
    assert code == 0
    assert not (tmp_home / ".claude" / "settings.json").exists()
    assert not (tmp_home / ".codex" / "hooks.json").exists()


def test_cli_uninstall_coach_all_flag_iterates_all_tools(
    tmp_home, capsys, no_copilot
):
    """--all attempts uninstall on every tool, even those not detected."""
    install_codex()  # only Codex; Claude and Copilot are absent.

    code = main(["uninstall-coach", "--all", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Removed Codex coaching hook." in out
    # Claude and Copilot were no-ops; they are not in the "Removed" output.
    assert "Removed Claude Code coaching hook." not in out
    assert "Removed Copilot" not in out


def test_cli_uninstall_coach_tool_rejects_unknown_tool(tmp_home, capsys):
    code = main(["uninstall-coach", "--tool", "bogus", "--yes"])
    err = capsys.readouterr().err
    assert code == 1
    assert "Unknown --tool 'bogus'" in err
    assert "claude-code" in err
    assert "codex" in err
    assert "copilot" in err


def test_cli_uninstall_coach_all_and_tool_mutually_exclusive(tmp_home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["uninstall-coach", "--all", "--tool", "codex"])
    assert exc_info.value.code != 0


def test_cli_uninstall_coach_unparseable_claude_prints_error_continues(
    tmp_home, capsys, no_copilot
):
    """Unparseable settings.json -> stderr message, CLI still exits 0.

    A bad config for one tool must not block uninstalling another.
    """
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("not-json", encoding="utf-8")

    code = main(["uninstall-coach", "--tool", "claude-code", "--yes"])
    captured = capsys.readouterr()
    assert code == 0
    assert "not valid JSON" in captured.err
    # File NOT overwritten.
    assert settings.read_text(encoding="utf-8") == "not-json"


def test_cli_uninstall_coach_nothing_to_uninstall_when_user_declines(
    tmp_home, monkeypatch, capsys, no_copilot
):
    """If the user declines all prompts and nothing was removed, 'Nothing to uninstall.' is printed."""
    install_claude_code()
    install_codex()

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code = main(["uninstall-coach"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Nothing to uninstall." in out
    # Files still present.
    assert (tmp_home / ".claude" / "settings.json").exists()
    assert (tmp_home / ".codex" / "hooks.json").exists()


# ---------------------------------------------------------------------------
# Programmatic API (run_uninstall_coach).
# ---------------------------------------------------------------------------


def test_run_uninstall_coach_assume_yes_iterates_detected(
    tmp_home, capsys, no_copilot
):
    install_codex()
    code = run_uninstall_coach(assume_yes=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "Removed Codex coaching hook." in out
    assert not (tmp_home / ".codex" / "hooks.json").exists()


def test_run_uninstall_coach_no_detection_returns_zero(
    tmp_home, tmp_cwd, capsys, force_darwin, no_copilot
):
    code = run_uninstall_coach()
    out = capsys.readouterr().out
    assert code == 0
    assert "Nothing to uninstall." in out
