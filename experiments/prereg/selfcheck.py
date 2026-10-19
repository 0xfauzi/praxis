"""derive.py --selfcheck: a pilot planted through store.py with values chosen by hand,
and every rule checked on hand-worked inputs (the acceptance rule in derive.py's
docstring). Split out of derive.py so that file stays under 800 lines; run it through
`derive.py --selfcheck`, which imports this module.
"""

from __future__ import annotations

import datetime as dt
import math
import subprocess
from typing import TYPE_CHECKING

from derive import (
    Segment,
    burn_in,
    derive,
    n_validation,
    rule_lag,
    touch_window,
)
from stats import (
    Inputs,
    ceil_to,
    fifo_gaps,
    icc1,
    nearest_rank,
    otsu,
    power,
    wilson_upper,
)

from praxis import scoring
from praxis.components import derive as derive_map
from praxis.components import to_json
from praxis.store import Evidence, Store

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# -- the self-check: a planted pilot with hand-worked answers -------------------------

S0 = dt.datetime(2026, 1, 5, 9, tzinfo=dt.UTC)
REPO = "0123456789abcdef"
EV = Evidence("full", ["selfcheck"], "selfcheck-v1", [], [])
FLAGS = {1: ("agent_previewed", "late_prediction"), 2: ("version_change",)}
FLAGS |= {3: ("late_prediction",), 4: ("abandoned",)}


def stamp(days: float = 0, minutes: float = 0) -> str:
    return (S0 + dt.timedelta(days=days, minutes=minutes)).isoformat()


