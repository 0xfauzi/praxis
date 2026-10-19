"""E11: does `praxis.supply.measure` count the owner's existing transcripts correctly
and reproducibly, so that history can supply the Stage 2 cells for Q1, Q3 and Q15?

Acceptance rule, written before the first run (2026-10-19). The owner's machine is the
population; this container is the method check. On this container the runner passes
when all of these hold:

  A1. On a frozen copy of `~/.claude/projects/*/*.jsonl` (the E08 population), the
      supply counts equal the counts that the E08 runner prints for the same copy, for
      the fields that both count: files read, human-typed prompts, AskUserQuestion and
      ExitPlanMode main-thread calls. The window is wide enough to hold every prompt;
      the runner checks that the earliest week in it is unobservable.
  A2. The decision and session counts equal the E08 write-up (docs/experiments/E08.md):
      0 AskUserQuestion, 0 ExitPlanMode, 1 file. The transcript is live, so human
      prompts grew after E08 and are compared under A1 only.
  A3. A second run of `measure` on the frozen copy, with the same `now`, returns the
      same Supply, so the registration denominator (`mutating`) is reproducible.
  A4. Nothing measured holds a path, a repository name or prompt text: the JSON output
      contains no `/` outside the population pattern and no cwd read from the files.

The copy lives in a temporary directory that is deleted when the run ends; it exists
only so that two readers see the same bytes while the session that writes the live
transcript continues. Only counts are printed.

Usage: uv run python experiments/E11/history_supply.py [CLAUDE_PROJECTS] [--weeks N]
"""

# ruff: noqa: T201 - a runner's printed output is the record its write-up quotes.
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from praxis import supply

ROOT = Path(__file__).resolve().parents[2]
E08 = ROOT / "experiments" / "E08" / "decision_supply.py"
E08_DOC = {"files": 1, "AskUserQuestion": 0, "ExitPlanMode": 0}
WEEKS = 8


def freeze(projects: Path, into: Path) -> list[Path]:
    copied = []
    for path in sorted(projects.glob("*/*.jsonl")):
        target = into / path.parent.name / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append(target)
    return copied


def e08_counts(projects: Path) -> dict[str, int]:
    """The first population block of the E08 runner's output (`*/*.jsonl`)."""
    out = subprocess.run(
        [sys.executable, str(E08), str(projects)],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    ).stdout
    block = out.split("\n== ")[0]
    found = {
        "files": re.search(r"(\d+) files, (\d+) unreadable", block),
        "human": re.search(r"human-typed prompts: (\d+)", block),
        "AskUserQuestion": re.search(r"AskUserQuestion: (\d+) main-thread", block),
        "ExitPlanMode": re.search(r"ExitPlanMode: (\d+) main-thread", block),
    }
    missing = [k for k, m in found.items() if m is None]
    if missing:
        raise SystemExit(f"E08 output lacks {missing}")
    return {k: int(m.group(1)) for k, m in found.items() if m is not None}


def supply_counts(found: supply.Supply) -> dict[str, int | None]:
    total = found.total
    decisions = total.decisions or {}
    return {
        "files": found.files,
        "human": total.human,
        "AskUserQuestion": decisions.get("AskUserQuestion", {}).get("calls"),
        "ExitPlanMode": decisions.get("ExitPlanMode", {}).get("calls"),
    }


def cwds(files: list[Path]) -> set[str]:
    seen: set[str] = set()
    for path in files:
        for line in path.read_bytes().split(b"\n"):
            try:
                entry = json.loads(line) if line.strip() else None
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("cwd"), str):
                seen.add(entry["cwd"])
    return seen


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("projects", nargs="?", type=Path)
    ap.add_argument("--weeks", type=int, default=WEEKS)
    args = ap.parse_args(argv)
    weeks: int = args.weeks
    projects: Path = args.projects or Path.home() / ".claude" / "projects"
    codex = Path.home() / ".codex" / "sessions"
    now = dt.datetime.now(dt.UTC)
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="e11-") as tmp:
        files = freeze(projects, Path(tmp))
        e08 = e08_counts(Path(tmp))
        start = time.perf_counter()
        first = supply.measure(Path(tmp), codex, weeks, now=now)
        took = time.perf_counter() - start
        second = supply.measure(Path(tmp), codex, weeks, now=now)
        places = cwds(files)
    ours = supply_counts(first)
    checks["A1 window holds every prompt"] = first.weeks[0].coverage == "unobservable"
    checks["A1 equals E08 rerun"] = ours == e08
    checks["A2 equals E08 write-up"] = all(ours[k] == v for k, v in E08_DOC.items())
    checks["A3 second run equal"] = first == second
    text = json.dumps(first.to_json())
    leaked = [p for p in places if p and p in text]
    checks["A4 no path or name"] = not leaked and "/" not in text.replace(
        "claude_projects/*/*.jsonl", ""
    )
    live = supply.measure(projects, codex, weeks, now=dt.datetime.now(dt.UTC))

    print(f"E08 rerun on the frozen copy: {e08}")
    print(f"supply on the frozen copy:    {ours}")
    print(f"measure wall time: {took * 1000:.0f} ms for {first.files} files")
    print(f"distinct cwds in the files: {len(places)}")
    supply.render(first, sys.stdout)
    total, live_total = first.total, live.total
    print(
        f"live directory, a moment later: human {live_total.human}, mutating "
        f"{live_total.mutating} (frozen copy: {total.human}, {total.mutating})"
    )
    for name, ok in checks.items():
        print(f"{name}: {'pass' if ok else 'FAIL'}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
