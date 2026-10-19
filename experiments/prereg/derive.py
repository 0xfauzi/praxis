"""Stage 2 parameters from pilot data, by the rules committed in docs/preregistration.md
section 1.7 ("Rules derive.py applies"). Committed before the first pilot row.

Every rule below is a pure function of pilot rows; none reads a result and then picks
its own threshold. A rule that cannot be applied (too few rows, a quantity the ledger
does not hold) prints None with the reason, never a default: the design choice in
docs/preregistration.md 1.4 then stands, or the parameter stays unset and Stage 2 says
so. Pilot weeks 2 and 3 only: [first task + 7 days, first task + 21 days). The ledger is
opened through store.Store and only read; prompts are read from transcripts at run time
for R1's prompt-named flag and discarded.

Acceptance rule for this script, written before its first run (2026-10-19):
`--selfcheck` plants a pilot through store.py into a temporary $PRAXIS_HOME with values
chosen by hand (W 600 s, D1 deadline 30 s, L 7, L_max 14, exclusion cap 0.56, N 2
sessions per week), checks the pure rules on hand-worked inputs, and checks the power
simulation against the normal-theory answer in the case it has one (no ICC, no week
effect, every item answered: 44 tasks give power 0.80 at g = 0.54, one-sided alpha
0.05, Bernoulli arms). Each must hold; the simulated power within 0.72 to 0.88 (four
Monte Carlo standard errors at 400 runs). Added with N_VALIDATION, the exposed-only
lag rule and W_touch (2026-10-19, before any pilot row): the planted pilot gives
N_VALIDATION 0 and W_touch None, and each of the three has a case worked by hand. The
self-check lives in selfcheck.py and the pure statistics in stats.py, beside this file.

Usage: uv run python experiments/prereg/derive.py [--selfcheck] [--home DIR] [--now ISO]
[--projects DIR] [--sims N] [--draws N] [--seed N] [--prior-supply TASKS]
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from stats import (  # experiments/prereg/stats.py, beside this script on sys.path
    Inputs,
    ceil_to,
    fifo_gaps,
    half_width_n,
    icc1,
    mde,
    min_tasks,
    nearest_rank,
    otsu,
    power,
    when,
    wilson_upper,
    worst,
)

from praxis import scoring
from praxis.components import ComponentMap, Rule, assign, product_components
from praxis.store import Store, home
from praxis.transcript import TranscriptError, locate, read

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    Rows = Sequence[sqlite3.Row]

# Design choices (docs/preregistration.md 1.4) that the rules start from.
L_DAYS, L_MAX_DAYS, DEADLINE_S, ITEMS_PER_TASK = 7.0, 28.0, 60.0, 4
G_TARGETS = (0.54, 0.28)
RHO_GRID = (-0.2, 0.0, 0.2)
CONTAMINATION = ("agent_previewed", "late_prediction", "version_change")
# Rule constants, each [proposed, needs owner decision] in the preregistration.
L_MARGIN = 0.10
LMAX_CANDIDATES, MISSED_MAX, MIN_EXPOSURES = (14.0, 21.0, 28.0), 0.10, 20
DEADLINE_Q, DEADLINE_STEP, MIN_ANSWERED = 0.90, 5.0, 30
W_Q, MIN_BOUND = 0.95, 10
WD_Q, MIN_PAIRS = 0.95, 30
MIN_DECISIONS, MIN_TASKS, MIN_PROBES = 10, 10, 30
BLOCK, BURN_TOL = 5, 0.01
SHORT_LAG = 2.0
# N_VALIDATION: a year of validation draws stays under 1 / POOL_SHARE of the D1 pool.
WEEKS_PER_YEAR, POOL_SHARE = 52, 4
# W_touch candidates in weeks; "all" (all history, the Stage 1 rule) first, so a tie
# keeps it, then the longer window.
TOUCH_WINDOWS: tuple[int | str, ...] = ("all", 26, 13, 8, 4)
V1_FLOOR, V3_KAPPA = 0.70, 0.60
BOOT, INTERVAL = 2000, 0.90


@dataclass(frozen=True)
class Param:
    name: str
    value: object  # None: the rule could not be applied
    n: int  # the pilot rows it read
    note: str
    statement: str = ""  # the Stage 2 statement, with its data, when the rule has one


@dataclass(frozen=True)
class Pilot:
    start: dt.datetime
    now: dt.datetime

    def inside(self, ts: str | None) -> bool:
        """In pilot weeks 2 and 3."""
        return ts is not None and 7 <= (when(ts) - self.start) / dt.timedelta(1) < 21

    def week(self, ts: str) -> int:
        return (when(ts) - self.start).days // 7 + 1


# -- the pure rules -------------------------------------------------------------------


def n_validation(pool: int, tasks_per_week: float) -> int:
    """The largest N whose yearly draw, WEEKS_PER_YEAR * N, stays under a quarter of
    the D1 pool; 0 when the pool is smaller than a year of tasks at ITEMS_PER_TASK."""
    if pool < ITEMS_PER_TASK * tasks_per_week * WEEKS_PER_YEAR:
        return 0
    return max(0, (pool - 1) // (POOL_SHARE * WEEKS_PER_YEAR))


def burn_in(by_j: Sequence[tuple[int, float]]) -> int | None:
    """The start of the first block of BLOCK earlier-segment counts whose mean R1
    Brier improves on the next block's by at most BURN_TOL. Blocks with no task are
    skipped; None when no two observed blocks show it."""
    blocks: dict[int, list[float]] = defaultdict(list)
    for j, b in by_j:
        blocks[j // BLOCK].append(b)
    seen = sorted(blocks)
    for a, b in itertools.pairwise(seen):
        if statistics.fmean(blocks[a]) - statistics.fmean(blocks[b]) <= BURN_TOL:
            return a * BLOCK
    return None


# -- the ledger readers ---------------------------------------------------------------
def first_probes(db: sqlite3.Connection, pilot: Pilot) -> list[sqlite3.Row]:
    """The first response to every item exposed in pilot weeks 2 and 3."""
    rows = db.execute(
        "SELECT r.*, e.role, e.ts AS exposed_ts, i.relation FROM probe_responses r"
        " JOIN exposures e USING (item_id) JOIN probe_items i USING (item_id)"
        " WHERE r.id = (SELECT MIN(q.id) FROM probe_responses q"
        " WHERE q.item_id = r.item_id) ORDER BY r.ts, r.id"
    ).fetchall()
    return [r for r in rows if pilot.inside(r["exposed_ts"])]


def primary(rows: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    return [
        r
        for r in rows
        if r["p"] is not None and r["deadline_met"] and not r["truth_changed"]
    ]


def brier(r: sqlite3.Row) -> float:
    return scoring.brier(r["p"], r["truth_at_probe"])


def by_day(rows: Sequence[sqlite3.Row]) -> list[list[float]]:
    days: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        days[when(r["ts"]).astimezone(dt.UTC).date().isoformat()].append(brier(r))
    return [v for _, v in sorted(days.items())]


def rule_lag(probes: Sequence[sqlite3.Row]) -> Param:
    """L = 2 against L = 7 on exposed items only. A validation row has task_id and
    lag_days NULL together by schema (CHECK in probe_responses), so it has no lag to
    compare and is left out by its task_id."""
    ok = [r for r in primary(probes) if r["task_id"] is not None]
    short = by_day([r for r in ok if SHORT_LAG <= r["lag_days"] < L_DAYS])
    long = by_day([r for r in ok if r["lag_days"] >= L_DAYS])
    rel = (scoring.split_half(short), scoring.split_half(long))
    if rel[0] is None or rel[1] is None:
        value, note = L_DAYS, "7 stands: L 2 against 7 not computable"
    else:
        value = SHORT_LAG if rel[0] >= rel[1] + L_MARGIN else L_DAYS
        note = f"split-half {rel[0]:.3f} and {rel[1]:.3f}"
    said = [shown(x) for x in rel]
    statement = (
        f"L is {value:g} days. Split-half reliability (days as groups) of first-probe"
        f" Brier on exposed items: {said[0]} at lags of {SHORT_LAG:g} to {L_DAYS:g}"
        f" days ({len(short)} days), {said[1]} at {L_DAYS:g} days or more"
        f" ({len(long)} days). L is {SHORT_LAG:g} only when the first is at least"
        f" {L_MARGIN} above the second. Validation rows carry no lag by schema, so"
        " they are not compared."
    )
    return Param("L days", value, len(ok), note, statement)


def shown(x: float | None) -> str:
    return "not computable" if x is None else f"{x:.3f}"


def rule_lmax(db: sqlite3.Connection, pilot: Pilot) -> Param:
    exposed = [
        r
        for r in db.execute(
            "SELECT e.item_id, e.ts, (SELECT MIN(lag_days) FROM probe_responses r"
            " WHERE r.item_id = e.item_id) AS lag FROM exposures e"
        )
        if pilot.inside(r[1])
    ]
    for c in LMAX_CANDIDATES:
        due = [r for r in exposed if (pilot.now - when(r[1])).days >= c]
        if len(due) < MIN_EXPOSURES:
            return Param("L_max days", None, len(due), f"too few exposures at {c}")
        missed = sum(r[2] is None or r[2] > c for r in due) / len(due)
        if missed <= MISSED_MAX:
            return Param("L_max days", c, len(due), f"missed share {missed:.3f}")
    return Param("L_max days", None, len(exposed), "infeasible: missed at 28 (V9)")


def rule_deadlines(probes: Sequence[sqlite3.Row]) -> list[Param]:
    out = []
    for rel in ("D1", "D2", "reach"):
        answered = [r for r in probes if r["p"] is not None]
        secs = [r["seconds"] for r in answered if r["relation"] == rel]
        name = f"deadline {rel} s"
        if len(secs) < MIN_ANSWERED:
            out.append(Param(name, DEADLINE_S, len(secs), "60 stands: too few"))
            continue
        q = nearest_rank(secs, DEADLINE_Q)
        out.append(Param(name, ceil_to(q, DEADLINE_STEP), len(secs), f"P90 {q:.1f}"))
    return out


def rule_window(tasks: Sequence[sqlite3.Row]) -> Param:
    gaps = [
        (when(t["bound_ts"]) - when(t["ts"])).total_seconds()
        for t in tasks
        if t["bound_ts"] is not None
    ]
    if len(gaps) < MIN_BOUND:
        return Param("W s", None, len(gaps), f"fewer than {MIN_BOUND} bound tasks")
    q = nearest_rank(gaps, W_Q)
    return Param("W s", ceil_to(q, 60.0), len(gaps), f"P95 {q:.0f} s")


def rule_wd(db: sqlite3.Connection, pilot: Pilot) -> Param:
    sql = "SELECT file, line_hash, delta, ts FROM agent_deltas ORDER BY ts, id"
    events = [(r[0], r[1], r[2], r[3]) for r in db.execute(sql)]
    gaps = [g for added, g in fifo_gaps(events) if pilot.inside(added)]
    if len(gaps) < MIN_PAIRS:
        return Param("W_d s", None, len(gaps), f"fewer than {MIN_PAIRS} removals")
    q = nearest_rank(gaps, WD_Q)
    return Param("W_d s", ceil_to(q, 3600.0), len(gaps), f"P95 {q:.0f} s")


def rule_survival(ledger: Store, pilot: Pilot) -> Param:
    db, fractions = ledger.db, []
    for d in db.execute("SELECT session_id, ts FROM decisions ORDER BY ts, id"):
        if not pilot.inside(d[1]):
            continue
        nxt = db.execute(
            "SELECT prompt_id FROM hook_events WHERE session_id = ? AND event ="
            " 'UserPromptSubmit' AND ts > ? AND prompt_id IS NOT NULL ORDER BY ts, id"
            " LIMIT 1",
            (d[0], d[1]),
        ).fetchone()
        if nxt is None:
            continue
        added: Counter[str] = Counter()
        for h, n in db.execute(
            "SELECT line_hash, delta FROM agent_deltas WHERE session_id = ? AND"
            " prompt_id = ? AND delta > 0",
            (d[0], nxt[0]),
        ):
            added[h] += n
        if added:
            kept = ledger.survival([d[0]], added)
            fractions.append(sum(kept.values()) / sum(added.values()))
    cut = otsu(fractions) if len(fractions) >= MIN_DECISIONS else None
    note = "Otsu cut" if cut is not None else "too few decisions or one value"
    return Param("survival threshold", cut, len(fractions), note)


def rule_cap(db: sqlite3.Connection, tasks: Sequence[sqlite3.Row]) -> Param:
    ids = {t["task_id"] for t in tasks}
    sql = "SELECT task_id FROM flags WHERE code IN (?, ?, ?)"
    flagged = {r[0] for r in db.execute(sql, CONTAMINATION)} & ids
    if len(ids) < MIN_TASKS:
        return Param("exclusion cap", None, len(ids), "too few tasks")
    up = wilson_upper(len(flagged), len(ids))
    return Param("exclusion cap", ceil_to(up, 0.01), len(ids), f"Wilson {up:.4f}")


def rule_sessions(db: sqlite3.Connection, pilot: Pilot) -> Param:
    weeks: dict[int, set[str]] = {2: set(), 3: set()}
    for sid, ts in db.execute(
        "SELECT session_id, ts FROM hook_events WHERE session_id IS NOT NULL"
    ):
        if pilot.inside(ts):
            weeks[pilot.week(ts)].add(sid)
    counts = [len(v) for v in weeks.values()]
    if not any(counts):
        return Param("N sessions per week", None, 0, "no sessions in weeks 2 and 3")
    n = math.floor(0.5 * statistics.fmean(counts))
    return Param("N sessions per week", n, sum(counts), f"weekly {counts}")


# -- R1 burn-in -----------------------------------------------------------------------
def stored_map(db: sqlite3.Connection, repo_id: str, version: str) -> ComponentMap:
    row = db.execute(
        "SELECT source, rules_json, warnings_json FROM component_maps"
        " WHERE repo_id = ? AND version = ?",
        (repo_id, version),
    ).fetchone()
    rules = tuple(Rule(r[0], tuple(r[1:])) for r in json.loads(row[1]))
    return ComponentMap(repo_id, row[0], rules, version, tuple(json.loads(row[2])))


def named(
    projects: Path, task: sqlite3.Row, cmap: ComponentMap
) -> frozenset[str] | None:
    """Product components the bound prompt names, matched whole; None when the
    transcript or the prompt cannot be found. The prompt's tokens are not kept."""
    try:
        path = locate(task["bound_session_id"], projects)
        session = None if path is None else read(path)
    except TranscriptError:
        return None
    want = task["bound_prompt_id"]
    hits = [p for p in session.prompts if p.prompt_id == want] if session else []
    if len(hits) != 1:
        return None
    product = set(product_components(cmap))
    tokens = hits[0].names
    found = {t for t in tokens if t in product}
    found |= {t.rsplit(".", 1)[-1] for t in tokens if "." in t} & product
    found |= {assign(cmap, t) for t in tokens if "/" in t} & product
    return frozenset(found)


