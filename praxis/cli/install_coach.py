"""``praxis install-coach`` / ``praxis uninstall-coach`` -- per-tool coaching hooks.

The coaching hooks fire on SessionStart and Stop in the supported AI
coding tools (Claude Code, Codex, Copilot) so Praxis can surface the
weekly commitment at the start of work and capture a reflection at the
end. US-028 wired detection + per-tool yes/no prompting; US-029 fills
in the Claude Code branch with a real settings.json merger (atomic
write, sentinel-tagged blocks); US-030 fills in the Codex branch with
a parallel installer that writes ``~/.codex/hooks.json``; US-031 fills
in the Copilot branch with a workspace-level markdown injector and an
optional user-level prompt-file + settings.json patcher; US-032
provides the symmetric ``uninstall-coach`` command that removes ONLY
the Praxis-authored blocks/entries (sentinel for JSON; markers for
markdown) so user content at the same surfaces is preserved.

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

Copilot installer (US-031):
  - Workspace surface (per-repo, default Y): manages a marker-bounded
    block ``<!-- praxisManaged:begin --> ... <!-- praxisManaged:end -->``
    inside ``<cwd>/.github/copilot-instructions.md``. Creates the file
    if absent; replaces an existing managed block in place, preserving
    surrounding markdown byte-for-byte. Re-runs are idempotent. The
    workspace prompt is asked separately from the top-level
    "Found Copilot. Install...?" prompt because committing a managed
    block to a shared repo is opt-in per-repo.
  - User-level surface (optional, default N): writes
    ``<vscode-user-dir>/prompts/praxis-commitment.instructions.md`` and
    patches user-level ``settings.json`` so VS Code auto-loads the file
    via ``chat.instructionsFilesLocations``. This is gated behind a
    second explicit prompt because it modifies user-level VS Code
    settings; ``--yes`` does NOT opt into this surface (the AC says the
    user must explicitly confirm). If the user declines, neither path
    is touched.
  - Unparseable user-level ``settings.json`` and non-object top level
    abort with :class:`InstallCoachError`; the file is never
    overwritten.
"""
from __future__ import annotations

import json
import os
import platform
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


COPILOT_MARKER_BEGIN = "<!-- praxisManaged:begin -->"
COPILOT_MARKER_END = "<!-- praxisManaged:end -->"
COPILOT_INSTRUCTION_FILENAME = "praxis-commitment.instructions.md"
COPILOT_SETTINGS_KEY = "chat.instructionsFilesLocations"


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


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic write helper for text files.

    Tempfile in the same dir (so ``os.replace`` is atomic on POSIX),
    cleans up the tempfile on failure. Used by both the JSON installers
    (via :func:`_atomic_write_json`) and the Copilot markdown injector.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: object) -> None:
    """Validate + atomic-write JSON to ``path``.

    Validation is the ``json.dumps`` call itself: if ``data`` is not
    JSON-serializable it raises ``TypeError`` before any tempfile is
    written. The atomic dance is the standard tempfile + ``os.replace``
    on the same filesystem so a crash mid-write leaves the original
    file intact.
    """
    encoded = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(path, encoded)


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


def copilot_workspace_path(cwd: Path | None = None) -> Path:
    """Return the canonical workspace-level Copilot instructions path.

    Defaults to ``<cwd>/.github/copilot-instructions.md``; tests pass an
    explicit ``cwd`` to scope writes to ``tmp_path`` without relying on
    ``monkeypatch.chdir``. The path is VS Code's auto-loaded location
    (per spec section "Copilot"), so files written here are picked up by
    Copilot Chat with zero settings changes.
    """
    base = cwd if cwd is not None else Path.cwd()
    return base / ".github" / "copilot-instructions.md"


