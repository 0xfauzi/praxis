"""E12: does the praxis pipeline give uniform p values when nothing can work?

Acceptance rule, written before the first run (2026-10-19). Under the null every test
the report prints must give a p uniform on [0, 1]. For each test in TESTS, over the R
runs in which the report printed a number for it:

  KS        the Kolmogorov-Smirnov distance of its p values from Uniform(0, 1) is below
            the 5 percent critical value 1.3581 / (sqrt(n) + 0.12 + 0.11 / sqrt(n))
            (Stephens 1974), n the runs with a number;
  share     the count of runs with p < 0.05 lies inside the central 95 percent interval
            of Binomial(n, 0.05), from the exact binomial CDF;
  placebo   the count of runs in which the pipeline placebo suspends the contrasts (its
            p < 0.05) is at most that interval's upper end.

The rule binds the uniform responder, the null the contract names. A failure names the
test and the leak it suggests: a share above the interval is a leak (the arm, the
sampling or the outcome reaches the scored items); a KS failure with the share inside
or below the interval is conservatism (ties or discreteness in the statistic), not a
leak. The general effect needs 3 repositories and each run has one, so its p is
recorded as printed (null) with the reason the report gives, and is not tested.

Two more responders run under the same rule with their own expectations, also written
before the first run. `lean` knows nothing and leans to "no" (weights 0.35, 0.30, 0.15,
0.10, 0.10 on 1 3 5 7 9), so its Brier depends on the truth: a truth-rate difference
between drawn-not-shown and never-drawn items, or between the arms, shows only here.
Expected: every test passes. `remembers` answers an item it was asked as a prequestion
by its truth (9 or 1) and every other item uniformly: a testing effect, the positive
control. Expected: specific fails with a share above the interval (power), and near,
untargeted, placebo and f1_shuffle pass (specificity).

The world. A temporary git repository holds a flat package of `--modules` modules with
real `from pkg import` lines (a seeded layered graph, `--density`), set with `praxis set
components` at T0, a Monday. Tasks start `--lead-weeks` after T0, so every start-week
candidate (design 6.4: weeks 2 to 7) has passed and every task is randomized. Each day
holds `--per-day` tasks: `praxis start task` answered by the responder, then one agent
segment replayed through `praxis hook` with the 2.1.303 recorded payloads (SessionStart
once a day; UserPromptSubmit, PreToolUse Write, the file edit, PostToolUse Write, Stop),
so the Stop closes the task. The segment edits one module drawn uniformly; with
`--edge-rate` it adds a `from pkg import` line, else a constant. The prompt names that
module and a canary, and a synthetic transcript entry holds it, so R1 and the f1 shuffle
can be computed. Every evening `praxis test structure` is answered by the responder, so
an exposed item is first probed 7 days and 11 hours after exposure. After the last
task, probing goes on for 8 days; then `praxis show report --json`.

The clock. Every command runs as `praxis.cli.main` in a fresh interpreter with piped
stdin and stdout, which is what the `praxis` console script runs, with one
substitution: `praxis.hooks.now_iso`, the clock that every command and hook reads at its
start, returns the runner's simulated time from E12_NOW. The runner writes no row:
every row comes from the binary. Clocks left real: task.py stamps forecast rows with
the wall clock (the report never reads that stamp); probe seconds are monotonic time.

What the runner cannot test: owner behaviour (the responders are scripts), real agent
segments (one recorded Write per task), and the probe lag in real time.

Usage: uv run python experiments/E12/null_pipeline.py [--runs 200] [--tasks 60]
  [--responder uniform|lean|remembers] [--jobs 4] [--out results.json] | --selfcheck
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import re
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "claude" / "2.1.303"
TESTS = ("specific", "near", "untargeted", "pipeline_placebo", "f1_shuffle")
ALPHA = 0.05
GRID = ("1", "3", "5", "7", "9")
LEAN = (0.35, 0.30, 0.15, 0.10, 0.10)
T0 = dt.datetime(2027, 1, 4, 9, tzinfo=dt.UTC)  # a Monday
PROBE_TAIL_DAYS = 8
CANARY = "E12CANARYq7x"
NOTICE = '{"terminalSequence": "\\u001b]9;praxis: result ready\\u0007"}'
COMMAND_TIMEOUT_S = 300.0
PROMPT_END = ("[1 3 5 7 9 s] ", "[1 3 5 7 9 s]: ")
FORECAST_Q = re.compile(r"Will the agent change code in (\S+)\?")
IMPORT_Q = re.compile(r"Does code in (\S+) import code in (\S+)\?")
CLOCK_SHIM = (
    "import os, sys\n"
    "import praxis.hooks\n"
    "praxis.hooks.now_iso = lambda: os.environ['E12_NOW']\n"
    "from praxis.cli import main\n"
    "sys.exit(main(sys.argv[1:]))\n"
)
Pair = tuple[str, str]


@dataclass(frozen=True)
class Plan:
    tasks: int = 60
    per_day: int = 3
    lead_weeks: int = 8
    modules: int = 12
    density: float = 0.15
    edge_rate: float = 0.2
    skip_rate: float = 0.1
    responder: str = "uniform"


@dataclass
class World:
    """One run's repository, homes, clock and the responder's memory."""

    root: Path
    plan: Plan
    rng: random.Random
    modules: list[str]
    edges: set[Pair]
    remembered: set[Pair] = field(default_factory=set)
    session: str = ""
    processes: int = 0
    answers: int = 0
    skips: int = 0
    arms: dict[str, int] = field(default_factory=dict)

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def praxis_home(self) -> Path:
        return self.root / "praxis-home"

    @property
    def home(self) -> Path:
        return self.root / "home"

    def env(self, now: dt.datetime) -> dict[str, str]:
        return {"HOME": str(self.home), "PRAXIS_HOME": str(self.praxis_home),
                "PATH": os.environ.get("PATH", ""), "E12_NOW": stamp(now)}  # fmt: skip


def stamp(t: dt.datetime) -> str:
    return (
        t.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


# -- the binary ---------------------------------------------------------------------


def praxis(
    w: World,
    args: Sequence[str],
    now: dt.datetime,
    stdin: bytes | None = None,
    answer: Callable[[str], str] | None = None,
) -> tuple[int, str]:
    """One command through praxis.cli.main with the simulated clock. ``answer`` gets
    each prompt line the command asks and returns the line to type."""
    w.processes += 1
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", CLOCK_SHIM, *args], cwd=w.repo, env=w.env(now),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip
    assert proc.stdin is not None
    assert proc.stdout is not None
    if stdin is not None:
        out, err = proc.communicate(stdin, timeout=COMMAND_TIMEOUT_S)
        return proc.returncode, out.decode()
    text = _converse(proc, answer or (lambda _q: ""))
    err = proc.communicate(timeout=COMMAND_TIMEOUT_S)[1]
    if proc.returncode not in (0, 2):
        raise RuntimeError(f"praxis {' '.join(args)} exited {proc.returncode}: {err!r}")
    return proc.returncode, text


def _converse(proc: subprocess.Popen[bytes], answer: Callable[[str], str]) -> str:
    assert proc.stdin is not None
    assert proc.stdout is not None
    fd, buf, seen = proc.stdout.fileno(), bytearray(), 0
    deadline = time.monotonic() + COMMAND_TIMEOUT_S
    while True:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            proc.kill()
            raise TimeoutError("a praxis command did not finish")
        chunk = os.read(fd, 65536)
        if not chunk:
            return buf.decode()
        buf += chunk
        text = buf.decode(errors="replace")
        if len(text) > seen and text.endswith(PROMPT_END):
            # Only the text after the last answered prompt: the prompts end with no
            # newline, so one output line holds every prompt of a session.
            new, seen = text[seen:], len(text)
            proc.stdin.write((answer(new) + "\n").encode())
            proc.stdin.flush()


def hook(w: World, name: str, now: dt.datetime, **override: object) -> str:
    """One recorded 2.1.303 payload, paths and ids substituted, through `praxis hook`.
    Invariant 7 is checked on every call: exit 0, stdout empty or the one notice."""
    text = (FIXTURES / f"{name}.json").read_text()
    text = text.replace("<repo>", str(w.repo)).replace("<home>", str(w.home))
    body = json.loads(text)
    body.update(session_id=w.session, cwd=str(w.repo), **override)
    code, out = praxis(w, ["hook"], now, stdin=json.dumps(body).encode())
    if code != 0 or out not in ("", NOTICE):
        raise RuntimeError(f"praxis hook {name}: exit {code}, stdout {out[:80]!r}")
    return out


# -- the world ----------------------------------------------------------------------


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   timeout=60)  # fmt: skip


def build(root: Path, plan: Plan, rng: random.Random) -> World:
    """The flat package: a seeded layer order, each later module importing each
    earlier one with probability ``density``; committed once."""
    modules = [f"m{i:02d}" for i in range(1, plan.modules + 1)]
    order = rng.sample(modules, len(modules))
    edges = {(b, a) for i, a in enumerate(order) for b in order[i + 1 :]
             if rng.random() < plan.density}  # fmt: skip
    w = World(root, plan, rng, modules, edges)
    pkg = w.repo / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for m in modules:
        imports = "".join(f"from pkg import {b}\n" for a, b in sorted(edges) if a == m)
        (pkg / f"{m}.py").write_text(f"{imports}NAME = {m!r}\n")
    (w.home / ".claude" / "projects" / "-e12-repo").mkdir(parents=True)
    git(w.repo, "init", "-q")
    git(w.repo, "config", "user.email", "e12@example.invalid")
    git(w.repo, "config", "user.name", "e12")
    git(w.repo, "add", "-A")
    git(w.repo, "commit", "-q", "-m", "base")
    return w


# -- the responders -----------------------------------------------------------------


def respond(w: World, question: str, *, registering: bool) -> str:
    """The grid key for one question; no responder sees the arm."""
    w.answers += 1
    rng = w.rng
    if rng.random() < w.plan.skip_rate:
        w.skips += 1
        return "s"
    found = IMPORT_Q.search(question)
    if found is None:  # an F1 forecast: every responder answers it uniformly
        if FORECAST_Q.search(question) is None:
            raise ValueError("an unexpected prompt")
        return rng.choice(GRID)
    pair = (found.group(1), found.group(2))
    truth = pair in w.edges
    if w.plan.responder == "lean":
        return rng.choices(GRID, weights=LEAN)[0]
    if w.plan.responder == "remembers":
        if registering:
            w.remembered.add(pair)
        elif pair in w.remembered:
            return "9" if truth else "1"
    return rng.choice(GRID)


# -- one run ------------------------------------------------------------------------


def task(w: World, ordinal: int, at: dt.datetime) -> float:
    """Register, then one recorded segment that closes it; the wall seconds."""
    started = time.perf_counter()
    code, out = praxis(w, ["start", "task"], at,
                       answer=lambda q: respond(w, q, registering=True))  # fmt: skip
    found = re.search(rf"recorded task {ordinal} as a (treatment|control) task", out)
    if code != 0 or found is None:
        raise RuntimeError(f"task {ordinal} was not registered: {out[-200:]!r}")
    w.arms[found.group(1)] = w.arms.get(found.group(1), 0) + 1
    touched = w.rng.choice(w.modules)
    prompt_id = f"prompt-{ordinal:03d}"
    text = f"Change {touched} for task {ordinal}. {CANARY}"
    transcript(w, prompt_id, text, at + dt.timedelta(minutes=1))
    hook(w, "user_prompt_submit", at + dt.timedelta(minutes=1), prompt_id=prompt_id,
         prompt=text)  # fmt: skip
    tid = f"toolu_e12_{ordinal:03d}"
    path = w.repo / "src" / "pkg" / f"{touched}.py"
    before = path.read_text()
    after = before + edit(w, touched, ordinal)
    write = {"file_path": str(path), "content": after}
    hook(w, "pre_tool_use_write", at + dt.timedelta(minutes=2), prompt_id=prompt_id,
         tool_use_id=tid, tool_input=write)  # fmt: skip
    path.write_text(after)
    response = {"type": "update", "filePath": str(path), "content": after,
                "structuredPatch": [], "originalFile": before}  # fmt: skip
    hook(w, "post_tool_use_write", at + dt.timedelta(minutes=3), prompt_id=prompt_id,
         tool_use_id=tid, tool_input=write, tool_response=response)  # fmt: skip
    if hook(w, "stop", at + dt.timedelta(minutes=4), prompt_id=prompt_id) != NOTICE:
        raise RuntimeError(f"task {ordinal}: the Stop did not close the segment")
    return time.perf_counter() - started


def edit(w: World, module: str, ordinal: int) -> str:
    others = [m for m in w.modules if m != module and (module, m) not in w.edges]
    if others and w.rng.random() < w.plan.edge_rate:
        target = w.rng.choice(others)
        w.edges.add((module, target))
        return f"from pkg import {target}\n"
    return f"VALUE_{ordinal} = {ordinal}\n"


def transcript(w: World, prompt_id: str, text: str, at: dt.datetime) -> None:
    """A synthetic Claude Code 2.1.303 user entry, where `report` reads prompt names."""
    entry = {"type": "user", "promptId": prompt_id, "timestamp": stamp(at),
             "origin": {"kind": "human"}, "sessionId": w.session,
             "message": {"role": "user", "content": text}}  # fmt: skip
    path = w.home / ".claude" / "projects" / "-e12-repo" / f"{w.session}.jsonl"
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def probe(w: World, at: dt.datetime) -> tuple[int, float]:
    started = time.perf_counter()
    code, out = praxis(w, ["test", "structure"], at,
                       answer=lambda q: respond(w, q, registering=False))  # fmt: skip
    if code != 0:
        raise RuntimeError(f"praxis test structure exited {code}: {out[-200:]!r}")
    found = re.search(r"Answered: (\d+)\.", out)
    return (int(found.group(1)) if found else 0), time.perf_counter() - started


def run(seed: int, plan: Plan, workdir: Path, keep: bool = False) -> dict[str, object]:
    """One whole study under the null; the report's p values and the timings."""
    root = workdir / f"run-{seed:05d}"
    if root.exists():
        shutil.rmtree(root)
    started = time.perf_counter()
    w = build(root, plan, random.Random(seed))
    try:
        return _study(w, seed, started)
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