@dataclass(frozen=True)
class Segment:
    """A closed task R1 can read: its map, when it was registered, the product
    components its bound prompt named, and o_first per product component."""

    key: tuple[str, str]
    ts: str
    said: frozenset[str]
    outcomes: dict[str, int]


def segments(ledger: Store, projects: Path) -> list[Segment]:
    """Closed tasks by repository and ordinal whose bound prompt is found."""
    db, found = ledger.db, []
    for t in db.execute(
        "SELECT * FROM tasks WHERE status = 'closed' ORDER BY repo_id, ordinal"
    ).fetchall():
        key = (t["repo_id"], t["map_version"])
        cmap = stored_map(db, *key)
        said = named(projects, t, cmap)
        if said is None:
            continue
        product = set(product_components(cmap))
        outcomes = {
            r["component"]: r["o_first"]
            for r in ledger.outcomes_of(t["task_id"])
            if r["component"] in product
        }
        found.append(Segment(key, t["ts"], said, outcomes))
    return found


def r1_briers(
    segs: Sequence[Segment], weeks: int | None
) -> list[tuple[int, float, str]]:
    """(history size, mean R1 Brier, ts) per segment with outcomes. R1's history is
    the earlier segments on the same map at most ``weeks`` before it, all when None:
    the segment itself is never in it."""
    span = None if weeks is None else dt.timedelta(weeks=weeks)
    out = []
    for i, seg in enumerate(segs):
        history = [h for h in segs[:i] if h.key == seg.key and within(h, seg, span)]
        cells = [(c, c in h.said, o) for h in history for c, o in h.outcomes.items()]
        scores = [
            (r1(cells, c, c in seg.said) - o) ** 2 for c, o in seg.outcomes.items()
        ]
        if scores:
            out.append((len(history), statistics.fmean(scores), seg.ts))
    return out


