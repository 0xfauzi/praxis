"""``praxis install-coach`` -- per-tool coaching hook installer.

The coaching hooks fire on SessionStart and Stop in the supported AI
coding tools (Claude Code, Codex, Copilot) so Praxis can surface the
weekly commitment at the start of work and capture a reflection at the
end. US-028 wired detection + per-tool yes/no prompting; US-029 fills
in the Claude Code branch with a real settings.json merger (atomic
write, sentinel-tagged blocks); US-030 fills in the Codex branch with
a parallel installer that writes ``~/.codex/hooks.json``. US-031
(Copilot) follows.

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

Claude Code installer (US-029):
  - Reads ``~/.claude/settings.json`` (or starts from ``{}``), merges a
    ``SessionStart`` block (``praxis nudge --format claude-code``) and a
    ``Stop`` block (``praxis reflect --session-end
    --non-interactive-fallback``), each tagged with
    ``_praxisManaged: true``.
  - Pre-existing user-authored blocks at the same event name are
    preserved unchanged; only Praxis-managed blocks get replaced.
  - Unparseable JSON aborts the Claude Code install with
    :class:`InstallCoachError`; the file on disk is never overwritten.
  - The merged JSON is validated via ``json.dumps`` then written
    atomically via ``tempfile.mkstemp`` + ``os.replace``.

Codex installer (US-030):
  - Reads ``~/.codex/hooks.json`` (or starts from ``{"hooks": []}``).
    The Codex shape is a flat list of ``{event, command}`` entries, not
    Claude's dict-of-event-lists -- so the merge logic is parallel but
    separate.
  - Each Praxis-managed entry carries ``_praxisManaged: true``;
    re-installing filters out the old sentinel-tagged entries and
    appends fresh ones, so re-runs never duplicate.
  - User-authored entries (no sentinel) are preserved.
  - Unparseable JSON, non-object top level, and non-array ``hooks``
    abort with :class:`InstallCoachError`; the file is never
    overwritten.
  - A ``PermissionError`` from the atomic write (unwriteable
    ``~/.codex/``) is converted to a clear :class:`InstallCoachError`
    pointing the user at the directory; no partial file is created
    because ``tempfile.mkstemp`` fails atomically before any content
    is written.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


TOOL_CLAUDE_CODE = "claude-code"
TOOL_CODEX = "codex"
TOOL_COPILOT = "copilot"


ALL_TOOLS: tuple[str, ...] = (TOOL_CLAUDE_CODE, TOOL_CODEX, TOOL_COPILOT)


_DISPLAY_NAMES: dict[str, str] = {
    TOOL_CLAUDE_CODE: "Claude Code",
    TOOL_CODEX: "Codex",
    TOOL_COPILOT: "Copilot",
}


SENTINEL = "_praxisManaged"


CLAUDE_HOOK_COMMANDS: dict[str, str] = {
    "SessionStart": "praxis nudge --format claude-code",
    "Stop": "praxis reflect --session-end --non-interactive-fallback",
}


CODEX_HOOK_COMMANDS: dict[str, str] = {
    "SessionStart": "praxis nudge --format codex",
    "Stop": "praxis reflect --session-end --non-interactive-fallback",
}


class InstallCoachError(Exception):
    """Raised when a per-tool installer cannot complete safely.

    The CLI catches this, prints ``str(exc)`` to stderr, and continues
    with the next tool (a bad Claude Code config should not block a
    Codex install). The message must be self-contained: it tells the
    user exactly which file is at fault and how to recover.
    """


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


def claude_settings_path(home: Path | None = None) -> Path:
    """Return the canonical ``~/.claude/settings.json`` path.

    The ``home`` override mirrors the detector helpers so tests can
    isolate state without monkeypatching ``Path.home``. Production
    callers leave it ``None`` and rely on the ``tmp_home`` fixture's
    ``Path.home`` patch in tests, or the real ``Path.home()`` at
    runtime.
    """
    base = home if home is not None else Path.home()
    return base / ".claude" / "settings.json"


def _build_claude_block(command: str) -> dict[str, Any]:
    """Build a single Praxis-managed Claude Code hook block.

    The block shape mirrors PLAN.md section "Claude Code": one matcher
    (``"*"``), the sentinel, and a single command-type hook entry. The
    sentinel makes uninstall (US-032) able to identify Praxis blocks
    without touching user-authored hooks at the same event name.
    """
    return {
        "matcher": "*",
        SENTINEL: True,
        "hooks": [
            {"type": "command", "command": command},
        ],
    }


def _atomic_write_json(path: Path, data: object) -> None:
    """Validate + atomic-write JSON to ``path``.

    Validation is the ``json.dumps`` call itself: if ``data`` is not
    JSON-serializable it raises ``TypeError`` before any tempfile is
    written. The atomic dance is the standard tempfile + ``os.replace``
    on the same filesystem so a crash mid-write leaves the original
    file intact.
    """
    encoded = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def install_claude_code(home: Path | None = None) -> Path:
    """Install (or refresh) the Praxis Claude Code SessionStart + Stop hooks.

    Reads ``~/.claude/settings.json`` (or starts from ``{}`` when the
    file is absent or empty), merges Praxis-managed blocks for
    ``SessionStart`` and ``Stop`` events, then writes the result back
    atomically. Pre-existing user blocks at the same event name that do
    NOT carry the :data:`SENTINEL` key are preserved unchanged; any
    existing Praxis-managed block at those event names is replaced (so
    re-running the installer is idempotent).

    Returns the path of the written settings file.

    Raises :class:`InstallCoachError` when:
      - the existing file is unparseable JSON;
      - the top-level value is not a JSON object;
      - the existing ``hooks`` key is present but not a JSON object.
    In every error case the file on disk is left untouched.
    """
    settings_path = claude_settings_path(home)

    data: dict[str, Any]
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        if raw.strip() == "":
            data = {}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InstallCoachError(
                    f"Cannot install Claude Code hooks: {settings_path} is "
                    f"not valid JSON ({exc.msg} at line {exc.lineno} "
                    f"column {exc.colno}). Fix the file manually and "
                    "re-run `praxis install-coach`."
                ) from exc
            if not isinstance(parsed, dict):
                raise InstallCoachError(
                    f"Cannot install Claude Code hooks: {settings_path} top "
                    f"level must be a JSON object, got {type(parsed).__name__}. "
                    "Fix the file manually and re-run `praxis install-coach`."
                )
            data = parsed
    else:
        data = {}

    raw_hooks = data.get("hooks")
    if raw_hooks is None:
        hooks_obj: dict[str, Any] = {}
        data["hooks"] = hooks_obj
    elif isinstance(raw_hooks, dict):
        hooks_obj = raw_hooks
    else:
        raise InstallCoachError(
            f"Cannot install Claude Code hooks: {settings_path} 'hooks' "
            f"key must be a JSON object, got {type(raw_hooks).__name__}. "
            "Fix the file manually and re-run `praxis install-coach`."
        )

    for event, command in CLAUDE_HOOK_COMMANDS.items():
        existing = hooks_obj.get(event)
        entries: list[Any]
        if isinstance(existing, list):
            entries = [
                entry
                for entry in existing
                if not (isinstance(entry, dict) and entry.get(SENTINEL) is True)
            ]
        else:
            entries = []
        entries.append(_build_claude_block(command))
        hooks_obj[event] = entries

    _atomic_write_json(settings_path, data)
    return settings_path


def codex_hooks_path(home: Path | None = None) -> Path:
    """Return the canonical ``~/.codex/hooks.json`` path.

    ``home`` override mirrors :func:`claude_settings_path` so the
    install + uninstall surfaces (US-030, US-032) and tests share one
    resolver and never drift on the path string.
    """
    base = home if home is not None else Path.home()
    return base / ".codex" / "hooks.json"


def _build_codex_entry(event: str, command: str) -> dict[str, Any]:
    """Build a single Praxis-managed Codex hook entry.

    The shape mirrors PLAN.md section "Codex CLI": a flat
    ``{event, command}`` dict carrying the sentinel so uninstall
    (US-032) can identify and remove only Praxis-authored entries
    without touching user hooks at the same event name.
    """
    return {
        "event": event,
        "command": command,
        SENTINEL: True,
    }


def install_codex(home: Path | None = None) -> Path:
    """Install (or refresh) the Praxis Codex SessionStart + Stop hooks.

    Reads ``~/.codex/hooks.json`` (or starts from ``{"hooks": []}`` when
    the file is absent or empty), drops any existing Praxis-managed
    entries (sentinel-tagged), appends fresh ``SessionStart`` and
    ``Stop`` entries, then writes the result back atomically. User-
    authored entries (no sentinel) are preserved.

    Returns the path of the written hooks file.

    Raises :class:`InstallCoachError` when:
      - the existing file is unparseable JSON;
      - the top-level value is not a JSON object;
      - the existing ``hooks`` key is present but not a JSON array;
      - the ``~/.codex/`` directory is not writable (permission denied).
    In every error case the file on disk is left untouched and no
    partial file is created (``tempfile.mkstemp`` fails atomically
    before any content is written).
    """
    hooks_path = codex_hooks_path(home)

    data: dict[str, Any]
    if hooks_path.exists():
        raw = hooks_path.read_text(encoding="utf-8")
        if raw.strip() == "":
            data = {"hooks": []}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InstallCoachError(
                    f"Cannot install Codex hooks: {hooks_path} is "
                    f"not valid JSON ({exc.msg} at line {exc.lineno} "
                    f"column {exc.colno}). Fix the file manually and "
                    "re-run `praxis install-coach`."
                ) from exc
            if not isinstance(parsed, dict):
                raise InstallCoachError(
                    f"Cannot install Codex hooks: {hooks_path} top "
                    f"level must be a JSON object, got "
                    f"{type(parsed).__name__}. Fix the file manually "
                    "and re-run `praxis install-coach`."
                )
            data = parsed
    else:
        data = {"hooks": []}

    raw_hooks = data.get("hooks")
    entries: list[Any]
    if raw_hooks is None:
        entries = []
    elif isinstance(raw_hooks, list):
        entries = raw_hooks
    else:
        raise InstallCoachError(
            f"Cannot install Codex hooks: {hooks_path} 'hooks' key "
            f"must be a JSON array, got {type(raw_hooks).__name__}. "
            "Fix the file manually and re-run `praxis install-coach`."
        )

    preserved: list[Any] = [
        entry
        for entry in entries
        if not (isinstance(entry, dict) and entry.get(SENTINEL) is True)
    ]
    for event, command in CODEX_HOOK_COMMANDS.items():
        preserved.append(_build_codex_entry(event, command))
    data["hooks"] = preserved

    try:
        _atomic_write_json(hooks_path, data)
    except PermissionError as exc:
        raise InstallCoachError(
            f"Cannot install Codex hooks: permission denied writing to "
            f"{hooks_path.parent}. Fix the directory permissions and "
            "re-run `praxis install-coach`."
        ) from exc
    return hooks_path


def install_for_tool(tool: str) -> None:
    """Per-tool installer dispatch.

    US-029 fills in the Claude Code branch with the real settings.json
    merger (sentinel + atomic write); US-030 fills in the Codex branch
    with the parallel ``~/.codex/hooks.json`` installer. US-031
    (Copilot) replaces its matching branch later. Until then the
    Copilot branch prints a placeholder so users get acknowledgement
    rather than silence.

    Errors raised by a per-tool installer are caught here and printed
    to stderr; we deliberately do NOT abort the whole ``install-coach``
    run -- a bad Claude Code settings file should not block Codex.
    """
    label = display_name(tool)
    if tool == TOOL_CLAUDE_CODE:
        try:
            path = install_claude_code()
        except InstallCoachError as exc:
            print(str(exc), file=sys.stderr)
            return
        print(f"Installed {label} coaching hook: {path}")
        return
    if tool == TOOL_CODEX:
        try:
            path = install_codex()
        except InstallCoachError as exc:
            print(str(exc), file=sys.stderr)
            return
        print(f"Installed {label} coaching hook: {path}")
        return
    if tool == TOOL_COPILOT:
        print(f"Installing {label} coaching hook... (not yet implemented)")
        return
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