def _study(w: World, seed: int, started: float) -> dict[str, object]:
    code, out = praxis(w, ["set", "components"], T0)
    if code != 0 or f"{w.plan.modules} product components" not in out:
        raise RuntimeError(f"praxis set components: {out[-300:]!r}")
    d0 = T0 + dt.timedelta(weeks=w.plan.lead_weeks)
    days = math.ceil(w.plan.tasks / w.plan.per_day)
    task_s: list[float] = []
    probe_s: list[float] = []
    probed = 0
    for day in range(days + PROBE_TAIL_DAYS):
        date = d0 + dt.timedelta(days=day)
        first = day * w.plan.per_day + 1
        today = range(first, min(first + w.plan.per_day, w.plan.tasks + 1))
        if today:
            w.session = str(uuid.UUID(int=w.rng.getrandbits(128)))
            hook(w, "session_start", date - dt.timedelta(hours=1))
        for k, ordinal in enumerate(today):
            task_s.append(task(w, ordinal, date + dt.timedelta(hours=2 * k)))
        answered, secs = probe(w, date + dt.timedelta(hours=11))
        probed, probe_s = probed + answered, [*probe_s, secs]
    end = d0 + dt.timedelta(days=days + PROBE_TAIL_DAYS)
    report_started = time.perf_counter()
    code, out = praxis(w, ["show", "report", "--json"], end)
    if code != 0:
        raise RuntimeError(f"praxis show report --json exited {code}")
    report_s = time.perf_counter() - report_started
    doc = json.loads(out)
    return {
        "seed": seed, "repo_id": doc["repo_id"], "tests": doc["tests"],
        "suspended": doc["suspended"], "general": doc["general"],
        "blocks": {f"{b['measure']} {b['role']} {b['arm']}": b["n"]
                   for b in doc["blocks"]},
        "excluded": doc["excluded"], "probed": probed, "answers": w.answers,
        "skips": w.skips, "stored_skips": stored_skips(w.praxis_home),
        "arms": w.arms,
        "processes": w.processes, "edges_final": len(w.edges),
        "canary_absent": canary_absent(w.praxis_home),
        "task_s": task_s, "probe_session_s": probe_s, "report_s": report_s,
        "run_s": time.perf_counter() - started,
    }  # fmt: skip