def within(earlier: Segment, seg: Segment, span: dt.timedelta | None) -> bool:
    return span is None or when(seg.ts) - when(earlier.ts) <= span


def rule_burn_in(segs: Sequence[Segment], pilot: Pilot) -> Param:
    """R1 leave-one-out Brier per pilot task against j, the count of earlier closed
    tasks on the same map whose prompt-named flags are known (R1's history)."""
    by_j = [(j, b) for j, b, ts in r1_briers(segs, None) if pilot.inside(ts)]
    value = burn_in(by_j) if len(by_j) >= MIN_TASKS else None
    return Param("R1 burn-in segments", value, len(by_j), "block rule")


def touch_window(
    segs: Sequence[Segment], inside: Callable[[str], bool], minimum: int = MIN_TASKS
) -> tuple[int | str, dict[int | str, float]] | None:
    """The TOUCH_WINDOWS entry whose R1 history gives the smallest mean leave-one-out
    Brier over the segments ``inside`` accepts, with every window's mean; None below
    ``minimum`` such segments. A tie keeps the earlier entry."""
    means: dict[int | str, float] = {}
    for weeks in TOUCH_WINDOWS:
        span = weeks if isinstance(weeks, int) else None
        scores = [b for _, b, ts in r1_briers(segs, span) if inside(ts)]
        if len(scores) < minimum:
            return None
        means[weeks] = statistics.fmean(scores)
    return min(means, key=means.__getitem__), means


