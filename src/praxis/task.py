"""`praxis start task` and `praxis show task`: registration, the start week, the
pre-task binding rule, the arm, forecast entry, and the result after a scored segment
(design 03 4.4 and 6.1-6.4).

Design 6.4: a repository is all control until its start week. ``draw_start`` draws it
once, when the repository's first component map is set, from the K_START ISO weeks that
begin START_GAP_WEEKS after that week, by a generator keyed by sha256 of
``json.dumps([SEED, repo_id, "start"])``. A task registered in an earlier week is
control and marked ``forced_control``, and its text is that of any control task: the
owner must not learn the schedule. From the start week the arm has its own generator,
keyed by the committed seed, the repository id and the task ordinal alone
(``seed_hash``, sha256 of ``json.dumps([seed, repo_id, ordinal])``; treatment when its
first ``random()`` is below 0.5), so neither the items drawn nor the owner's answers can
move it. Items and the forecast sample come from a second generator keyed by the same
three and ``"bind"``. The region's touch rate counts the closed tasks in the trailing
TOUCH_WINDOW_WEEKS. Nothing the owner types is kept: an answer outside the closed
vocabulary is asked again and dropped, not even its length. The result names bound items
only; a related or drawn-not-shown item is never shown.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from functools import partial
from random import Random
from typing import TYPE_CHECKING, TextIO

from praxis.components import assign, from_json, product_components
from praxis.extract import static_imports
from praxis.probe import GRID_HELP, GRID_KEYS, QUESTION, SKIP
from praxis.store import Evidence, Store
from praxis.unassisted import Repo, locate

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

# Design choices fixed in docs/preregistration.md 1.4; Stage 2 may restate them.
H, K_E, K_R, K_P, R = 3, 2, 1, 1, 5
P_TREATMENT = 0.5
# Design choices for design 6.4's candidate start weeks; derive.py may replace them.
K_START, START_GAP_WEEKS = 6, 2
# Design 6.2's trailing touch window in weeks, for the region and report's R_fam. None
# counts every closed task: the Stage 1 rule. Stage 2 sets it from derive.py's W_touch.
TOUCH_WINDOW_WEEKS: int | None = None
# The committed seed that arms and start weeks are drawn with: derive.py's default,
# cited in preregistration 1.7 (Rules derive.py applies); Stage 2 restates it (1.2).
SEED = 20261019
REDUCER_VERSION = "task-v1"
ASSUMPTIONS = (
    "typed 1 3 5 7 9 is read as 0.1 0.3 0.5 0.7 0.9; s or end of input is unanswered",
    "ts is the clock after the last answer, not the registration time",
    "the components asked are a seeded uniform sample of the frozen map's product",
)
YES_NO = {0: "no", 1: "yes", None: "unknown"}
START, SHOW = "praxis start task", "praxis show task"
BOUND = "Questions for this task, with the answer when praxis selected them and now:\n"
NO_REPO = "run this command in a git repository."


@dataclass(frozen=True)
class Draw:
    region: list[str]
    bound: list[str]
    related: list[str]
    drawn: list[str]
    stratum: str


def register(
    repo_root: Path,
    praxis_home: Path,
    stdin: TextIO,
    stdout: TextIO,
    now: str,
    seed: int,
) -> int:
    repo = locate(repo_root)
    if repo is None:
        stdout.write(f"{START}: {NO_REPO}\n")
        return 2
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        return _register(ledger, repo, (stdin, stdout), now, seed)
    finally:
        ledger.close()


def last(repo_root: Path, praxis_home: Path, stdout: TextIO) -> int:
    repo = locate(repo_root)
    if repo is None:
        stdout.write(f"{SHOW}: {NO_REPO}\n")
        return 2
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        _feedback(ledger, repo, stdout)
    finally:
        ledger.close()
    return 0


def _key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()


def _t(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)


def week_of(t: dt.datetime) -> str:
    """The ISO week, in UTC, as the ledger writes it: YYYY-Www."""
    year, week, _ = t.astimezone(dt.UTC).isocalendar()
    return f"{year}-W{week:02d}"


def in_window(closed_ts: str, t: dt.datetime) -> bool:
    """Whether a task closed before t counts in the trailing touch window at t."""
    weeks = TOUCH_WINDOW_WEEKS
    return weeks is None or _t(closed_ts) >= t - dt.timedelta(weeks=weeks)


def window_assumption() -> str:
    span = (
        "all history" if TOUCH_WINDOW_WEEKS is None else f"{TOUCH_WINDOW_WEEKS} weeks"
    )
    return (
        f"the trailing touch window TOUCH_WINDOW_WEEKS is {span}. All history is the "
        "Stage 1 rule, and Stage 2 sets the window from derive.py."
    )


def draw_start(ledger: Store, repo_id: str, map_version: str, now: str) -> str:
    """Design 6.4: the repository's start week, drawn at its first set map and read
    back after that. Nothing is printed: the owner must not learn the schedule."""
    found = ledger.repository_start(repo_id)
    if found is not None:
        return str(found["start_week"])
    weeks = range(START_GAP_WEEKS, START_GAP_WEEKS + K_START)
    candidates = [week_of(_t(now) + dt.timedelta(weeks=k)) for k in weeks]
    seed_hash = _key(SEED, repo_id, "start")
    week = Random(int(seed_hash, 16)).choice(candidates)
    ledger.repository_start_put(
        candidates=candidates, repo_id=repo_id, map_version=map_version, drawn_ts=now,
        start_week=week, seed_hash=seed_hash,
    )  # fmt: skip
    return week


def _register(
    ledger: Store, repo: Repo, io: tuple[TextIO, TextIO], now: str, seed: int
) -> int:
    stdin, out = io
    repo_id = repo.repo_id
    frozen = ledger.component_map_frozen(repo_id)
    start = ledger.repository_start(repo_id)
    if frozen is None or start is None:
        out.write(f"{START}: {_missing(frozen is None)}\n")
        return 2
    cmap = from_json(frozen)
    product = product_components(cmap)
    # Design 6.3: the last segment's result opens the next task, in both arms.
    _feedback(ledger, repo, out)
    ordinal = ledger.task_count(repo_id) + 1
    seed_hash = _key(seed, repo_id, ordinal)
    rng = Random(int(_key(seed, repo_id, ordinal, "bind"), 16))
    draw = _bind(ledger, repo_id, cmap.version, product, (rng, _t(now)))
    forced = week_of(_t(now)) < start["start_week"]
    treated = not forced and Random(int(seed_hash, 16)).random() < P_TREATMENT
    arm = "treatment" if treated else "control"
    task_id = _key(repo_id, ordinal)[:16]
    old = ledger.task_register(
        region=draw.region, bound=draw.bound, related=draw.related, drawn=draw.drawn,
        task_id=task_id, repo_id=repo_id, map_version=cmap.version, ordinal=ordinal,
        ts=now, arm=arm, seed_hash=seed_hash, stratum=draw.stratum,
        forced_control=forced,
    )  # fmt: skip
    if old is not None:
        out.write("The previous open task is now void. This task replaces it.\n")
    if treated:
        sample = rng.sample(sorted(product), min(R, len(product)))
        _elicit(ledger, task_id, sample, draw.bound, (stdin, out))
    out.write(f"Praxis recorded task {ordinal} as a {arm} task.\n")
    return 0


def _missing(no_map: bool) -> str:
    if no_map:
        return (
            "this repository has no set component map. To set one, run"
            " `praxis set components`."
        )
    return (
        "this repository has no study start record. To make one, run"
        " `praxis set components`."
    )


def _region(
    ledger: Store,
    repo_id: str,
    version: str,
    product: Sequence[str],
    draw: tuple[Random, dt.datetime],
) -> list[str]:
    """The h product components with the highest touch rate over the closed tasks on
    this map in the trailing touch window. A component with no outcome row in a task
    is unknown there, not untouched."""
    rng, now = draw
    touched: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    for task in ledger.task_rows(repo_id):
        if task["status"] != "closed" or task["map_version"] != version:
            continue
        if not in_window(task["closed_ts"], now):
            continue
        for row in ledger.outcomes_of(task["task_id"]):
            seen[row["component"]] += 1
            touched[row["component"]] += row["o_first"]
    order = sorted(product)
    rng.shuffle(order)  # ties at the cut fall at random, never alphabetically
    rated = [c for c in order if seen[c] and touched[c]]
    rated.sort(key=lambda c: -touched[c] / seen[c])
    return sorted(rated[:H])


def _bind(
    ledger: Store,
    repo_id: str,
    version: str,
    product: Sequence[str],
    draw: tuple[Random, dt.datetime],
) -> Draw:
    rng = draw[0]
    region = _region(ledger, repo_id, version, product, draw)
    # A validation-probed item is no longer blind (design 6.2, 6.3): out of the pool.
    pair = _pairs(ledger.probe_items_unprobed(repo_id, version))
    ids = list(pair)
    near = [i for i in ids if pair[i] & set(region)]
    bound = rng.sample(near, min(K_E, len(near)))
    rest = [i for i in ids if i not in bound]
    bound += rng.sample(rest, min(K_E - len(bound), len(rest)))
    # Related: shares exactly one component with a bound item and two with none.
    overlap = {i: max((len(pair[i] & pair[b]) for b in bound), default=0) for i in ids}
    linked = [i for i in ids if i not in bound and overlap[i] == 1]
    related = rng.sample(linked, min(K_R, len(linked)))
    left = [i for i in ids if i not in bound and i not in related]
    drawn = rng.sample(left, min(K_P, len(left)))
    return Draw(region, bound, related, drawn, _stratum(bound, near))


def _pairs(pool: Sequence[sqlite3.Row]) -> dict[str, set[str]]:
    """Each pool item's pair of components, refusing an item listed twice."""
    pair = {r["item_id"]: {r["src_component"], r["dst_component"]} for r in pool}
    if len(pair) != len(pool):
        raise ValueError("an unexposed item is listed twice")
    return pair