def stored_skips(praxis_home: Path) -> int:
    """Unanswered forecast, prequestion and probe rows, read from the ledger after the
    run: each must be a skip the responder typed."""
    import sqlite3

    db = sqlite3.connect(f"file:{praxis_home / 'ledger.db'}?mode=ro", uri=True)
    try:
        sql = ("SELECT (SELECT COUNT(*) FROM forecasts WHERE p IS NULL)"
               " + (SELECT COUNT(*) FROM probe_responses WHERE p IS NULL)")  # fmt: skip
        return int(db.execute(sql).fetchone()[0])
    finally:
        db.close()


def canary_absent(praxis_home: Path) -> bool:
    """Invariant 5: no prompt text reaches any byte under $PRAXIS_HOME."""
    blob = b"".join(p.read_bytes() for p in praxis_home.rglob("*") if p.is_file())
    return CANARY.encode() not in blob


# -- the acceptance rule ------------------------------------------------------------


def ks_distance(ps: Sequence[float]) -> float:
    xs = sorted(ps)
    n = len(xs)
    return max(max((i + 1) / n - x, x - i / n) for i, x in enumerate(xs))


def ks_critical(n: int) -> float:
    root = math.sqrt(n)
    return 1.3581 / (root + 0.12 + 0.11 / root)


