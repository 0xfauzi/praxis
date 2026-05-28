"""``praxis install-coach`` -- per-tool coaching hook installer.

The coaching hooks fire on SessionStart and Stop in the supported AI
coding tools (Claude Code, Codex, Copilot) so Praxis can surface the
weekly commitment at the start of work and capture a reflection at the
end. The installer's first responsibility -- the only one US-028
covers -- is tool detection and per-tool yes/no prompting. The actual
hook-writing branches land in US-029 (Claude Code), US-030 (Codex), and
US-031 (Copilot); each story replaces the matching placeholder branch
in :func:`install_for_tool`.

Detection criteria (AC US-028):
  - Claude Code: ``~/.claude/settings.json`` OR ``~/.claude/projects/``
    is present on disk.
  - Codex: ``~/.codex/`` is present.
  - Copilot: any VS Code workspace storage directory contains Copilot
    chat artifacts -- either ``chatSessions/*.json`` (newer) or
    ``state.vscdb`` (older). This reuses the existing scanner discovery
    by importing :func:`praxis.scanners.copilot._vscode_user_paths`.

Flag semantics:
  - ``--yes``       -- assume yes for every prompt (no input).
  - ``--tool NAME`` -- restrict to a single tool; still prompts unless
                       ``--yes`` is also set. Mutually exclusive with
                       ``--all``.
  - ``--all``       -- iterate every known tool regardless of detection.

Returns CLI exit code 0 for the happy path (including the no-tools-
detected branch); exit code 1 for an invalid ``--tool`` argument.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path


TOOL_CLAUDE_CODE = "claude-code"
TOOL_CODEX = "codex"
TOOL_COPILOT = "copilot"


ALL_TOOLS: tuple[str, ...] = (TOOL_CLAUDE_CODE, TOOL_CODEX, TOOL_COPILOT)


_DISPLAY_NAMES: dict[str, str] = {
    TOOL_CLAUDE_CODE: "Claude Code",
    TOOL_CODEX: "Codex",
    TOOL_COPILOT: "Copilot",
}


def display_name(tool: str) -> str:
    """Human-facing label used in the 'Found <Tool>...' prompt."""
    return _DISPLAY_NAMES.get(tool, tool)


def detect_claude_code(home: Path | None = None) -> bool:
    """Return True when Claude Code state exists under ``home``.

    Either ``~/.claude/settings.json`` (the Claude Code user-config
    file) or ``~/.claude/projects/`` (per-project session data) is
    sufficient. The ``home`` override is for tests; production callers
    leave it as ``None`` so :func:`Path.home` is honored under
    the ``tmp_home`` fixture too.
    """
    base = home if home is not None else Path.home()
    settings = base / ".claude" / "settings.json"
    projects = base / ".claude" / "projects"
    return settings.exists() or projects.exists()


def detect_codex(home: Path | None = None) -> bool:
    """Return True when ``~/.codex/`` exists under ``home``.

    The Codex CLI writes its config and session rollouts under
    ``~/.codex/``; the directory itself being present is a sufficient
    detection signal because no other tool uses that path.
    """
    base = home if home is not None else Path.home()
    return (base / ".codex").exists()


def _has_copilot_artifacts(user_dirs: Iterable[Path]) -> bool:
    """Return True when any workspace under ``user_dirs`` has Copilot chat data.

    Matches :class:`praxis.scanners.copilot.CopilotScanner.discover`:
    a workspace counts when it has either a ``chatSessions`` dir or a
    ``state.vscdb`` SQLite file.
    """
    for user_dir in user_dirs:
        ws_storage = user_dir / "workspaceStorage"
        if not ws_storage.exists():
            continue
        for workspace in ws_storage.iterdir():
            if not workspace.is_dir():
                continue
            if (workspace / "chatSessions").exists():
                return True
            if (workspace / "state.vscdb").exists():
                return True
    return False


def detect_copilot(vscode_user_dirs: Iterable[Path] | None = None) -> bool:
    """Return True when VS Code workspace storage has Copilot chat artifacts.

    Reuses the existing scanner's user-dir discovery via
    :func:`praxis.scanners.copilot._vscode_user_paths` when
    ``vscode_user_dirs`` is None, so the install path agrees with what
    the scanner would find. Tests pass ``vscode_user_dirs=[...]``
    explicitly to scope detection to a tmp path without monkeypatching
    ``platform.system``.
    """
    if vscode_user_dirs is None:
        from praxis.scanners.copilot import _vscode_user_paths

        vscode_user_dirs = _vscode_user_paths()
    return _has_copilot_artifacts(vscode_user_dirs)


def detect_all(home: Path | None = None) -> list[str]:
    """Return detected tools in :data:`ALL_TOOLS` order.

    The ``home`` override propagates to the file-system detectors
    (Claude, Codex). The Copilot detector resolves its own paths via
    the scanner's helper because the VS Code paths are platform-
    dependent and not rooted at the user's home on Windows.
    """
    found: list[str] = []
    if detect_claude_code(home):
        found.append(TOOL_CLAUDE_CODE)
    if detect_codex(home):
        found.append(TOOL_CODEX)
    if detect_copilot():
        found.append(TOOL_COPILOT)
    return found


def install_for_tool(tool: str) -> None:
    """Per-tool installer dispatch. Placeholder for US-029/030/031.

    US-028 wires detection + prompts; the actual hook writers (Claude
    Code settings.json merge, Codex hooks.json append, Copilot file
    injection) land in later stories which replace the matching branch
    in this function. The placeholder prints a one-line status so users
    who run ``install-coach`` today get acknowledgement rather than
    silence.
    """
    label = display_name(tool)
    print(f"Installing {label} coaching hook... (not yet implemented)")


def _prompt_yes(tool_display: str) -> bool:
    """Prompt with default-Y for a single tool.

    Returns True for empty input, 'y', or 'yes' (case-insensitive);
    False otherwise. ``EOFError`` (e.g. piped stdin closes after the
    first prompt) is treated as 'no' so a non-interactive shell that
    reaches this branch silently skips rather than hanging.
    """
    try:
        reply = input(
            f"Found {tool_display}. Install the Praxis coaching hook? [Y/n]: "
        ).strip().lower()
    except EOFError:
        return False
    return reply in {"", "y", "yes"}


def run_install_coach(
    *,
    assume_yes: bool = False,
    tool: str | None = None,
    all_tools: bool = False,
    home: Path | None = None,
) -> int:
    """Top-level orchestrator for ``praxis install-coach``.

    See module docstring for flag semantics. ``home`` is forwarded to
    the home-rooted detectors (Claude, Codex) so the ``tmp_home``
    fixture can drive end-to-end tests.
    """
    if tool is not None:
        key = tool.strip().lower()
        if key not in ALL_TOOLS:
            valid = ", ".join(ALL_TOOLS)
            print(
                f"Unknown --tool {tool!r}: expected one of {valid}.",
                file=sys.stderr,
            )
            return 1
        targets: list[str] = [key]
    elif all_tools:
        targets = list(ALL_TOOLS)
    else:
        targets = detect_all(home)
        if not targets:
            print("No supported AI tools detected. Pass --all to install anyway.")
            return 0

    for t in targets:
        if assume_yes or _prompt_yes(display_name(t)):
            install_for_tool(t)
    return 0
