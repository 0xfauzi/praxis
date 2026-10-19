"""`praxis doctor`: what is installed, what is recorded, what is pending. Read-only: it
opens the ledger with SQLite's read-only mode and never creates it."""

from __future__ import annotations

import platform
import sqlite3
import subprocess
from typing import TYPE_CHECKING, TextIO

from praxis import install
from praxis.unassisted import attached_agents, locate

if TYPE_CHECKING:
    from pathlib import Path


def _git_version() -> str:
    try:
        done = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return "git: not found"
    return done.stdout.strip() or "git: no version reported"


def _hooks_state(agent: str, home: Path) -> str:
    path = install.target_path(agent, home)
    if not path.exists():
        return f"{agent}: {path} absent; `praxis setup {agent} --apply` installs"
    try:
        data = install.load(path)
    except install.Refusal as exc:
        return f"{agent}: {exc.reason}"
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return f"{agent}: no hooks in {path}; `praxis setup {agent} --apply` installs"
    ours = sorted(
        e
        for e, v in hooks.items()
        if isinstance(v, list) and any(map(install.is_ours, v))
    )
    want = [*install.EVENTS[agent]["unmatched"], *install.EVENTS[agent]["matched"]]
    missing = [e for e in want if e not in ours]
    if not ours:
        return (
            f"{agent}: hooks present, none are praxis; `praxis setup {agent} --apply`"
        )
    if missing:
        return (
            f"{agent}: praxis hooks on {len(ours)} events, missing {', '.join(missing)}"
        )
    return f"{agent}: praxis hooks installed on all {len(ours)} events"


def _ledger_state(praxis_home: Path) -> list[str]:
    path = praxis_home / "ledger.db"
    if not path.exists():
        return [f"ledger: {path} absent; the first hook event creates it"]
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [f"ledger: {path} cannot be opened read-only ({exc})"]
    lines = [f"ledger: {path}"]
    try:
        for label, sql in (
            ("sessions", "SELECT COUNT(DISTINCT session_id) FROM hook_events"),
            ("hook events", "SELECT COUNT(*) FROM hook_events"),
            (
                "unobservable or error events",
                ("SELECT COUNT(*) FROM hook_events WHERE outcome <> 'ok'"),
            ),
            ("attribution rows", "SELECT COUNT(*) FROM attributions"),
        ):
            try:
                (n,) = db.execute(sql).fetchone()
            except sqlite3.Error:
                n = "table absent"
            lines.append(f"  {label}: {n}")
    finally:
        db.close()
    return lines


def run(*, cwd: Path, home: Path, praxis_home: Path, out: TextIO) -> int:
    out.write(f"python {platform.python_version()} on {platform.system()}\n")
    out.write(_git_version() + "\n")
    out.write(f"PRAXIS_HOME {praxis_home}\n")
    for agent in ("claude", "codex"):
        out.write(_hooks_state(agent, home) + "\n")
    for line in _ledger_state(praxis_home):
        out.write(line + "\n")
    repo = locate(cwd)
    if repo is None:
        out.write("cwd: not inside a git repository\n")
    else:
        agents = attached_agents(repo.root)
        out.write(f"cwd: repository {repo.repo_id}; agents attached: {len(agents)}\n")
    return 0