def binomial_interval(n: int, p: float = ALPHA) -> tuple[int, int]:
    """The central 95 percent interval of Binomial(n, p), from the exact CDF."""
    cdf, lo, hi = 0.0, None, None
    for k in range(n + 1):
        cdf += math.comb(n, k) * p**k * (1 - p) ** (n - k)
        if lo is None and cdf > 0.025:
            lo = k
        if hi is None and cdf >= 0.975:
            hi = k
    assert lo is not None
    assert hi is not None
    return lo, hi


LEAKS = {
    "specific": "the arm reaches the bound items' scores: allocation, binding or "
    "exclusion depends on the arm",
    "near": "the arm reaches the related items: binding or exclusion depends on it",
    "untargeted": "the arm reaches the drawn-not-shown items",
    "pipeline_placebo": "drawn-not-shown and never-drawn items differ with no effect: "
    "sampling or scheduling separates the two sets",
    "f1_shuffle": "forecasts beat shuffled pairings with no skill: the outcome or R1 "
    "reaches the forecast",
}


def verdict(name: str, ps: Sequence[float]) -> dict[str, object]:
    n = len(ps)
    if n == 0:
        return {"test": name, "n": 0, "pass": False, "why": "never computed"}
    d, crit = ks_distance(ps), ks_critical(n)
    low = sum(p < ALPHA for p in ps)
    lo, hi = binomial_interval(n)
    ks_ok, share_ok = d < crit, lo <= low <= hi
    why = None
    if low > hi:
        why = f"leak: {LEAKS[name]}"
    elif not ks_ok or low < lo:
        why = "conservative: ties or discreteness in the statistic, not a leak"
    return {"test": name, "n": n, "ks": d, "ks_critical": crit, "ks_pass": ks_ok,
            "below_alpha": low, "interval": [lo, hi], "share_pass": share_ok,
            "pass": ks_ok and share_ok, "why": why}  # fmt: skip


