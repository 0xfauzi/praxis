"""Tests for ``praxis install-coach`` (US-028).

Covers tool detection (Claude Code, Codex, Copilot) and the per-tool
prompt + flag (``--yes``, ``--tool``, ``--all``) behavior. The actual
hook-writing branches (US-029 / 030 / 031) replace the placeholder in
``install_for_tool`` later; for now these tests assert detection and
prompt routing only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from praxis.cli.__main__ import main
from praxis.cli.install_coach import (
    ALL_TOOLS,
    TOOL_CLAUDE_CODE,
    TOOL_CODEX,
    TOOL_COPILOT,
    detect_all,
    detect_claude_code,
    detect_codex,
    detect_copilot,
    display_name,
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