def plant(home: Path, repo: Path) -> None:
    """Eleven tasks (one in week 1), 30 D1 items, one response each, four flagged
    tasks (three contaminated), 4 sessions in week 2 and 6 in week 3."""
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    for i in range(1, 7):
        (repo / "pkg" / f"m{i}.py").write_text("X = 1\n")
    for cmd in (["init", "-q"], ["add", "."]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    led = Store(home / "ledger.db").open()
    version = led.component_map_put(to_json(derive_map(repo, REPO)), stamp())
    led.component_map_freeze(REPO, version, stamp())
    comps = [f"m{i}" for i in range(1, 7)]
    pairs = [(a, b) for a in comps for b in comps if a != b]
    truth = [k % 3 == 0 for k in range(len(pairs))]
    rows = [
        ("D1", a, b, t, 0, t or None, t or None)
        for (a, b), t in zip(pairs, truth, strict=True)
    ]
    items = led.probe_items_put(rows=rows, ev=EV, repo_id=REPO, truth_ts=stamp(),
                                map_version=version,
                                extractor_version="x1-v1")  # fmt: skip
    for i in range(11):
        plant_task(led, version, i, [] if i == 0 else items[3 * (i - 1) : 3 * i], truth)
    seen = [(7, "a"), (8, "a"), (8, "b"), (9, "c"), (10, "d")]
    for day, sid in seen + [(15, f"w3-{n}") for n in range(6)]:
        led.hook_event(session_id=sid, event="Stop", tool_name=None, tool_use_id=None,
                       ts=stamp(day), duration_ms=1.0, outcome="ok",
                       note=None)  # fmt: skip
    led.close()


def plant_task(
    led: Store, version: str, i: int, mine: list[str], truth: list[bool]
) -> None:
    """Task i: bound at i minutes, closed, flagged per FLAGS, every item answered."""
    day, tid = (0 if i == 0 else 6 + i), f"{i + 1:016x}"
    led.task_register(region=[], bound=mine[:2], related=[], drawn=mine[2:],
                      task_id=tid, repo_id=REPO, map_version=version, ordinal=i + 1,
                      ts=stamp(day), arm="treatment", seed_hash=f"{i:064x}",
                      stratum="uniform")  # fmt: skip
    led.task_bind(tid, f"s{i}", None, stamp(day, max(i, 1)))
    led.task_close(tid, stamp(day, 120))
    for code in FLAGS.get(i, ()):
        led.flag(ts=stamp(day), code=code, detail={}, task_id=tid)
    for k, item in enumerate(mine, start=3 * (i - 1)):
        led.probe_response(ev=EV, item_id=item, task_id=tid, ts=stamp(day + 8),
                           p=scoring.GRID[k % 5], seconds=k + 1.0, lag_days=8.0,
                           deadline_met=True, truth_at_probe=truth[k],
                           truth_changed=False, head_sha=None, tree_hash=None,
                           dirty=None, extractor_version="x1-v1")  # fmt: skip


def rule_cases(expect: Callable[[str, object, object], None]) -> None:
    """N_VALIDATION, the lag rule and W_touch on inputs worked by hand."""
    expect("N, 1000 items and 1 task a week: 52 N under 250", n_validation(1000, 1), 4)
    expect("N, 209 items: 52 is under 52.25", n_validation(209, 1.0), 1)
    expect("N, 208 items: 52 is not under 52", n_validation(208, 1.0), 0)
    expect("N, 100 items: below a year of tasks, 208", n_validation(100, 1.0), 0)
    # Brier 0.01, 0.25, 0.49 for p 0.9, 0.5, 0.3 against 1; 0.25 for 0.5 against 0.
    # Short days [.01 .01] [.25 .25] [.49 .49]: odd = even, r 1, split-half 1. Long days
    # [.01 .25] [.25 .01] [.49 .49]: r 0.5, split-half 2(0.5)/1.5 = 0.667. 1 >= 0.767.
    short = [
        (1, 0.9, 1),
        (1, 0.9, 1),
        (2, 0.5, 1),
        (2, 0.5, 0),
        (3, 0.3, 1),
        (3, 0.3, 1),
    ]
    long = [
        (1, 0.9, 1),
        (1, 0.5, 1),
        (2, 0.5, 0),
        (2, 0.9, 1),
        (3, 0.3, 1),
        (3, 0.3, 1),
    ]
    rows = [lag_row(d, 3.0, p, o) for d, p, o in short]
    rows += [lag_row(d, 8.0, p, o) for d, p, o in long]
    rows.append({**lag_row(1, 8.0, 0.9, 1), "task_id": None, "lag_days": None})
    lag = rule_lag(rows)  # type: ignore[arg-type]
    expect(
        "L, exposed rows only",
        (lag.value, lag.note),
        (2.0, "split-half 1.000 and 0.667"),
    )
    # One component, never named, touched in tasks 1 and 2 and not in 3 and 4, 35 days
    # apart. 4 weeks: no history, 0.25 each. 8: 1/4, (2/3 - 1)^2, (2/3)^2, (1/3)^2 ->
    # 33/144. 13: 1/4, 1/9, (3/4)^2, (2/4)^2 -> 169/576. 26 and all: task 4 sees all
    # three, (3/5)^2 -> (1/4 + 1/9 + 9/16 + 9/25) / 4 = 4621/14400.
    segs = [Segment(("r", "v"), stamp(d), frozenset(), {"a": o})
            for d, o in ((0, 1), (35, 1), (70, 0), (105, 0))]  # fmt: skip
    found = touch_window(segs, lambda _: True, minimum=1)
    best, means = found if found is not None else (None, {})
    expect("W_touch, smallest Brier", best, 8)
    hand = {"all": 4621 / 14400, 26: 4621 / 14400, 13: 169 / 576, 8: 33 / 144, 4: 0.25}
    for weeks, want in hand.items():
        expect(f"W_touch, {weeks} weeks", means.get(weeks), want)
    expect("W_touch, 4 tasks under 10", touch_window(segs, lambda _: True), None)


def lag_row(day: int, lag: float, p: float, o: int) -> dict[str, object]:
    return {"ts": stamp(day), "p": p, "truth_at_probe": o, "task_id": "t",
            "lag_days": lag, "deadline_met": 1, "truth_changed": 0}  # fmt: skip


def selfcheck(tmp: Path) -> int:
    bad: list[str] = []

    def expect(name: str, got: object, want: object) -> None:
        same = (
            math.isclose(got, want)
            if isinstance(got, float) and isinstance(want, float)
            else got == want
        )
        print(f"{'ok  ' if same else 'FAIL'} {name}: got {got}, want {want}")
        if not same:
            bad.append(name)

    expect("P90 nearest rank of 1..30", nearest_rank(range(1, 31), 0.9), 27)
    expect("27 s up to 5 s", ceil_to(27.0, 5.0), 30.0)
    expect("Wilson 3 of 10, up to 0.01", ceil_to(wilson_upper(3, 10), 0.01), 0.56)
    expect("Otsu cut", otsu([0.0, 0.05, 0.1, 0.9, 0.95, 1.0]), 0.5)
    means = (0.30, 0.22, 0.215)
    expect("burn-in", burn_in([(j, means[j // 5]) for j in range(15)]), 5)
    expect("ICC identical pairs", icc1([[1, 1], [2, 2], [3, 3]]), 1.0)
    expect("ICC opposed pairs", icc1([[1, 2], [1, 2], [1, 2]]), -1.0)
    t0, t1, t2 = stamp(0), stamp(1), stamp(3)
    events = [("f", "h", 1, t0), ("f", "h", 1, t1), ("f", "h", -1, t2)]
    events.append(("f", "g", -1, t2))
    expect("FIFO pairs the first copy", fifo_gaps(events), [(t0, 3 * 86400.0)])
    rule_cases(expect)
    plant(tmp / "home", tmp / "repo")
    params = derive(tmp / "home", S0 + dt.timedelta(days=60), tmp, (2, 20, 1))
    got = {p.name: p.value for p in params or []}
    wanted = {"W s": 600.0, "deadline D1 s": 30.0, "L days": 7.0, "L_max days": 14.0}
    wanted |= {"exclusion cap": 0.56, "N sessions per week": 2}
    # All 30 D1 items are exposed, so the unasked pool, 0, is below a year of tasks at
    # 5 a week; no transcript holds a bound prompt, so W_touch cannot be applied.
    wanted |= {"N_VALIDATION": 0, "W_touch weeks": None}
    for name, want in wanted.items():
        expect(name, got.get(name, "missing"), want)
    x = Inputs(sd_item=0.25, icc=0.0, sd_week=0.0, rate=10.0, answer=1.0)
    pw = power(44, 0.54 * 0.25, x, 0.0, 400, 200, 7)
    expect("power 44 tasks g 0.54 within 0.72 to 0.88", 0.72 <= pw <= 0.88, True)
    print(f"simulated power {pw:.3f}; {'PASS' if not bad else 'FAIL'}")
    return 1 if bad else 0