def p_values(run: dict[str, object]) -> dict[str, float | None]:
    """Each test's printed p in one run; None where the report printed no number."""
    tests = run["tests"]
    assert isinstance(tests, dict)
    return {t: None if tests[t] is None else float(tests[t]["p"]) for t in TESTS}


def summary(done: Sequence[dict[str, object]]) -> dict[str, object]:
    runs = [r for r in done if "error" not in r]
    errors = [r for r in done if "error" in r]
    found = [p_values(r) for r in runs]
    verdicts = [
        verdict(t, [p for f in found if (p := f[t]) is not None]) for t in TESTS
    ]
    suspended = sum(bool(r["suspended"]) for r in runs)
    hi = binomial_interval(len(runs))[1]
    passed = all(v["pass"] for v in verdicts)
    return {
        "runs": len(runs), "errors": errors, "verdicts": verdicts,
        "placebo_suspended": suspended, "placebo_suspended_max": hi,
        "placebo_pass": suspended <= hi,
        "accepted": not errors and passed and suspended <= hi,
        "any_below_alpha": sum(map(_any_below, found)),
        **_bookkeeping(runs),
    }  # fmt: skip


def _any_below(found: dict[str, float | None]) -> bool:
    return any(p is not None and p < ALPHA for p in found.values())