def vscode_user_dir(home: Path | None = None) -> Path:
    """Return the platform-specific VS Code ``User`` directory.

      - macOS:   ``~/Library/Application Support/Code/User``
      - Linux:   ``~/.config/Code/User``
      - Windows: ``%APPDATA%/Code/User``  (falls back to
        ``~/AppData/Roaming/Code/User`` when ``APPDATA`` is unset)

    Other platforms fall through to the Linux layout. We deliberately
    pick the plain "Code" variant (not Insiders / VSCodium / Cursor): a
    user opting into the user-level surface most commonly means the
    primary VS Code install. Forks/Insiders users can copy the prompt
    file manually if needed.
    """
    base = home if home is not None else Path.home()
    system = platform.system()
    if system == "Darwin":
        return base / "Library" / "Application Support" / "Code" / "User"
    if system == "Windows":
        appdata_env = os.environ.get("APPDATA")
        if appdata_env:
            return Path(appdata_env) / "Code" / "User"
        return base / "AppData" / "Roaming" / "Code" / "User"
    return base / ".config" / "Code" / "User"


def _build_copilot_block_content() -> str:
    """Return the Praxis-managed Copilot block (markers included).

    At install time we write a placeholder pointing the user at
    ``praxis commit``. The block is rewritten by ``praxis commit`` when
    the active commitment changes (future story); the install layer only
    establishes the managed surface. The exact bytes are fixed so
    re-running ``install-coach`` is idempotent.
    """
    return (
        f"{COPILOT_MARKER_BEGIN}\n"
        "This week's focus: (not yet set - run `praxis commit` to set "
        "this week's focus.)\n"
        "\n"
        "(Praxis - your AI usage coach. Run `praxis review` to see how "
        "it's going.)\n"
        f"{COPILOT_MARKER_END}"
    )


def _replace_or_append_copilot_block(content: str, new_block: str) -> str:
    """Replace the Praxis-managed block, or append if absent.

    Preserves byte-level content outside the markers. When no begin
    marker is found, append the new block with a blank-line separator
    (or alone on an empty file). When the begin marker is found but the
    end marker is truncated, replace from the begin marker to end of
    file so the file ends with a well-formed managed block.
    """
    begin = content.find(COPILOT_MARKER_BEGIN)
    if begin == -1:
        if not content:
            return new_block + "\n"
        sep = "\n" if content.endswith("\n") else "\n\n"
        return content + sep + new_block + "\n"

    end = content.find(COPILOT_MARKER_END, begin)
    if end == -1:
        return content[:begin] + new_block + "\n"

    end_after = end + len(COPILOT_MARKER_END)
    return content[:begin] + new_block + content[end_after:]


def install_copilot_workspace(cwd: Path | None = None) -> Path:
    """Install (or refresh) the workspace-level Copilot instructions block.

    Reads ``<cwd>/.github/copilot-instructions.md`` (or starts from an
    empty string if absent), replaces the Praxis-managed block in place
    (or appends if no block is present), then writes back atomically.
    Content outside the markers is preserved byte-for-byte.

    Returns the path of the written file.
    """
    target = copilot_workspace_path(cwd)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    new_content = _replace_or_append_copilot_block(
        existing, _build_copilot_block_content()
    )
    _atomic_write_text(target, new_content)
    return target


