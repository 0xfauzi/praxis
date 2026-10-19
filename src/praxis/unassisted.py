"""Repository location and the attached-agent check that gates every unassisted probe.

Where /proc is missing the check counts any agent process anywhere, the stricter
reading, because a probe answered with an agent attached is not unassisted.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from praxis.gitdiff import repo_id as make_repo_id
from praxis.gitdiff import toplevel

AGENT_NAMES = frozenset({"claude", "codex"})


@dataclass(frozen=True)
class Repo:
    root: Path
    repo_id: str


def locate(cwd: Path) -> Repo | None:
    top = toplevel(cwd)
    return None if top is None else Repo(Path(top), make_repo_id(top))


def attached_agents(root: Path) -> list[str]:
    """Agent processes working inside the repository. From /proc where it exists; where
    it does not, any agent process anywhere, which is the stricter reading."""
    proc = Path("/proc")
    return _agents_from_proc(proc, root) if proc.is_dir() else _agents_from_ps()


def _agents_from_proc(proc: Path, root: Path) -> list[str]:
    found: list[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        name = _agent_name(entry)
        if name is None:
            continue
        try:
            cwd = (entry / "cwd").readlink()
        except OSError:
            continue
        if cwd == root or root in cwd.parents:
            found.append(f"{name} pid {entry.name} in {cwd}")
    return found


def _agents_from_ps() -> list[str]:
    try:
        listing = subprocess.run(
            ["ps", "-eo", "pid=,comm=,args="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return ["process listing unavailable; cannot rule an agent out"]
    found: list[str] = []
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and _basename_in(parts[1:]):
            found.append(f"{parts[1]} pid {parts[0]} (cwd unknown)")
    return found


def _agent_name(entry: Path) -> str | None:
    try:
        argv = (entry / "cmdline").read_bytes().split(b"\x00")
        comm = (entry / "comm").read_text().strip()
    except OSError:
        return None
    names = [comm, *(Path(a.decode("utf-8", "replace")).name for a in argv[:2] if a)]
    return next((n for n in names if n in AGENT_NAMES), None)


def _basename_in(fields: list[str]) -> bool:
    return any(Path(f.split()[0]).name in AGENT_NAMES for f in fields if f.strip())