def rule_touch_window(segs: Sequence[Segment], pilot: Pilot) -> Param:
    n = sum(pilot.inside(ts) for _, _, ts in r1_briers(segs, None))
    found = touch_window(segs, pilot.inside)
    if found is None:
        why = f"fewer than {MIN_TASKS} pilot tasks whose bound prompt is found"
        statement = f"W_touch stays all history, the Stage 1 rule: {why} ({n})."
        return Param("W_touch weeks", None, n, f"all history stands: {why}", statement)
    best, means = found
    listed = ", ".join(f"{w}: {b:.4f}" for w, b in means.items())
    statement = (
        f"W_touch is {best} weeks. Mean leave-one-out R1 Brier on {n} pilot tasks, by"
        f" trailing window in weeks: {listed}. The smallest wins; a tie keeps all"
        " history, then the longer window. task.TOUCH_WINDOW_WEEKS takes this value,"
        " None for all history."
    )
    return Param("W_touch weeks", best, n, f"smallest of {listed}", statement)


def rule_validation(ledger: Store, pilot: Pilot, tasks: Rows) -> Param:
    """N_VALIDATION: per repository with a frozen map, n_validation over its D1 items
    no task exposed and no probe asked, at its pilot task rate; the smallest wins."""
    sql = "SELECT seconds, ts FROM probe_responses WHERE task_id IS NULL"
    secs = [r[0] for r in ledger.db.execute(sql) if pilot.inside(r[1])]
    median = statistics.median(secs) if secs else None
    per: dict[str, tuple[int, float, int]] = {}
    for repo, version in frozen_maps(ledger):
        items = ledger.probe_items_unprobed(repo, version)
        pool = sum(i["relation"] == "D1" for i in items)
        rate = sum(t["repo_id"] == repo for t in tasks) / 2
        per[repo] = (pool, rate, n_validation(pool, rate))
    cost = "not measured" if median is None else f"{median:.1f} s"
    if not per:
        why = "no repository has a frozen map"
        return Param("N_VALIDATION", None, 0, why, f"N_VALIDATION is unset: {why}.")
    value = min(n for _, _, n in per.values())
    listed = "; ".join(
        f"{r}: pool {pool}, {rate:g} tasks a week, N {n}"
        for r, (pool, rate, n) in sorted(per.items())
    )
    statement = (
        f"N_VALIDATION is {value} a week. Per repository, N is the largest count whose"
        f" {WEEKS_PER_YEAR} weeks of draws stay under a quarter of the unasked D1"
        f" pool, or 0 when that pool is smaller than {WEEKS_PER_YEAR} weeks of tasks at"
        f" {ITEMS_PER_TASK} items each; the smallest N wins ({listed}). Median"
        f" seconds per validation item: {cost} ({len(secs)} answers)."
    )
    return Param("N_VALIDATION", value, len(secs), f"median {cost}", statement)