def install_copilot_user_level(home: Path | None = None) -> tuple[Path, Path]:
    """Install the user-level Copilot prompt file + settings.json patch.

    Writes ``<vscode-user-dir>/prompts/praxis-commitment.instructions.md``
    and adds the prompts directory to the ``chat.instructionsFilesLocations``
    dict in user-level ``settings.json``. Returns
    ``(prompt_path, settings_path)``.

    Raises :class:`InstallCoachError` when the existing user
    ``settings.json`` is unparseable JSON or its top-level value is not
    a JSON object; in either error case the file on disk is left
    untouched.
    """
    user_dir = vscode_user_dir(home)
    prompts_dir = user_dir / "prompts"
    prompt_path = prompts_dir / COPILOT_INSTRUCTION_FILENAME

    prompt_content = _build_copilot_block_content() + "\n"
    _atomic_write_text(prompt_path, prompt_content)

    settings_path = user_dir / "settings.json"
    settings_data: dict[str, Any]
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        if raw.strip() == "":
            settings_data = {}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InstallCoachError(
                    f"Cannot install Copilot user-level surface: "
                    f"{settings_path} is not valid JSON ({exc.msg} at "
                    f"line {exc.lineno} column {exc.colno}). Fix the "
                    "file manually and re-run `praxis install-coach`."
                ) from exc
            if not isinstance(parsed, dict):
                raise InstallCoachError(
                    f"Cannot install Copilot user-level surface: "
                    f"{settings_path} top level must be a JSON object, "
                    f"got {type(parsed).__name__}. Fix the file "
                    "manually and re-run `praxis install-coach`."
                )
            settings_data = parsed
    else:
        settings_data = {}

    raw_locations = settings_data.get(COPILOT_SETTINGS_KEY)
    locations: dict[str, Any]
    if isinstance(raw_locations, dict):
        locations = raw_locations
    else:
        locations = {}
    locations[str(prompts_dir)] = True
    settings_data[COPILOT_SETTINGS_KEY] = locations

    _atomic_write_json(settings_path, settings_data)
    return prompt_path, settings_path