def _stratum(bound: Sequence[str], near: Sequence[str]) -> str:
    """A fill item cannot touch the region (the region stratum was exhausted first), so
    each bound item's stratum is recoverable from region_json; the task's is the
    weakest of them."""
    if not bound:
        return "empty"
    return "region" if set(bound) <= set(near) else "uniform"


def _ask(stdin: TextIO, out: TextIO, prompt: str) -> float | None:
    while True:
        out.write(f"{prompt} [1 3 5 7 9 s]: ")
        out.flush()
        line = stdin.readline()
        if not line:
            out.write("\n")
            return None
        key = line.strip()
        if key == SKIP:
            return None
        if key in GRID_KEYS:
            return GRID_KEYS[key]
        out.write("  Type one of the answers in the brackets.\n")


def _change(component: str) -> str:
    return f"Will the agent change code in {component}?"


def _question(item: sqlite3.Row) -> str:
    template = QUESTION[item["relation"]]
    return template.format(item["src_component"], item["dst_component"])


def _elicit(
    ledger: Store,
    task_id: str,
    sample: Sequence[str],
    bound: Sequence[str],
    io: tuple[TextIO, TextIO],
) -> None:
    stdin, out = io
    out.write("Before you write the prompt, answer each question.\n" + GRID_HELP)
    rows: list[tuple[str | None, float | None, str, str | None]] = [
        (c, _ask(stdin, out, f"  {_change(c)}"), "forecast", None) for c in sample
    ]
    items = [ledger.probe_item(i) for i in bound]
    if items:
        out.write("Answer these questions about the code as it is now.\n")
    for item_id, item in zip(bound, items, strict=True):
        if item is None:
            raise KeyError(f"bound item {item_id} is not in the pool")
        rows.append((None, _ask(stdin, out, f"  {_question(item)}"), "prequestion",
                     item_id))  # fmt: skip
    stamp = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
    ev = Evidence("full", [f"task:{task_id}"], REDUCER_VERSION, ASSUMPTIONS, [])
    ledger.forecast(task_id, rows, stamp.replace("+00:00", "Z"), ev=ev)