def r1(history: Sequence[tuple[str, bool, int]], component: str, said: bool) -> float:
    cell = [o for c, s, o in history if c == component and s == said]
    return (sum(cell) + 1) / (len(cell) + 2)  # (k + 1) / (n + 2) over the same s


# -- power inputs ---------------------------------------------------------------------
def power_inputs(ledger: Store, pilot: Pilot, probes: Rows, tasks: Rows) -> Inputs:
    x, ok = Inputs(), primary(probes)
    x.measured["first probes"] = len(ok)
    if len(ok) >= MIN_PROBES:
        x.sd_item = statistics.stdev(map(brier, ok))
    groups: dict[str, list[float]] = defaultdict(list)
    for r in (r for r in ok if r["role"] == "bound"):
        groups[r["task_id"]].append(brier(r))
    paired = [g for g in groups.values() if len(g) >= 2]
    x.measured["tasks with 2 bound answers"] = len(paired)
    x.icc = icc1(paired) if len(paired) >= MIN_TASKS else None
    x.sd_week = week_sd(ok, pilot) if x.sd_item is not None else None
    x.rate = len(tasks) / 2 if tasks else None
    x.answer = answer_rate(probes, ledger.db, pilot)
    x.supply = supply(ledger)
    return x


def week_sd(ok: Rows, pilot: Pilot) -> float | None:
    """Between-week SD of mean first-probe Brier, item noise subtracted, from weeks
    holding at least 5 probes; None below two such weeks."""
    weeks: dict[int, list[float]] = defaultdict(list)
    for r in ok:
        weeks[pilot.week(r["ts"])].append(brier(r))
    full = [v for v in weeks.values() if len(v) >= 5]
    if len(full) < 2:
        return None
    means = [statistics.fmean(v) for v in full]
    noise = statistics.fmean(statistics.pvariance(v) / len(v) for v in full)
    return math.sqrt(max(0.0, statistics.variance(means) - noise))