def install_for_tool(tool: str, *, assume_yes: bool = False) -> None:
    """Per-tool installer dispatch.

    US-029 fills in the Claude Code branch with the real settings.json
    merger (sentinel + atomic write); US-030 fills in the Codex branch
    with the parallel ``~/.codex/hooks.json`` installer; US-031 fills
    in the Copilot branch with the workspace markdown injector and an
    opt-in user-level prompt-file + settings.json patcher.

    The Copilot branch reads ``assume_yes`` to know whether to skip the
    workspace prompt. The user-level surface is gated behind an
    explicit confirmation regardless of ``assume_yes``, per AC US-031
    ("behind an explicit prompt the user must confirm").

    Errors raised by a per-tool installer are caught here and printed
    to stderr; we deliberately do NOT abort the whole ``install-coach``
    run -- a bad Claude Code settings file should not block Codex, and
    a bad workspace markdown should not block the user-level prompt.
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
        workspace_target = copilot_workspace_path()
        if assume_yes or _prompt_yes_text(
            f"Write the Praxis commitment block to {workspace_target}?",
            default_yes=True,
        ):
            try:
                path = install_copilot_workspace()
            except InstallCoachError as exc:
                print(str(exc), file=sys.stderr)
            else:
                print(f"Installed {label} workspace surface: {path}")
        # User-level surface is gated behind explicit confirmation.
        # --yes does NOT opt into it: the AC says the user must confirm.
        if assume_yes:
            return
        prompts_file = (
            vscode_user_dir() / "prompts" / COPILOT_INSTRUCTION_FILENAME
        )
        if _prompt_yes_text(
            (
                f"Also write a user-level prompt file at {prompts_file} "
                "and update VS Code settings.json?"
            ),
            default_yes=False,
        ):
            try:
                prompt_path, settings_path = install_copilot_user_level()
            except InstallCoachError as exc:
                print(str(exc), file=sys.stderr)
            else:
                print(f"Installed {label} user-level prompt: {prompt_path}")
                print(f"Updated VS Code user settings: {settings_path}")
        return
    print(f"Installing {label} coaching hook... (not yet implemented)")


def _prompt_yes(tool_display: str) -> bool:
    """Top-level per-tool prompt (default Y, EOF -> no).

    Matches AC US-028: ``Found <Tool>. Install the Praxis coaching
    hook? [Y/n]:``. Delegates to :func:`_prompt_yes_text` so the empty/
    EOF semantics stay consistent across prompts.
    """
    return _prompt_yes_text(
        f"Found {tool_display}. Install the Praxis coaching hook?",
        default_yes=True,
    )


def _prompt_yes_text(prompt_text: str, *, default_yes: bool = True) -> bool:
    """Generalized yes/no prompt with selectable default.

    Returns ``default_yes`` for empty input; ``True`` for 'y'/'yes';
    ``False`` for anything else. ``EOFError`` (e.g. piped stdin closes
    after the first prompt) is treated as 'no' so a non-interactive
    shell silently declines rather than hanging.
    """
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    try:
        reply = input(prompt_text + suffix).strip().lower()
    except EOFError:
        return False
    if reply == "":
        return default_yes
    return reply in {"y", "yes"}


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
            install_for_tool(t, assume_yes=assume_yes)
    return 0


# ---------------------------------------------------------------------------
# US-032: ``praxis uninstall-coach`` -- symmetric teardown.
#
# The uninstall surfaces are deliberately parallel to the install surfaces:
# one per-tool removal function returning ``bool`` (whether anything was
# removed), a dispatcher, and a ``run_uninstall_coach`` orchestrator that
# mirrors :func:`run_install_coach`'s ``--yes`` / ``--tool`` / ``--all``
# flag semantics.
#
# The key invariants (AC US-032):
#   - Only blocks/entries carrying the :data:`SENTINEL` (Claude, Codex) or
#     the markdown markers (Copilot) are removed; user-authored content
#     at the same event/path is preserved unchanged.
#   - On a system where Praxis was never installed, the command prints
#     ``Nothing to uninstall.`` and exits 0 without any file writes.
#   - Deep-equal symmetry: install -> uninstall -> install reproduces the
#     bytes of a fresh install on a clean machine. This drives the
#     "delete the file when its content would be empty after removal"
#     branches in :func:`uninstall_claude_code` /
#     :func:`uninstall_codex` / :func:`uninstall_copilot_workspace`.
# ---------------------------------------------------------------------------


def _read_json_settings(
    path: Path, *, tool_label: str
) -> dict[str, Any] | None:
    """Read a JSON settings file or return ``None`` if absent/empty.

    Raises :class:`InstallCoachError` with a clear message when the file
    is unparseable JSON or its top-level value is not a JSON object.
    Mirrors the install-side parse/validate so uninstall never overwrites
    a malformed config silently. ``tool_label`` is used in the error
    message to point the user at the offending file.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if raw.strip() == "":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstallCoachError(
            f"Cannot uninstall {tool_label} hooks: {path} is not valid "
            f"JSON ({exc.msg} at line {exc.lineno} column {exc.colno}). "
            "Fix the file manually and re-run `praxis uninstall-coach`."
        ) from exc
    if not isinstance(parsed, dict):
        raise InstallCoachError(
            f"Cannot uninstall {tool_label} hooks: {path} top level must "
            f"be a JSON object, got {type(parsed).__name__}. Fix the "
            "file manually and re-run `praxis uninstall-coach`."
        )
    return parsed


def uninstall_claude_code(home: Path | None = None) -> bool:
    """Remove Praxis-managed Claude Code hooks from settings.json.

    Walks the ``hooks`` dict, dropping any block whose top level carries
    ``_praxisManaged: true``. Empty event lists are pruned; an empty
    ``hooks`` dict is removed entirely; if the resulting top-level dict
    is empty (i.e. Praxis was the only thing in the file), the file is
    deleted so a subsequent ``install-coach`` reproduces the bytes of a
    fresh-on-clean-machine install (deep-equal symmetry).
    Returns True iff anything was removed.
    """
    settings_path = claude_settings_path(home)
    data = _read_json_settings(settings_path, tool_label="Claude Code")
    if data is None:
        return False

    raw_hooks = data.get("hooks")
    if not isinstance(raw_hooks, dict):
        return False

    removed = False
    new_hooks: dict[str, Any] = {}
    for event, entries in raw_hooks.items():
        if not isinstance(entries, list):
            new_hooks[event] = entries
            continue
        kept = [
            entry
            for entry in entries
            if not (isinstance(entry, dict) and entry.get(SENTINEL) is True)
        ]
        if len(kept) != len(entries):
            removed = True
        if kept:
            new_hooks[event] = kept

    if not removed:
        return False

    if new_hooks:
        data["hooks"] = new_hooks
    else:
        data.pop("hooks", None)

    if not data:
        try:
            settings_path.unlink()
        except FileNotFoundError:
            pass
        return True

    _atomic_write_json(settings_path, data)
    return True


