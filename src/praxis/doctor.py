"""`praxis show status`: what is installed, what is recorded, what is due. Read-only: it
opens the ledger with SQLite's read-only mode and never creates it. Items due are read
through probe.due, which opens a Store; that open migrates when the schema version
differs, so this module asks for them only from a ledger at the current version.

hooks, probe and scenarios are imported where they are used: the knockout tracer counts
a module body that runs at import as a component that the scenario entered."""

from __future__ import annotations

import platform
import sqlite3
import subprocess
from typing import TYPE_CHECKING, TextIO

from praxis import install
from praxis.schema import SCHEMA_VERSION
from praxis.unassisted import Repo, attached_agents, locate

if TYPE_CHECKING:
    from pathlib import Path

# (label, query) per count; a query's rows are (key, count) and print as key count.
_GROUPED = (
    ("tasks by status", "SELECT status, COUNT(*) FROM tasks GROUP BY status"),
    (
        "void reasons",
        "SELECT void_reason, COUNT(*) FROM tasks WHERE void_reason IS NOT NULL"
        " GROUP BY void_reason",
    ),
    ("flags by code", "SELECT code, COUNT(*) FROM flags GROUP BY code"),
)
_FROZEN = (
    "SELECT r.repo_id, m.version, m.source FROM (SELECT repo_id FROM session_repos"
    " UNION SELECT repo_id FROM component_maps) r LEFT JOIN component_maps m"
    " ON m.repo_id = r.repo_id AND m.frozen_ts IS NOT NULL AND m.superseded_ts IS NULL"
    " ORDER BY r.repo_id"
)
_BINDING = "SELECT COUNT(*), SUM(status IN ('bound', 'closed')) FROM tasks"


def _git_version() -> str:
    try:
        done = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return "git: not found"
    return done.stdout.strip() or "git: no version in its output"


def _hooks_state(agent: str, home: Path) -> str:
    path = install.target_path(agent, home)
    run = f"To install the praxis hooks, run `praxis install {agent}`."
    if not path.exists():
        return f"{agent}: {path} does not exist. {run}"
    try:
        data = install.load(path)
    except install.Refusal as exc:
        return f"{agent}: {exc.reason}"
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return f"{agent}: {path} has no hooks. {run}"
    ours = {
        e
        for e, v in hooks.items()
        if isinstance(v, list) and any(map(install.is_ours, v))
    }
    want = [*install.EVENTS[agent]["unmatched"], *install.EVENTS[agent]["matched"]]
    missing = [e for e in want if e not in ours]
    if not missing:
        return f"{agent}: all {len(want)} events have a praxis hook."
    have = len(want) - len(missing)
    return (
        f"{agent}: {have} of {len(want)} events have a praxis hook. These events have"
        f" none: {', '.join(missing)}. {run}"
    )


def _ledger_state(praxis_home: Path) -> list[str]:
    path = praxis_home / "ledger.db"
    if not path.exists():
        return [f"ledger: {path} does not exist. The first hook event makes it."]
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [f"ledger: praxis cannot read {path} ({exc})."]
    lines = [f"ledger: {path}"]
    try:
        for label, sql in (
            ("sessions", "SELECT COUNT(DISTINCT session_id) FROM hook_events"),
            ("hook events", "SELECT COUNT(*) FROM hook_events"),
            (
                "hook events with the outcome unobservable or error",
                ("SELECT COUNT(*) FROM hook_events WHERE outcome <> 'ok'"),
            ),
            ("line records", "SELECT COUNT(*) FROM attributions"),
        ):
            try:
                (n,) = db.execute(sql).fetchone()
            except sqlite3.Error:
                n = "no table"
            lines.append(f"  {label}: {n}")
        lines += _measurement(db)
    finally:
        db.close()
    return lines


def _measurement(db: sqlite3.Connection) -> list[str]:
    """The instrument's state: set maps, tasks, binding, voids and flags."""
    try:
        maps = db.execute(_FROZEN).fetchall()
        registered, bound = db.execute(_BINDING).fetchone()
        grouped = [(label, db.execute(sql).fetchall()) for label, sql in _GROUPED]
    except sqlite3.Error:
        return ["  measurement tables: none. The next hook event makes them."]
    lines = [
        f"  repository {r}: set component map {v} ({s})" if v is not None
        else f"  repository {r}: no set component map. To set one, run"
             " `praxis set components` in that repository."
        for r, v, s in maps
    ]  # fmt: skip
    rate = (
        f"{bound} of {registered} recorded tasks have the status bound or closed"
        if registered
        else "no recorded task"
    )
    lines.append(f"  binding rate: {rate}")
    for label, rows in grouped:
        found = ", ".join(f"{k} {n}" for k, n in rows) or "none"
        lines.append(f"  {label}: {found}")
    return lines


def _due(repo: Repo, praxis_home: Path) -> str:
    path = praxis_home / "ledger.db"
    label = "items due for `praxis test structure`"
    if not path.exists():
        return f"{label}: no ledger"
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            (version,) = db.execute("PRAGMA user_version").fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return f"{label}: unknown. Praxis cannot read the ledger ({exc})."
    if version != SCHEMA_VERSION:
        return f"{label}: unknown until the next hook event updates the ledger"
    from praxis import hooks, probe

    due = probe.due(praxis_home, repo.repo_id, hooks.now_iso(), probe.L_DAYS)
    window = f"{probe.L_DAYS:g} to {probe.L_MAX_DAYS:g} days after exposure"
    return f"{label} ({window}): {len(due)}"


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
        out.write("current directory: not in a git repository\n")
    else:
        agents = attached_agents(repo.root)
        out.write(f"current directory: repository {repo.repo_id}\n")
        out.write(f"agents attached: {len(agents)}\n")
        out.write(_due(repo, praxis_home) + "\n")
        from praxis import scenarios

        out.write(scenarios.summary(praxis_home, repo.repo_id) + "\n")
    return 0