def answer_rate(
    probes: Sequence[sqlite3.Row], db: sqlite3.Connection, pilot: Pilot
) -> float | None:
    """Bound items that gave a primary observation (answered, deadline met, truth
    unchanged) over bound items resolved: probed, or past L_max unprobed. An item
    still inside its window is censored."""
    answered = {r["item_id"] for r in primary(probes) if r["role"] == "bound"}
    probed = {r["item_id"] for r in probes}
    resolved = 0
    for item, ts in db.execute(
        "SELECT item_id, ts FROM exposures WHERE role = 'bound'"
    ):
        old = (pilot.now - when(ts)).days > L_MAX_DAYS
        resolved += pilot.inside(ts) and (item in probed or old)
    return len(answered) / resolved if resolved >= MIN_TASKS else None


def frozen_maps(ledger: Store) -> list[tuple[str, str]]:
    """(repo_id, version) of every repository's frozen, unsuperseded map."""
    sql = "SELECT repo_id, version FROM component_maps WHERE frozen_ts IS NOT NULL"
    found = ledger.db.execute(sql + " AND superseded_ts IS NULL ORDER BY repo_id")
    return [(r[0], r[1]) for r in found]


def supply(ledger: Store) -> int | None:
    maps = frozen_maps(ledger)
    if not maps:
        return None
    total = 0
    for repo, version in maps:
        total += len(ledger.probe_items_unexposed(repo, version)) // ITEMS_PER_TASK
    return total


