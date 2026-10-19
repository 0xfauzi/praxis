"""E13 shared shapes: the ``Oracle`` every adapter returns, the adapter error, and the
subprocess and path helpers the adapters share. Standard library only."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

Pair = tuple[str, str]
# Caps against a hung toolchain, not measured bounds.
TOOL_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class Oracle:
    language: str
    repo: str
    head: str
    edges: list[Pair]
    dir_edges: list[Pair]
    unresolved: int
    seconds: float
    detail: dict[str, object] = field(default_factory=dict)


class AdapterError(RuntimeError):
    """The toolchain left nothing to parse."""


# --- shared helpers ---------------------------------------------------------------


def run(
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = TOOL_TIMEOUT_S,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        errors="surrogateescape",
        timeout=timeout,
        check=False,
    )


def head_of(repo: Path) -> str:
    done = run(["git", "rev-parse", "--short", "HEAD"], repo)
    return done.stdout.strip() or "unknown"


def listed_files(repo: Path) -> set[str]:
    """Tracked plus untracked-not-ignored paths that exist: the universe X1 reads."""
    argv = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    done = run(argv, repo)
    if done.returncode != 0:
        raise AdapterError(f"git ls-files failed in {repo.name}")
    return {p for p in done.stdout.split("\0") if p and (repo / p).is_file()}


def relative(path: str, root: Path) -> str | None:
    """Repository-relative POSIX path, or None when the path is outside ``root``."""
    full = os.path.realpath(path)
    base = os.path.realpath(root) + os.sep
    return full[len(base) :] if full.startswith(base) else None


def dir_of(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return parent if parent else "."


def to_dirs(edges: Iterable[Pair]) -> list[Pair]:
    """File edges lifted to directories; a pair inside one directory is not an edge."""
    lifted = {(dir_of(s), dir_of(d)) for s, d in edges}
    return sorted((s, d) for s, d in lifted if s != d)


def keep_listed(pairs: Iterable[Pair], listed: set[str]) -> tuple[list[Pair], int]:
    """Distinct non-self pairs with both ends listed, and how many were dropped."""
    distinct = {(s, d) for s, d in pairs if s != d}
    kept = sorted(p for p in distinct if p[0] in listed and p[1] in listed)
    return kept, len(distinct) - len(kept)