def _feedback(ledger: Store, repo: Repo, out: TextIO) -> None:
    task = ledger.task_last_closed(repo.repo_id)
    if task is None:
        out.write("No task is closed yet.\n")
        return
    task_id = task["task_id"]
    out.write(f"Last closed task: task {task['ordinal']}, a {task['arm']} task, closed "
              f"at {task['closed_ts']}\n")  # fmt: skip
    outcomes = ledger.outcomes_of(task_id)
    _touched(outcomes, out)
    _divergences(ledger, task_id, out)
    bound = [e for e in ledger.exposures_of(task_id) if e["role"] == "bound"]
    now = _truth_now(ledger, repo, task["map_version"], bound) if bound else {}
    out.write(BOUND if bound else "Questions for this task: none\n")
    for item in bound:
        drawn, current = YES_NO[item["truth"]], YES_NO[now[item["item_id"]]]
        out.write(f"  {_question(item)} {drawn}, now {current}\n")
    if task["arm"] == "treatment":
        _misses(ledger.forecasts_of(task_id), outcomes, bound, out)


def _truth_now(
    ledger: Store, repo: Repo, version: str, bound: Sequence[sqlite3.Row]
) -> dict[str, int | None]:
    """D1 truth re-read from the working tree, as a probe would: the segment may have
    changed it. Unknown for another relation, another map, an unparsed source file or
    a tree X1 cannot read."""
    unknown: dict[str, int | None] = {e["item_id"]: None for e in bound}
    frozen = ledger.component_map_frozen(repo.repo_id)
    cmap = None if frozen is None else from_json(frozen)
    if cmap is None or cmap.version != version:
        return unknown
    lift = partial(assign, cmap)
    try:
        graph = static_imports(repo.root, lift)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return unknown
    failed = {lift(f) for f in graph.parse_failures}
    edges = {(e.src, e.dst) for e in graph.edges}
    truth = dict(unknown)
    for e in bound:
        if e["relation"] == "D1" and e["src_component"] not in failed:
            truth[e["item_id"]] = int((e["src_component"], e["dst_component"]) in edges)
    return truth