def tasks_power(
    n: int, *, delta: float, x: Inputs, rho: float, run: tuple[int, int, int]
) -> float:
    return power(n, delta, x, rho, *run)


def effect_power(
    g: float, *, n: int, sd: float, x: Inputs, rho: float, run: tuple[int, int, int]
) -> float:
    return power(n, g * sd, x, rho, *run)


def simulate(x: Inputs, sims: int, draws: int, seed: int) -> list[Param]:
    run = (sims, draws, seed)
    keys = ("sd_item", "icc", "sd_week", "rate", "answer")
    missing = [k for k in keys if getattr(x, k) is None]
    if missing or x.sd_item is None:
        return [Param("power simulation", None, 0, f"not measured: {missing}")]
    sd, out = x.sd_item, []
    for g in G_TARGETS:
        part = partial(tasks_power, delta=g * sd, x=x, run=run)
        need = [min_tasks(partial(part, rho=rho)) for rho in RHO_GRID]
        out.append(Param(f"tasks for g = {g}", worst(need), sims, f"per lag {need}"))
    n = x.supply
    if n is None or n < 8:
        return [*out, Param("MDE g at supply", None, n or 0, "supply below 8")]
    part = partial(effect_power, n=n, sd=sd, x=x, run=run)
    found = [mde(partial(part, rho=rho)) for rho in RHO_GRID]
    return [*out, Param("MDE g at supply", worst(found), n, f"per lag {found}")]


STAGE2 = (
    "Q1 test: scoring.randomization_test on per-task mean Brier, one-sided, re-drawing"
    " allocations with the treated count fixed at its observed value. Arms are drawn"
    " Bernoulli(0.5) per task; given the count every allocation of that size is equally"
    " likely under that rule, so the conditional test is exact and valid"
    " unconditionally. Exact enumeration at C(n, k) <= 70, else Monte Carlo.",
    "General effect: scoring.koehler_levin, each repository's start week drawn"
    " independently and uniformly from its committed candidate set (Marascuilo-Busk);"
    " statistic mean over repositories of (mean BS before minus mean BS after); weeks"
    " without P1 data are excluded from a phase mean; an assignment that empties a"
    " phase is dropped from numerator and denominator.",
    f"V5 and V8 intervals: {INTERVAL:.0%} two-sided percentile intervals by cluster"
    f" bootstrap over tasks, B = {BOOT}, seed committed here; excluding zero is the"
    " one-sided test at alpha 0.05.",
    f"V1 floor: split-half reliability (days as groups) at least {V1_FLOOR}. V3:"
    f" Cohen's kappa at least {V3_KAPPA}. V5 congruent accuracy is high when the lower"
    " end of the congruent BSS-against-R_base interval is above zero.",
)


