"""`praxis setup <agent> --apply` and `--remove`: the one consented write outside the
repository.

AGENTS.md invariant 8. Exactly one named file is written, after the owner has seen the
change and typed yes on a terminal. Without a terminal there is no yes, so there is no
write. The previous bytes of the file are kept under $PRAXIS_HOME so the write can be
undone by hand even when `--remove` cannot parse what a later tool did to the file.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

HOOK_TIMEOUT_S = 10
MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
# Claude Code fires PostToolUseFailure; the Codex hooks reference lists no such event,
# so the Codex block leaves it out rather than hand Codex an event name it may reject.
EVENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {
        "unmatched": ("SessionStart", "UserPromptSubmit", "Stop"),
        "matched": ("PreToolUse", "PostToolUse", "PostToolUseFailure"),
    },
    "codex": {
        "unmatched": ("SessionStart", "UserPromptSubmit", "Stop"),
        "matched": ("PreToolUse", "PostToolUse"),
    },
}
TARGETS = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json"}


@dataclass(frozen=True)
class Refusal(Exception):
    reason: str


def command() -> str:
    found = shutil.which("praxis")
    return f"{found or Path(sys.argv[0]).resolve()} hook"


def is_ours(entry: object) -> bool:
    """Our hook entries are recognised by their command, not by an extra marker key: a
    marker is a key the agent's settings validator has never seen."""
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    return any(
        isinstance(h, dict)
        and isinstance(h.get("command"), str)
        and h["command"].endswith("praxis hook")
        for h in inner
    )


def snippet(agent: str) -> dict[str, Any]:
    entry = {"type": "command", "command": command(), "timeout": HOOK_TIMEOUT_S}
    block: dict[str, Any] = {}
    for event in EVENTS[agent]["unmatched"]:
        block[event] = [{"hooks": [entry]}]
    for event in EVENTS[agent]["matched"]:
        block[event] = [{"matcher": MATCHER, "hooks": [entry]}]
    return {"hooks": block}


def target_path(agent: str, home: Path) -> Path:
    return home / TARGETS[agent]


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if raw.strip() == "":
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise Refusal(
            f"{path} is not valid JSON ({exc}); fix it by hand first"
        ) from exc
    if not isinstance(parsed, dict):
        raise Refusal(f"{path} must hold a JSON object at the top level")
    return parsed


def merged(data: dict[str, Any], agent: str) -> dict[str, Any]:
    """``data`` with our entries present once per event, everything else untouched."""
    out: dict[str, Any] = json.loads(json.dumps(data))  # a deep copy, no shared lists
    hooks = out.get("hooks")
    if hooks is None:
        hooks = out["hooks"] = {}
    if not isinstance(hooks, dict):
        raise Refusal("the file's 'hooks' key is not a JSON object")
    for event, ours in snippet(agent)["hooks"].items():
        existing = hooks.get(event)
        kept = (
            [e for e in existing if not is_ours(e)]
            if isinstance(existing, list)
            else []
        )
        hooks[event] = [*kept, *ours]
    return out


def removed(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` with our entries gone, empty event lists pruned, the rest as it was."""
    out: dict[str, Any] = json.loads(json.dumps(data))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not is_ours(e)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def describe(before: dict[str, Any], after: dict[str, Any], agent: str) -> list[str]:
    lines: list[str] = []
    b_hooks = _hooks_of(before)
    a_hooks = _hooks_of(after)
    for event in sorted(set(b_hooks) | set(a_hooks)):
        b_ours = sum(1 for e in b_hooks.get(event, []) if is_ours(e))
        a_ours = sum(1 for e in a_hooks.get(event, []) if is_ours(e))
        others = sum(1 for e in a_hooks.get(event, []) if not is_ours(e))
        if b_ours != a_ours:
            lines.append(
                f"  {event}: praxis entries {b_ours} -> {a_ours}"
                f" ({others} other entr{'y' if others == 1 else 'ies'} kept)"
            )
    if not lines:
        lines.append(f"  no change: the {agent} file already holds exactly this")
    return lines


def _hooks_of(data: dict[str, Any]) -> dict[str, list[Any]]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {}
    return {str(k): v for k, v in hooks.items() if isinstance(v, list)}


def backup(path: Path, home: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    keep = home / "settings-backups" / f"{path.name}.{stamp}"
    keep.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, keep)
    return keep


def write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.praxis-tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def apply(
    *,
    agent: str,
    remove: bool,
    home: Path,
    praxis_home: Path,
    stdin: TextIO,
    out: TextIO,
    assume_tty: bool | None = None,
) -> int:
    """The consent flow. Returns 0 written, 1 declined or unchanged, 2 refused."""
    path = target_path(agent, home)
    tty = (stdin.isatty() and out.isatty()) if assume_tty is None else assume_tty
    if not tty:
        out.write(
            f"refused: `praxis setup {agent} {'--remove' if remove else '--apply'}`"
            " writes a file outside this repository and needs your yes on a"
            " terminal (AGENTS.md invariant 8). Run it interactively, or use --print"
            " and paste.\n"
        )
        return 2
    try:
        before = load(path)
        after = removed(before) if remove else merged(before, agent)
    except Refusal as exc:
        out.write(f"refused: {exc.reason}\n")
        return 2
    out.write(f"{path}\n")
    for line in describe(before, after, agent):
        out.write(line + "\n")
    if after == before:
        return 1
    out.write("write this file? [y/N] ")
    out.flush()
    answer = stdin.readline().strip().lower()
    if answer not in ("y", "yes"):
        out.write("not written\n")
        return 1
    kept = backup(path, praxis_home)
    write_atomic(path, after)
    out.write(f"written {path}\n")
    if kept is not None:
        out.write(f"previous contents kept at {kept}\n")
    return 0
