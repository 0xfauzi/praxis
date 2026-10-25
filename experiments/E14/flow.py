"""X2 for E14: the scenario traces that label an L2 item, run in the E14 scratch clone.

Each scenario runs through ``praxis.extract.run_scenario`` with the source roots that
``praxis run scenarios`` uses. ``praxis`` is put on PATH as a two-line wrapper that runs
``praxis.cli.main`` in this interpreter without -I, so the clone's code and the tracer's
sitecustomize are the ones that load. After each run every tracked file but the mutant
is checked out again and untracked files are cleaned, because a scenario may write the
tree (the design 3.2 ``hook-edit`` scenario appends to README.md). Standard library
only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from praxis.components import ComponentMap, assign
from praxis.extract import Scenario, run_scenario
from praxis.scenarios import _environment, _roots, _scenario

if TYPE_CHECKING:
    from pathlib import Path

TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Shape:
    """What an L2 label reads from one trace: sets, never counts."""

    components: frozenset[str]
    edges: frozenset[tuple[str, str]]
    exit_code: int | None
    outcome: str


def load(path: Path) -> list[Scenario]:
    with path.open("rb") as handle:
        doc = tomllib.load(handle)
    tables = doc.get("scenario")
    if not isinstance(tables, list) or not tables:
        raise ValueError(f"{path}: expected [[scenario]] tables")
    return [_scenario(table, path) for table in tables]


def wrapper(bindir: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "praxis"
    call = "import sys; from praxis.cli import main; sys.exit(main())"
    script.write_text(f"#!/bin/sh\nexec '{sys.executable}' -c '{call}' \"$@\"\n")
    script.chmod(0o755)


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(clone), *args], capture_output=True, check=True, text=True
    )
    return done.stdout


def restore(clone: Path, keep: str | None) -> None:
    changed = [p for p in _git(clone, "diff", "--name-only", "-z").split("\0") if p]
    others = [p for p in changed if p != keep]
    if others:
        _git(clone, "checkout", "--", *others)
    _git(clone, "clean", "-fdq")
    shutil.rmtree(clone / ".praxis-scenario-home", ignore_errors=True)


def traces(
    clone: Path, cmap: ComponentMap, scenarios: list[Scenario], keep: str | None
) -> dict[str, Shape]:
    found: dict[str, Shape] = {}
    with _environment(clone, _roots(clone, cmap)):
        for scenario in scenarios:
            try:
                trace = run_scenario(clone, scenario, partial(assign, cmap), TIMEOUT_S)
            finally:
                restore(clone, keep)
            found[scenario.id] = Shape(
                trace.components,
                frozenset(trace.call_edges),
                trace.exit_code,
                trace.outcome,
            )
    return found


def environment(bindir: Path) -> None:
    """Process-wide: the wrapper first on PATH, and no bytecode anywhere, so a mutant
    of the same size and mtime second as its base is never read from a stale .pyc."""
    os.environ["PATH"] = os.pathsep.join([str(bindir), os.environ.get("PATH", "")])
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
