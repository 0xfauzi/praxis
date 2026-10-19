"""E08: how many structured decisions (AskUserQuestion, ExitPlanMode) do the agent
transcripts on a machine hold, and how many of them can S3 score?

Acceptance rule, written before the run (2026-10-19). This runner measures supply; it
decides nothing about the owner. The population the S3 minimum is judged on is the
owner's machine during the pilot (derive.py's S3 rule). On any other machine the result
is a count of that machine's sessions and is reported as such. The runner passes when
every file in the population is read (a file that cannot be read is counted, never
skipped) and the counts below are printed:

  - tool_use blocks by tool name, main thread and sidechain apart, over the population
    `~/.claude/projects/*/*.jsonl` (design Q3) and, separately, every `*.jsonl` below
    `~/.claude/projects` (subagent transcripts included);
  - per main-thread structured decision, whether `praxis.transcript.read` can score it
    (one question, multiSelect false, string labels: option_count not None) and whether
    its chosen index was recovered;
  - the transcript time each population spans (first to last timestamp), so a rate per
    30 days can be stated, and the human-typed prompts (`origin.kind == 'human'`).

Only tool names, counts and timestamps are read into memory as results; no prompt,
option label or tool input is printed or written.

Usage: uv run python experiments/E08/decision_supply.py [CLAUDE_PROJECTS]
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from praxis.transcript import TranscriptError, read

DECISIONS = ("AskUserQuestion", "ExitPlanMode")


def tool_names(path: Path) -> tuple[Counter[str], list[str], int]:
    """(tool name -> count, ':side' suffixed for sidechain; timestamps; bad lines)."""
    names: Counter[str] = Counter()
    stamps: list[str] = []
    bad = 0
    for line in path.read_bytes().split(b"\n"):
        entry = parse(line) if line.strip() else {}
        if entry is None:
            bad += 1
            continue
        if isinstance(entry.get("timestamp"), str):
            stamps.append(entry["timestamp"])
        side = ":side" if entry.get("isSidechain") is True else ""
        names.update(f"{name}{side}" for name in uses(entry))
    return names, stamps, bad


def parse(line: bytes) -> dict[str, Any] | None:
    try:
        entry = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return entry if isinstance(entry, dict) else None


def uses(entry: dict[str, Any]) -> list[str]:
    message = entry.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if not isinstance(blocks, list):
        return []
    return [
        str(b.get("name"))
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def span_days(stamps: list[str]) -> float | None:
    when = sorted(dt.datetime.fromisoformat(s) for s in stamps)
    return (when[-1] - when[0]).total_seconds() / 86400 if len(when) > 1 else None


def population(files: list[Path], label: str) -> None:
    names: Counter[str] = Counter()
    stamps: list[str] = []
    bad = unreadable = humans = 0
    scorable: Counter[str] = Counter()
    chosen: Counter[str] = Counter()
    for path in files:
        try:
            counted, seen, malformed = tool_names(path)
            session = read(path)
        except (OSError, TranscriptError):
            unreadable += 1
            continue
        names += counted
        stamps += seen
        bad += malformed
        humans += sum(p.origin_kind == "human" for p in session.prompts)
        for use in session.tools:
            if use.name in DECISIONS:
                scorable[use.name] += use.option_count is not None
                chosen[use.name] += use.chosen_index is not None
    days = span_days(stamps)
    print(f"== {label}: {len(files)} files, {unreadable} unreadable, {bad} bad lines")
    print(f"  tool_use blocks: {sum(names.values())}; human-typed prompts: {humans}")
    print(f"  span: {'unknown' if days is None else f'{days:.2f} days'}")
    for name, n in sorted(names.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"    {name}: {n}")
    for name in DECISIONS:
        total = names[name]
        per30 = None if not days else total * 30 / days
        print(
            f"  {name}: {total} main-thread, {names[name + ':side']} sidechain;"
            f" scorable {scorable[name]}, chosen index recovered {chosen[name]};"
            f" per 30 days {'unknown' if per30 is None else f'{per30:.1f}'}"
        )


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else Path.home() / ".claude" / "projects"
    population(sorted(root.glob("*/*.jsonl")), f"{root}/*/*.jsonl (design Q3)")
    population(sorted(root.rglob("*.jsonl")), f"{root}/**/*.jsonl (subagents too)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