def uninstall_codex(home: Path | None = None) -> bool:
    """Remove Praxis-managed Codex hooks from hooks.json.

    Filters the ``hooks`` list, dropping entries whose top level carries
    ``_praxisManaged: true``. If the resulting list is empty, the
    ``hooks`` key is removed; if the resulting top-level dict is empty,
    the file is deleted (deep-equal symmetry for fresh-on-clean install).
    Returns True iff anything was removed.
    """
    hooks_path = codex_hooks_path(home)
    data = _read_json_settings(hooks_path, tool_label="Codex")
    if data is None:
        return False

    raw_hooks = data.get("hooks")
    if not isinstance(raw_hooks, list):
        return False

    kept = [
        entry
        for entry in raw_hooks
        if not (isinstance(entry, dict) and entry.get(SENTINEL) is True)
    ]
    if len(kept) == len(raw_hooks):
        return False

    if kept:
        data["hooks"] = kept
    else:
        data.pop("hooks", None)

    if not data:
        try:
            hooks_path.unlink()
        except FileNotFoundError:
            pass
        return True

    _atomic_write_json(hooks_path, data)
    return True


def _remove_copilot_block(content: str) -> tuple[str, bool]:
    """Strip the markers-bounded Praxis block, preserving surrounding bytes.

    Returns ``(new_content, removed)``. If no markers are present the
    content is returned unchanged with ``removed=False``. If the block
    is found, the preceding blank-line separator (``\\n\\n``) is also
    trimmed so the file does not retain a stray gap where the block used
    to live; if the block is at the start of the file, a trailing
    newline directly after the end marker is consumed instead so the
    file still ends cleanly.
    """
    begin = content.find(COPILOT_MARKER_BEGIN)
    if begin == -1:
        return content, False
    end = content.find(COPILOT_MARKER_END, begin)
    if end == -1:
        # Truncated block (no end marker). Drop from begin to EOF -- the
        # alternative (leaving the dangling begin marker) is worse because
        # the file would still "look praxis-managed" to detect_managed().
        new_content = content[:begin]
        # Trim a single leading separator if present.
        if new_content.endswith("\n\n"):
            new_content = new_content[:-1]
        return new_content, True

    after = end + len(COPILOT_MARKER_END)
    leading = content[:begin]
    trailing = content[after:]

    if leading.endswith("\n\n"):
        # User content + blank line + our block; drop one of the newlines
        # so the surrounding content keeps its trailing single newline.
        leading = leading[:-1]
    elif not leading and trailing.startswith("\n"):
        # Block at start of file with a trailing newline directly after
        # the end marker -- consume that newline so we don't end up with
        # a stray leading newline.
        trailing = trailing[1:]

    return leading + trailing, True


def uninstall_copilot_workspace(cwd: Path | None = None) -> bool:
    """Remove the workspace-level Copilot block from copilot-instructions.md.

    If the file does not exist or has no markers, returns False without
    writes. If the markers are found, removes the block (preserving
    surrounding bytes per :func:`_remove_copilot_block`). If the file
    becomes empty (or only whitespace), it is deleted so a subsequent
    ``install-coach`` produces deep-equal bytes to a clean-machine
    install. Returns True iff anything was removed.
    """
    target = copilot_workspace_path(cwd)
    if not target.exists():
        return False
    existing = target.read_text(encoding="utf-8")
    new_content, removed = _remove_copilot_block(existing)
    if not removed:
        return False
    if new_content.strip() == "":
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return True
    _atomic_write_text(target, new_content)
    return True


