"""E13, the third clause of A4 (docs/design/04-languages.md section 4.5): E04 rerun with
one TypeScript or JavaScript file touched. Adapted from experiments/E04/hook_cost.py.

Rule, written before any held-out run: on a ``git clone --local`` of the repository, the
Stop that scans X1 edges of one touched file and closes a registered task, with the
repository's mechanical map frozen, adds at most 100 ms to the wall-clock p95 of the same
Stop with no frozen map, and every such Stop ends ``ok`` with ``task closed`` in its
note. The file touched is the largest product TypeScript or JavaScript file (the
product filter of ts_accept.py) at most 512 KB, a line appended per session: the costliest
single file to read. 20 sessions per condition, the installed binary, one process per
event, payloads recorded from Claude Code 2.1.257 as E04 used them.

Usage: ts_stop_cost.py REPO --json OUT [--sessions N]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from shapes import listed_files
from ts_accept import CODE, product_files

from praxis import components, gitdiff, store

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "claude" / "2.1.257"
PAST = "2000-01-01T00:00:00.000Z"
LIMIT_MS = 100.0
MAX_BYTES = 512 * 1024
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True)  # fmt: skip
    return done.stdout


def payload(name: str, repo: Path, home: Path, sid: str, **over: str) -> dict[str, object]:
    text = (FIX / f"{name}.json").read_text()
    body: dict[str, object] = json.loads(
        text.replace("<repo>", str(repo)).replace("<home>", str(home))
    )
    body.update(session_id=sid, cwd=str(repo), **over)
    return body


def session(repo: Path, home: Path, ph: Path, target: Path, wall: dict[str, list[float]]) -> str:
    env = {"HOME": str(home), "PRAXIS_HOME": str(ph), "PATH": os.environ["PATH"]}
    sid = str(uuid.uuid4())
    start = {"session_id": sid, "cwd": str(repo), "hook_event_name": "SessionStart",
             "source": "startup"}  # fmt: skip
    prompt = {"session_id": sid, "cwd": str(repo), "hook_event_name": "UserPromptSubmit",
              "prompt": "x", "prompt_id": sid}  # fmt: skip
    steps = [
        start,
        prompt,
        payload("pre_tool_use_edit", repo, home, sid, tool_use_id=f"t-{sid}"),
        payload("post_tool_use_edit", repo, home, sid, tool_use_id=f"t-{sid}"),
        payload("stop", repo, home, sid),
    ]
    binary = shutil.which("praxis") or "praxis"
    for event, body in zip(EVENTS, steps, strict=True):
        if event == "PostToolUse":
            with target.open("a") as handle:
                handle.write("\nexport const e13StopCost = 1;\n")
        t0 = time.perf_counter()
        subprocess.run([binary, "hook"], input=json.dumps(body).encode(), env=env,
                       check=True, capture_output=True)  # fmt: skip
        wall.setdefault(event, []).append((time.perf_counter() - t0) * 1000)
    return sid


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def condition(repo: Path, home: Path, ph: Path, target: Path, n: int, before=None) -> dict[str, object]:  # type: ignore[no-untyped-def]
    wall: dict[str, list[float]] = {}
    sids = []
    for i in range(n):
        if before is not None:
            before(i)
        sids.append(session(repo, home, ph, target, wall))
        git(repo, "checkout", "-q", "--", ".")
    db = sqlite3.connect(ph / "ledger.db")
    rows = db.execute(
        "SELECT event, duration_ms, note, outcome FROM hook_events WHERE session_id IN"
        " (SELECT value FROM json_each(?))", (json.dumps(sids),)
    ).fetchall()
    db.close()
    stops = [(o, note) for e, _, note, o in rows if e == "Stop"]
    inner = [d for e, d, _, _ in rows if e == "Stop"]
    return {
        "stop_wall_p50": round(pct(wall["Stop"], 50), 1),
        "stop_wall_p95": round(pct(wall["Stop"], 95), 1),
        "stop_inner_p50": round(pct(inner, 50), 1),
        "stop_inner_p95": round(pct(inner, 95), 1),
        "stop_notes": sorted({str(n) for _, n in stops})[:4],
        "closed_ok": sum(o == "ok" and bool(n) and "task closed" in n for o, n in stops),
        "stops": len(stops),
    }


def largest_product_file(repo: Path) -> Path:
    listed = listed_files(repo)
    product, _ = product_files(repo, listed)
    sizes = [(repo.joinpath(p).stat().st_size, p) for p in product if p.endswith(CODE)]
    return repo / max(s for s in sizes if s[0] <= MAX_BYTES)[1]


def measure(source: Path, n: int) -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="e13-stop-"))
    try:
        home = root / "home"
        home.mkdir()
        plain = root / "plain"
        duty = root / "duty"
        for clone in (plain, duty):
            subprocess.run(["git", "clone", "-q", "--local", str(source), str(clone)],
                           check=True)  # fmt: skip
        target = largest_product_file(plain)
        rel = target.relative_to(plain)
        cost_plain = condition(plain, home, root / "ph-plain", target, n)
        ph = root / "ph-duty"
        top = gitdiff.toplevel(duty)
        assert top is not None
        cmap = components.derive(duty, gitdiff.repo_id(top))
        led = store.Store(ph / "ledger.db").open()
        led.component_map_freeze(
            cmap.repo_id, led.component_map_put(components.to_json(cmap), PAST), PAST
        )
        led.close()

        def register(i: int) -> None:
            db = store.Store(ph / "ledger.db").open()
            db.task_register(
                region=[], bound=[], related=[], drawn=[], task_id=f"task-{i}",
                repo_id=cmap.repo_id, map_version=cmap.version, ordinal=i + 1, ts=PAST,
                arm="control", seed_hash="ab" * 32, stratum="empty",
            )  # fmt: skip
            db.close()

        cost_duty = condition(duty, home, ph, duty / rel, n, register)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    added = cost_duty["stop_wall_p95"] - cost_plain["stop_wall_p95"]  # type: ignore[operator]
    passed = added <= LIMIT_MS and cost_duty["closed_ok"] == cost_duty["stops"] == n
    return {
        "repo": source.name,
        "file": str(rel),
        "file_bytes": (source / rel).stat().st_size,
        "map_version": cmap.version,
        "components": len(components.product_components(cmap)),
        "plain": cost_plain,
        "duty": cost_duty,
        "added_stop_wall_p95_ms": round(added, 1),
        "verdict": "pass" if passed else "fail",
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="E13 A4: Stop cost with one TS file")
    parser.add_argument("repo", type=Path)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=20)
    args = parser.parse_args(argv)
    result = measure(args.repo.resolve(), args.sessions)
    sys.stdout.write(json.dumps(result) + "\n")
    args.json.write_text(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