def _bookkeeping(runs: Sequence[dict[str, object]]) -> dict[str, object]:
    """What the runs must also show: skips stored as typed, no forced-control task, the
    canary absent, the general effect as printed, and the wall times."""
    skips = sum(_int(r["skips"]) for r in runs)

    def floats(key: str) -> list[float]:
        return [x for r in runs for x in _floats(r[key])]

    return {
        "skip_share": skips / sum(_int(r["answers"]) for r in runs),
        "skips_stored_as_typed": all(r["skips"] == r["stored_skips"] for r in runs),
        "forced_control_excluded": sum(_excluded(r, "forced_control") for r in runs),
        "general": sorted({json.dumps(r["general"], sort_keys=True) for r in runs}),
        "canary_absent": all(bool(r["canary_absent"]) for r in runs),
        "timing_s": {
            "task_median": statistics.median(floats("task_s")),
            "probe_session_median": statistics.median(floats("probe_session_s")),
            "report_median": statistics.median(floats("report_s")),
            "run_median": statistics.median(floats("run_s")),
            "run_max": max(floats("run_s")),
        },
    }


def _excluded(run: dict[str, object], reason: str) -> int:
    rows = run["excluded"]
    assert isinstance(rows, list)
    return sum(int(e["n"]) for e in rows if e["reason"] == reason)


def _int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _floats(value: object) -> list[float]:
    if isinstance(value, list):
        return [float(v) for v in value]
    assert isinstance(value, (int, float))
    return [float(value)]


# -- self-check of the rule's arithmetic --------------------------------------------