def uninstall_copilot_user_level(home: Path | None = None) -> bool:
    """Remove the user-level Copilot prompt file + settings.json entry.

    Deletes ``<vscode-user-dir>/prompts/praxis-commitment.instructions.md``
    if present, and removes the prompts-dir entry from
    ``chat.instructionsFilesLocations`` in user-level ``settings.json``.
    If the locations dict becomes empty, the whole key is removed. The
    settings.json file itself is NOT deleted even if it becomes
    ``{}`` -- a user-level VS Code settings file may be expected to
    exist by other tooling, and deleting it is too presumptuous.
    Returns True iff anything was removed (prompt file or settings
    entry).

    Raises :class:`InstallCoachError` when the existing settings.json is
    unparseable JSON or non-object top-level (mirrors the install-side
    behavior so uninstall never overwrites a malformed config).
    """
    user_dir = vscode_user_dir(home)
    prompts_dir = user_dir / "prompts"
    prompt_path = prompts_dir / COPILOT_INSTRUCTION_FILENAME

    removed = False
    if prompt_path.exists():
        try:
            prompt_path.unlink()
            removed = True
        except FileNotFoundError:
            pass

    settings_path = user_dir / "settings.json"
    settings_data = _read_json_settings(
        settings_path, tool_label="Copilot user-level"
    )
    if settings_data is None:
        return removed

    locations = settings_data.get(COPILOT_SETTINGS_KEY)
    if not isinstance(locations, dict):
        return removed

    prompts_key = str(prompts_dir)
    if prompts_key not in locations:
        return removed

    locations.pop(prompts_key, None)
    removed = True
    if locations:
        settings_data[COPILOT_SETTINGS_KEY] = locations
    else:
        settings_data.pop(COPILOT_SETTINGS_KEY, None)

    _atomic_write_json(settings_path, settings_data)
    return True


def detect_managed_claude_code(home: Path | None = None) -> bool:
    """Return True when settings.json carries any sentinel-tagged block.

    Used by :func:`detect_managed_tools` so the default uninstall flow
    only prompts for tools that actually have Praxis content. Unparseable
    settings.json returns False (not an error) -- detection is a probe,
    not a parse-or-die operation; the real install/uninstall path will
    raise a clear error when the user opts in.
    """
    settings_path = claude_settings_path(home)
    if not settings_path.exists():
        return False
    raw = settings_path.read_text(encoding="utf-8")
    if raw.strip() == "":
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get(SENTINEL) is True:
                return True
    return False


def detect_managed_codex(home: Path | None = None) -> bool:
    """Return True when hooks.json carries any sentinel-tagged entry."""
    hooks_path = codex_hooks_path(home)
    if not hooks_path.exists():
        return False
    raw = hooks_path.read_text(encoding="utf-8")
    if raw.strip() == "":
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, list):
        return False
    for entry in hooks:
        if isinstance(entry, dict) and entry.get(SENTINEL) is True:
            return True
    return False


def detect_managed_copilot(
    home: Path | None = None, cwd: Path | None = None
) -> bool:
    """Return True when any Praxis-managed Copilot artifact is present.

    Checks (a) the workspace copilot-instructions.md for the begin/end
    markers, (b) the user-level prompt file's existence, and (c) the
    user-level settings.json for our entry in
    ``chat.instructionsFilesLocations``. Any of the three makes the tool
    a candidate for uninstall.
    """
    workspace = copilot_workspace_path(cwd)
    if workspace.exists():
        content = workspace.read_text(encoding="utf-8")
        if COPILOT_MARKER_BEGIN in content and COPILOT_MARKER_END in content:
            return True

    user_dir = vscode_user_dir(home)
    prompts_dir = user_dir / "prompts"
    prompt_path = prompts_dir / COPILOT_INSTRUCTION_FILENAME
    if prompt_path.exists():
        return True

    settings_path = user_dir / "settings.json"
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        if raw.strip() != "":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return False
            if isinstance(data, dict):
                locations = data.get(COPILOT_SETTINGS_KEY)
                if isinstance(locations, dict) and str(prompts_dir) in locations:
                    return True
    return False


