"""`praxis task` and `praxis last`: registration, the pre-task binding rule, the arm,
forecast entry, and the feedback after a scored segment (design 03 4.4 and 6.1-6.3).

The arm has its own generator, keyed by the committed seed, the repository id and the
task ordinal alone (``seed_hash``, sha256 of ``json.dumps([seed, repo_id, ordinal])``;
treatment when its first ``random()`` is below 0.5), so neither the items drawn nor the
owner's answers can move it. Items and the forecast sample come from a second generator
keyed by the same three and ``"bind"``. Nothing the owner types is kept: an answer
outside the closed vocabulary is asked again and dropped, not even its length.
Feedback names bound items only; a related or drawn-not-shown item is never shown.
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
from praxis.probe import GRID_KEYS, QUESTION, SKIP
from praxis.store import Evidence, Store
from praxis.unassisted import Repo, locate

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

# Design choices fixed in docs/preregistration.md 1.4; Stage 2 may restate them.
H, K_E, K_R, K_P, R = 3, 2, 1, 1, 5
P_TREATMENT = 0.5
# The committed seed `praxis task` draws arms with: derive.py's default, cited in
# preregistration 1.7 (Rules derive.py applies); Stage 2 restates it (1.2).
SEED = 20261019
REDUCER_VERSION = "task-v1"
ASSUMPTIONS = (
    "typed 1 3 5 7 9 is read as 0.1 0.3 0.5 0.7 0.9; s or end of input is unanswered",
    "ts is the clock after the last answer, not the registration time",
    "the components asked are a seeded uniform sample of the frozen map's product",
)
YES_NO = {0: "no", 1: "yes", None: "unknown"}


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
        stdout.write("praxis task: not inside a git repository\n")
        return 2
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        return _register(ledger, repo, (stdin, stdout), now, seed)
    finally:
        ledger.close()


def last(repo_root: Path, praxis_home: Path, stdout: TextIO) -> int:
    repo = locate(repo_root)
    if repo is None:
        stdout.write("praxis last: not inside a git repository\n")
        return 2
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        _feedback(ledger, repo, stdout)
    finally:
        ledger.close()
    return 0


def _key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()


def _register(
    ledger: Store, repo: Repo, io: tuple[TextIO, TextIO], now: str, seed: int
) -> int:
    stdin, out = io
    repo_id = repo.repo_id
    frozen = ledger.component_map_frozen(repo_id)
    if frozen is None:
        out.write("praxis task: no frozen component map; run `praxis components "
                  "freeze` first\n")  # fmt: skip
        return 2
    cmap = from_json(frozen)
    product = product_components(cmap)
    # Design 6.3: the last segment's feedback opens the next task, in both arms.
    _feedback(ledger, repo, out)
    ordinal = ledger.task_count(repo_id) + 1
    seed_hash = _key(seed, repo_id, ordinal)
    rng = Random(int(_key(seed, repo_id, ordinal, "bind"), 16))
    draw = _bind(ledger, repo_id, cmap.version, product, rng)
    treated = Random(int(seed_hash, 16)).random() < P_TREATMENT
    task_id = _key(repo_id, ordinal)[:16]
    old = ledger.task_register(
        region=draw.region, bound=draw.bound, related=draw.related, drawn=draw.drawn,
        task_id=task_id, repo_id=repo_id, map_version=cmap.version, ordinal=ordinal,
        ts=now, arm="treatment" if treated else "control", seed_hash=seed_hash,
        stratum=draw.stratum,
    )  # fmt: skip
    if old is not None:
        out.write("the open task was superseded by this one\n")
    if treated:
        sample = rng.sample(sorted(product), min(R, len(product)))
        _elicit(ledger, task_id, sample, draw.bound, (stdin, out))
    out.write("registered\n")
    return 0


def _region(
    ledger: Store, repo_id: str, version: str, product: Sequence[str], rng: Random
) -> list[str]:
    """The h product components with the highest touch rate over closed tasks on this
    map. A component with no outcome row in a task is unknown there, not untouched."""
    # The trailing window is unset in design 6.2 and preregistration 1.4 (searched
    # "trailing"): until Stage 2 fixes one, the rate runs over every closed task.
    touched: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    for task in ledger.task_rows(repo_id):
        if task["status"] != "closed" or task["map_version"] != version:
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
    ledger: Store, repo_id: str, version: str, product: Sequence[str], rng: Random
) -> Draw:
    region = _region(ledger, repo_id, version, product, rng)
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
        out.write(f"{prompt} [1 3 5 7 9, s skips]: ")
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
        out.write("  answer 1 3 5 7 9 or s\n")


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
    out.write("Before you write the prompt: how likely is it that the agent changes\n")
    rows: list[tuple[str | None, float | None, str, str | None]] = [
        (c, _ask(stdin, out, f"  {c}"), "forecast", None) for c in sample
    ]
    items = [ledger.probe_item(i) for i in bound]
    if items:
        out.write("And how likely is each of these true now?\n")
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
        out.write("no closed task yet\n")
        return
    task_id = task["task_id"]
    out.write(f"last task {task['ordinal']} ({task['arm']}), closed "
              f"{task['closed_ts']}\n")  # fmt: skip
    outcomes = ledger.outcomes_of(task_id)
    _touched(outcomes, out)
    _divergences(ledger, task_id, out)
    bound = [e for e in ledger.exposures_of(task_id) if e["role"] == "bound"]
    now = _truth_now(ledger, repo, task["map_version"], bound) if bound else {}
    out.write("bound items, true when drawn and now:\n" if bound else
              "bound items: none\n")  # fmt: skip
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
    return "edges unknown" if added is None else f"edges +{added} -{removed}"


def _touched(outcomes: Sequence[sqlite3.Row], out: TextIO) -> None:
    if not outcomes:
        out.write("components touched: not recorded\n")
        return
    shown = [o for o in outcomes if o["o_first"] or o["edges_added"]
             or o["edges_removed"]]  # fmt: skip
    names = [o["component"] for o in outcomes if o["o_first"]]
    out.write(f"components touched: {', '.join(names) or 'none'}\n")
    for o in shown:
        out.write(f"  {o['component']}: changed at the end "
                  f"{YES_NO[o['o_final']]}, {_edges(o)}\n")  # fmt: skip


def _divergences(ledger: Store, task_id: str, out: TextIO) -> None:
    rows = ledger.divergences_of(task_id)
    out.write("boundary divergences:" + ("\n" if rows else " none\n"))
    for r in rows:
        rule = "allowed" if r["rule_allowed"] else "forbidden"
        out.write(f"  {r['src_component']} -> {r['dst_component']} ({rule} by the "
                  "boundary rules)\n")  # fmt: skip


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
            seen.append((f"agent changes {c}", f["p"], touched.get(c)))
        else:
            item = truth[f["item_id"]]
            seen.append((_question(item), f["p"], item["truth"]))

    def miss(row: tuple[str, float | None, int | None]) -> tuple[bool, float]:
        _, p, o = row
        if p is None or o is None:
            return (True, 0.0)
        return (False, -((p - o) ** 2))

    out.write("your answers against the outcome, high-confidence misses first:\n")
    for label, p, o in sorted(seen, key=miss):
        said = "unanswered" if p is None else f"{p}"
        out.write(f"  you said {said}, outcome {YES_NO[o]}: {label}\n")