def _edges(row: sqlite3.Row) -> str:
    added, removed = row["edges_added"], row["edges_removed"]
    return "unknown" if added is None else f"{added} added, {removed} removed"


def _touched(outcomes: Sequence[sqlite3.Row], out: TextIO) -> None:
    if not outcomes:
        out.write("Components that the agent changed: not recorded\n")
        return
    shown = [o for o in outcomes if o["o_first"] or o["edges_added"]
             or o["edges_removed"]]  # fmt: skip
    names = [o["component"] for o in outcomes if o["o_first"]]
    out.write(f"Components that the agent changed: {', '.join(names) or 'none'}\n")
    for o in shown:
        out.write(f"  {o['component']}: changed at the end {YES_NO[o['o_final']]}, "
                  f"imports of other components {_edges(o)}\n")  # fmt: skip


def _divergences(ledger: Store, task_id: str, out: TextIO) -> None:
    rows = ledger.divergences_of(task_id)
    head = "Imports that the agent added on pairs with a boundary rule:"
    out.write(head + ("\n" if rows else " none\n"))
    for r in rows:
        rule = "allowed" if r["rule_allowed"] else "forbidden"
        out.write(f"  {r['src_component']} -> {r['dst_component']}: the rule is "
                  f"{rule}\n")  # fmt: skip


def _misses(
    forecasts: Sequence[sqlite3.Row],
    outcomes: Sequence[sqlite3.Row],
    bound: Sequence[sqlite3.Row],
    out: TextIO,
) -> None:
    touched = {o["component"]: o["o_first"] for o in outcomes}
    truth = {e["item_id"]: e for e in bound}
    seen: list[tuple[str, float | None, int | None]] = []
    for f in forecasts:
        if f["kind"] == "forecast":
            c = f["component"]
            seen.append((_change(c), f["p"], touched.get(c)))
        else:
            item = truth[f["item_id"]]
            seen.append((_question(item), f["p"], item["truth"]))

    def miss(row: tuple[str, float | None, int | None]) -> tuple[bool, float]:
        _, p, o = row
        if p is None or o is None:
            return (True, 0.0)
        return (False, -((p - o) ** 2))

    out.write("Your answers and the results, the most confident wrong answers first:\n")
    for label, p, o in sorted(seen, key=miss):
        said = "no answer" if p is None else f"{p}"
        out.write(f"  {label} Your answer {said}, result {YES_NO[o]}\n")