def detect_managed_tools(
    home: Path | None = None, cwd: Path | None = None
) -> list[str]:
    """Return tools that have any Praxis-managed content, in canonical order."""
    found: list[str] = []
    if detect_managed_claude_code(home):
        found.append(TOOL_CLAUDE_CODE)
    if detect_managed_codex(home):
        found.append(TOOL_CODEX)
    if detect_managed_copilot(home=home, cwd=cwd):
        found.append(TOOL_COPILOT)
    return found


def uninstall_for_tool(tool: str) -> bool:
    """Per-tool uninstall dispatch.

    Returns True iff anything was actually removed. Errors raised by the
    per-tool removers are caught and printed to stderr; a bad Claude
    Code config does not block a Codex uninstall. This mirrors
    :func:`install_for_tool`'s tolerance of per-tool failures.
    """
    label = display_name(tool)
    if tool == TOOL_CLAUDE_CODE:
        try:
            removed = uninstall_claude_code()
        except InstallCoachError as exc:
            print(str(exc), file=sys.stderr)
            return False
        if removed:
            print(f"Removed {label} coaching hook.")
        return removed
    if tool == TOOL_CODEX:
        try:
            removed = uninstall_codex()
        except InstallCoachError as exc:
            print(str(exc), file=sys.stderr)
            return False
        if removed:
            print(f"Removed {label} coaching hook.")
        return removed
    if tool == TOOL_COPILOT:
        workspace_removed = uninstall_copilot_workspace()
        if workspace_removed:
            print(f"Removed {label} workspace surface.")
        try:
            user_removed = uninstall_copilot_user_level()
        except InstallCoachError as exc:
            print(str(exc), file=sys.stderr)
            user_removed = False
        if user_removed:
            print(f"Removed {label} user-level surface.")
        return workspace_removed or user_removed
    return False


def _prompt_uninstall_yes(tool_display: str) -> bool:
    """Per-tool uninstall prompt (default Y, EOF -> no).

    Parallels :func:`_prompt_yes` so the surface wording stays
    consistent across install + uninstall.
    """
    return _prompt_yes_text(
        f"Found Praxis coaching hook in {tool_display}. Remove?",
        default_yes=True,
    )


def run_uninstall_coach(
    *,
    assume_yes: bool = False,
    tool: str | None = None,
    all_tools: bool = False,
    home: Path | None = None,
) -> int:
    """Top-level orchestrator for ``praxis uninstall-coach``.

    Mirrors :func:`run_install_coach`'s flag semantics:
      - default flow: detect tools with Praxis-managed content, prompt
        per tool, remove on yes;
      - ``--yes``: skip per-tool prompts;
      - ``--tool NAME``: restrict to one tool;
      - ``--all``: iterate every known tool regardless of detection.

    The ``Nothing to uninstall.`` message is printed when NO tool had
    anything to remove (either detected nothing, or all calls returned
    False). In every case the exit code is 0; the ``--tool NAME``
    validation error (unknown slug) is the only exit-1 path.
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
        targets = detect_managed_tools(home=home)
        if not targets:
            print("Nothing to uninstall.")
            return 0

    any_removed = False
    for t in targets:
        if assume_yes or _prompt_uninstall_yes(display_name(t)):
            if uninstall_for_tool(t):
                any_removed = True

    if not any_removed:
        print("Nothing to uninstall.")
    return 0
