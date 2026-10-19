"""`praxis doctor`: what is installed, what is recorded, what is pending. Read-only: it
opens the ledger with SQLite's read-only mode and never creates it. Probes due are read
through probe.due, which opens a Store; that open migrates when the schema version
differs, so doctor asks for them only from a ledger at the current version."""

from __future__ import annotations

import platform
import sqlite3
import subprocess
from typing import TYPE_CHECKING, TextIO

from praxis import hooks, install
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
        lines += _measurement(db)
    finally:
        db.close()
    return lines


def _measurement(db: sqlite3.Connection) -> list[str]:
    """The instrument's state: frozen maps, tasks, binding, voids and flags."""
    try:
        maps = db.execute(_FROZEN).fetchall()
        registered, bound = db.execute(_BINDING).fetchone()
        grouped = [(label, db.execute(sql).fetchall()) for label, sql in _GROUPED]
    except sqlite3.Error:
        return ["  measurement tables: absent; the next hook event adds them"]
    lines = [
        f"  repository {r}: frozen map {v} ({s})" if v is not None
        else f"  repository {r}: no frozen map; `praxis components freeze`"
        for r, v, s in maps
    ]  # fmt: skip
    rate = f"{bound} of {registered}" if registered else "no task registered"
    lines.append(f"  binding rate (bound or closed over registered): {rate}")
    for label, rows in grouped:
        found = ", ".join(f"{k} {n}" for k, n in rows) or "none"
        lines.append(f"  {label}: {found}")
    return lines


def _due(repo: Repo, praxis_home: Path) -> str:
    path = praxis_home / "ledger.db"
    if not path.exists():
        return "probes due: no ledger"
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            (version,) = db.execute("PRAGMA user_version").fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return f"probes due: unknown, the ledger cannot be read ({exc})"
    if version != SCHEMA_VERSION:
        return "probes due: unknown until the next hook event migrates the ledger"
    # Imported here: cli imports this module, and every hook pays for what it imports.
    from praxis import probe

    due = probe.due(praxis_home, repo.repo_id, hooks.now_iso(), probe.L_DAYS)
    window = f"{probe.L_DAYS:g} to {probe.L_MAX_DAYS:g} days after exposure"
    return f"probes due now (structural, {window}): {len(due)}"


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
        out.write(_due(repo, praxis_home) + "\n")
        from praxis import scenarios  # lazily, as probe: cli imports this module

        out.write(scenarios.summary(praxis_home, repo.repo_id) + "\n")
    return 0