def selfcheck() -> int:
    """Values worked by hand or by a separate script, before this code ran."""
    checks = {
        # [0.1, 0.4, 0.7]: the steps give max(1/3-0.1, 2/3-0.4, 1-0.7) = 0.3.
        "ks of three": (round(ks_distance([0.7, 0.1, 0.4]), 12), 0.3),
        # One value at 0.5: max(1 - 0.5, 0.5 - 0) = 0.5.
        "ks of one": (ks_distance([0.5]), 0.5),
        # 1.3581 / (14.1421 + 0.12 + 0.0078) = 0.09517 at n = 200.
        "critical 200": (round(ks_critical(200), 4), 0.0952),
        # Binomial(20, 0.05): P(0) = 0.3585 > 0.025, so lo 0; P(X <= 2) = 0.9245,
        # P(X <= 3) = 0.9841 >= 0.975, so hi 3.
        "interval 20": (binomial_interval(20), (0, 3)),
        # Binomial(200, 0.05), summed with lgamma in a separate one-off script:
        # P(X <= 3) = 0.0090, P(X <= 4) = 0.0264, so lo 4; P(X <= 15) = 0.9556,
        # P(X <= 16) = 0.9762, so hi 16.
        "interval 200": (binomial_interval(200), (4, 16)),
    }
    bad = [k for k, (got, want) in checks.items() if got != want]
    for k, (got, want) in checks.items():
        sys.stdout.write(
            f"{'FAIL' if k in bad else 'ok  '} {k}: got {got}, want {want}\n"
        )
    sys.stdout.write("PASS\n" if not bad else "FAIL\n")
    return 1 if bad else 0


# -- main ---------------------------------------------------------------------------


def _job(job: tuple[int, Plan, str, bool]) -> dict[str, object]:
    """One run; a failure is a result (the pipeline broke), not an abort."""
    seed, plan, workdir, keep = job
    try:
        return run(seed, plan, Path(workdir), keep)
    except (RuntimeError, TimeoutError, subprocess.SubprocessError, OSError) as exc:
        return {"seed": seed, "error": f"{type(exc).__name__}: {exc}"[:500]}


def _sidecar(out: Path | None) -> Path | None:
    return None if out is None else out.with_suffix(".runs.jsonl")


def _recorded(
    out: Path | None, plan: Plan, seeds: list[int]
) -> dict[int, dict[str, object]]:
    """Runs a stopped batch already finished, read from the sidecar of ``out``.
    A line written under another plan is a different experiment and refuses."""
    side = _sidecar(out)
    if side is None or not side.is_file():
        return {}
    done: dict[int, dict[str, object]] = {}
    for line in side.read_text().splitlines():
        row = json.loads(line)
        if row["plan"] != asdict(plan):
            raise SystemExit(f"{side} holds runs of another plan: refused")
        seed = int(row["run"]["seed"])
        if seed in done:
            raise SystemExit(f"{side} holds seed {seed} twice: refused")
        if seed in seeds:
            done[seed] = row["run"]
    return done


def _record(out: Path | None, plan: Plan, run: dict[str, object]) -> None:
    side = _sidecar(out)
    if side is not None:
        with side.open("a") as f:
            f.write(json.dumps({"plan": asdict(plan), "run": run}, sort_keys=True))
            f.write("\n")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--workdir", type=Path, default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    for name, default in asdict(Plan()).items():
        ap.add_argument(f"--{name.replace('_', '-')}", type=type(default),
                        default=default)  # fmt: skip
    args = ap.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    if args.responder not in ("uniform", "lean", "remembers"):
        ap.error("--responder is uniform, lean or remembers")
    plan = Plan(**{k: getattr(args, k) for k in asdict(Plan())})
    seeds = [args.seed + i for i in range(args.runs)]
    done = _recorded(args.out, plan, seeds)
    with tempfile.TemporaryDirectory(prefix="e12-") as tmp:
        workdir = str(args.workdir or tmp)
        jobs = [(s, plan, workdir, args.keep) for s in seeds if s not in done]
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for one in pool.map(_job, jobs):
                done[int(str(one["seed"]))] = one
                _record(args.out, plan, one)
    runs = [done[s] for s in seeds]
    result = {"plan": asdict(plan), "summary": summary(runs), "runs": runs}
    text = json.dumps(result, indent=1, sort_keys=True)
    if args.out is not None:
        args.out.write_text(text + "\n")
    sys.stdout.write(json.dumps(result["summary"], indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