def derive(
    home: Path, now: dt.datetime, projects: Path, sim: tuple[int, int, int]
) -> list[Param] | None:
    path = home / "ledger.db"
    if not path.is_file():
        return None
    ledger = Store(path).open()
    try:
        db = ledger.db
        first = db.execute("SELECT MIN(ts) FROM tasks").fetchone()[0]
        if first is None:
            return [Param("pilot", None, 0, "no task row: nothing to derive")]
        pilot = Pilot(when(first), now)
        every = db.execute("SELECT * FROM tasks ORDER BY ordinal").fetchall()
        tasks = [t for t in every if pilot.inside(t["ts"])]
        probes = first_probes(db, pilot)
        params = [rule_lag(probes), rule_lmax(db, pilot), *rule_deadlines(probes)]
        params += [rule_window(tasks), rule_wd(db, pilot), rule_survival(ledger, pilot)]
        segs = segments(ledger, projects)
        params += [rule_burn_in(segs, pilot), rule_touch_window(segs, pilot)]
        params += [rule_cap(db, tasks), rule_sessions(db, pilot)]
        params += [rule_validation(ledger, pilot, tasks)]
        params += minimums(ledger, probes, pilot)
        x = power_inputs(ledger, pilot, probes, tasks)
        params += [Param(f"input {k}", v, 0, "measured") for k, v in vars(x).items()]
        params += simulate(x, *sim)
        return params
    finally:
        ledger.close()


def minimums(ledger: Store, probes: Rows, pilot: Pilot) -> list[Param]:
    ok = primary(probes)
    n_week = half_width_n(statistics.stdev(map(brier, ok))) if len(ok) >= 30 else None
    week = Param("min probes per week", n_week, len(ok), "half-width 0.10")
    sql = (
        "SELECT r.recalled_index, d.chosen_index, r.ts FROM decision_responses r JOIN"
        " decisions d ON d.id = r.decision_id WHERE r.recalled_index IS NOT NULL"
        " AND d.chosen_index IS NOT NULL"
    )
    hits = [r[0] == r[1] for r in ledger.db.execute(sql) if pilot.inside(r[2])]
    p = statistics.fmean(hits) if len(hits) >= MIN_DECISIONS else None
    n_s3 = None if p is None else half_width_n(math.sqrt(p * (1 - p)))
    return [week, Param("S3 minimum decisions", n_s3, len(hits), "None: counts only")]


def show(params: list[Param] | None) -> None:
    if params is None:
        print("no ledger at this $PRAXIS_HOME: nothing to derive")
        return
    for p in params:
        print(f"{p.name}: {p.value}  (n {p.n}; {p.note})")
    print("Stage 2 statements:")
    for line in [*STAGE2, *(p.statement for p in params if p.statement)]:
        print(f"- {line}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=home())
    ap.add_argument("--now", default=dt.datetime.now(dt.UTC).isoformat())
    ap.add_argument("--projects", type=Path, default=Path.home() / ".claude/projects")
    ap.add_argument("--sims", type=int, default=200)
    ap.add_argument("--draws", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261019)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--prior-supply", type=int, help="design 6.6 priors, not Stage 2")
    args = ap.parse_args(argv)
    if args.prior_supply is not None:
        # Design 6.6: per-item Brier SD at most sqrt(5/64) with item variation; no ICC,
        # no week effect and every item answered are the case normal theory solves.
        x = Inputs(math.sqrt(5 / 64), 0.0, 0.0, 1.0, 1.0, args.prior_supply)
        prior = Param("prior illustration", vars(x), 0, "not pilot data")
        show([prior, *simulate(x, args.sims, args.draws, args.seed)])
        return 0
    if args.selfcheck:
        from selfcheck import selfcheck  # beside this script; it imports this module

        with tempfile.TemporaryDirectory() as tmp:
            return selfcheck(Path(tmp))
    sim = (args.sims, args.draws, args.seed)
    show(derive(args.home, when(args.now), args.projects, sim))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
