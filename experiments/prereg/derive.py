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
Monte Carlo standard errors at 400 runs).

Usage: uv run python experiments/prereg/derive.py [--selfcheck] [--home DIR] [--now ISO]
[--projects DIR] [--sims N] [--draws N] [--seed N] [--prior-supply TASKS]
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import random
import statistics
import sys
import tempfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from praxis import scoring
from praxis.components import ComponentMap, Rule, assign, product_components
from praxis.store import Evidence, Store, home
from praxis.transcript import TranscriptError, locate, read

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    Rows = Sequence[sqlite3.Row]

# Design choices (docs/preregistration.md 1.4) that the rules start from.
L_DAYS, L_MAX_DAYS, DEADLINE_S, K_E, ITEMS_PER_TASK = 7.0, 28.0, 60.0, 2, 4
ALPHA, POWER = 0.05, 0.80
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
Z_ONE_SIDED, Z_TWO_SIDED, HALF_WIDTH = 1.645, 1.96, 0.10
V1_FLOOR, V3_KAPPA = 0.70, 0.60
BOOT, INTERVAL, N_CAP = 2000, 0.90, 4000


@dataclass(frozen=True)
class Param:
    name: str
    value: object  # None: the rule could not be applied
    n: int  # the pilot rows it read
    note: str


@dataclass(frozen=True)
class Pilot:
    start: dt.datetime
    now: dt.datetime

    def inside(self, ts: str | None) -> bool:
        """In pilot weeks 2 and 3."""
        return ts is not None and 7 <= (when(ts) - self.start) / dt.timedelta(1) < 21

    def week(self, ts: str) -> int:
        return (when(ts) - self.start).days // 7 + 1


@dataclass
class Inputs:
    """The power simulation's inputs; None is not measured."""

    sd_item: float | None = None
    icc: float | None = None
    sd_week: float | None = None
    rate: float | None = None
    answer: float | None = None
    supply: int | None = None
    measured: dict[str, int] = field(default_factory=dict)


def when(ts: str) -> dt.datetime:
    if (stamp := dt.datetime.fromisoformat(ts)).tzinfo is None:
        raise ValueError("a ledger timestamp without a zone is refused")
    return stamp


# -- the pure rules -------------------------------------------------------------------
def nearest_rank(xs: Sequence[float], q: float) -> float:
    ordered = sorted(xs)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def ceil_to(x: float, step: float) -> float:
    return math.ceil(x / step - 1e-9) * step


def wilson_upper(k: int, n: int, z: float = Z_ONE_SIDED) -> float:
    p, z2 = k / n, z * z
    centre = p + z2 / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (centre + half) / (1 + z2 / n)


def otsu(xs: Sequence[float]) -> float | None:
    """The cut between two classes that maximizes between-class variance, at the
    midpoint of the gap it falls in; None with one distinct value."""
    ordered = sorted(xs)
    best: tuple[float, float] | None = None
    for i in range(1, len(ordered)):
        if ordered[i] == ordered[i - 1]:
            continue
        low, high = ordered[:i], ordered[i:]
        between = (
            len(low) * len(high) * (statistics.fmean(low) - statistics.fmean(high)) ** 2
        )
        if best is None or between > best[0]:
            best = (between, (ordered[i - 1] + ordered[i]) / 2)
    return None if best is None else best[1]


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


def icc1(groups: Sequence[Sequence[float]]) -> float | None:
    """One-way random-effects ICC(1) for unequal group sizes (groups of 2 or more)."""
    groups = [g for g in groups if len(g) >= 2]
    g, n = len(groups), sum(len(x) for x in groups)
    if g < 2:
        return None
    grand = math.fsum(map(math.fsum, groups)) / n
    msb = math.fsum(len(x) * (statistics.fmean(x) - grand) ** 2 for x in groups) / (
        g - 1
    )
    msw = math.fsum((v - statistics.fmean(x)) ** 2 for x in groups for v in x) / (n - g)
    n0 = (n - math.fsum(len(x) ** 2 for x in groups) / n) / (g - 1)
    den = msb + (n0 - 1) * msw
    return None if den == 0 else (msb - msw) / den


def fifo_gaps(events: Sequence[tuple[str, str, int, str]]) -> list[tuple[str, float]]:
    """(added ts, seconds to removal) per agent line removed, from (file, hash, delta,
    ts) in time order. Each removed copy pairs with the earliest unpaired added copy of
    the same line in the same file; a removal with no added copy is not an S4 event."""
    queues: dict[tuple[str, str], deque[str]] = defaultdict(deque)
    gaps: list[tuple[str, float]] = []
    for file, h, delta, ts in events:
        q = queues[file, h]
        if delta > 0:
            q.extend([ts] * delta)
            continue
        for _ in range(min(-delta, len(q))):
            added = q.popleft()
            gaps.append((added, (when(ts) - when(added)).total_seconds()))
    return gaps


def half_width_n(sd: float, design_effect: float = 1.0) -> int:
    return math.ceil((Z_TWO_SIDED * sd / HALF_WIDTH) ** 2 * design_effect)


# -- the power simulation -------------------------------------------------------------
def study(
    n: int, delta: float, x: Inputs, rho: float, rng: random.Random
) -> tuple[list[float], list[int]]:
    """One simulated confirmatory study: per-task mean Brier of answered bound items,
    week effects AR(1), a task effect for the ICC, Bernoulli(0.5) arms."""
    sd, icc, sd_w = x.sd_item or 0.0, max(x.icc or 0.0, 0.0), x.sd_week or 0.0
    rate, answer = x.rate or 1.0, 1.0 if x.answer is None else x.answer
    weeks = math.ceil(n / rate)
    effect, effects = rng.gauss(0, sd_w), []
    for _ in range(weeks):
        effects.append(effect)
        effect = rho * effect + math.sqrt(1 - rho * rho) * rng.gauss(0, sd_w)
    values, arms = [], []
    for i in range(n):
        arm = int(rng.random() < 0.5)
        task = effects[int(i // rate)] + rng.gauss(0, math.sqrt(icc) * sd)
        items = [
            task + rng.gauss(0, math.sqrt(1 - icc) * sd) - delta * arm
            for _ in range(K_E)
            if rng.random() < answer
        ]
        if items:
            values.append(statistics.fmean(items))
            arms.append(arm)
    return values, arms


def power(
    n: int, delta: float, x: Inputs, rho: float, sims: int, draws: int, seed: int
) -> float:
    """Share of simulated studies whose randomization test gives p below ALPHA; a study
    with one arm empty cannot be tested and counts as no rejection."""
    rng = random.Random(seed)
    hits = 0
    for _ in range(sims):
        values, arms = study(n, delta, x, rho, rng)
        if 0 < sum(arms) < len(arms):
            p, _ = scoring.randomization_test(values, arms, rng.getrandbits(32), draws)
            hits += p < ALPHA
    return hits / sims


def min_tasks(power_at: Callable[[int], float]) -> int | None:
    lo, hi = 4, 8
    while power_at(hi) < POWER:
        lo, hi = hi, hi * 2
        if hi > N_CAP:
            return None
    while hi - lo > max(2, hi // 50):
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if power_at(mid) >= POWER else (mid, hi)
    return hi


def mde(power_at: Callable[[float], float]) -> float | None:
    lo, hi = 0.0, 3.0
    if power_at(hi) < POWER:
        return None
    while hi - lo > 0.01:
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if power_at(mid) >= POWER else (mid, hi)
    return round(hi, 2)


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
    ok = primary(probes)
    short = scoring.split_half(by_day([r for r in ok if r["lag_days"] < L_DAYS]))
    long = scoring.split_half(by_day([r for r in ok if r["lag_days"] >= L_DAYS]))
    if short is None or long is None:
        return Param(
            "L days", L_DAYS, len(ok), "7 stands: L 2 against 7 not computable"
        )
    value = 2.0 if short >= long + L_MARGIN else L_DAYS
    return Param("L days", value, len(ok), f"split-half {short:.3f} and {long:.3f}")


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


def rule_burn_in(ledger: Store, pilot: Pilot, projects: Path) -> Param:
    """R1 leave-one-out Brier per pilot task against j, the count of earlier closed
    tasks on the same map whose prompt-named flags are known (R1's history)."""
    db, by_j = ledger.db, []
    history: dict[tuple[str, str], list[tuple[str, bool, int]]] = defaultdict(list)
    segments: Counter[tuple[str, str]] = Counter()
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
        scores = [
            (r1(history[key], c, c in said) - o) ** 2 for c, o in outcomes.items()
        ]
        if scores and pilot.inside(t["ts"]):
            by_j.append((segments[key], statistics.fmean(scores)))
        history[key].extend((c, c in said, o) for c, o in outcomes.items())
        segments[key] += 1
    value = burn_in(by_j) if len(by_j) >= MIN_TASKS else None
    return Param("R1 burn-in segments", value, len(by_j), "block rule")


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


def supply(ledger: Store) -> int | None:
    sql = "SELECT repo_id FROM component_maps WHERE frozen_ts IS NOT NULL"
    repos = [r[0] for r in ledger.db.execute(sql + " AND superseded_ts IS NULL")]
    if not repos:
        return None
    total = 0
    for repo in repos:
        frozen = ledger.component_map_frozen(repo)
        version = json.loads(frozen or "{}").get("version")
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


def worst(values: Sequence[float | None]) -> float | None:
    """The largest over the lag-1 grid; unknown if any grid point is unknown."""
    known = [v for v in values if v is not None]
    return max(known) if known and len(known) == len(values) else None


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
        params += [rule_burn_in(ledger, pilot, projects), rule_cap(db, tasks)]
        params += [rule_sessions(db, pilot)]
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
    for line in STAGE2:
        print(f"- {line}")


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
    import subprocess

    from praxis.components import derive as derive_map
    from praxis.components import to_json

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
    plant(tmp / "home", tmp / "repo")
    params = derive(tmp / "home", S0 + dt.timedelta(days=60), tmp, (2, 20, 1))
    got = {p.name: p.value for p in params or []}
    wanted = {"W s": 600.0, "deadline D1 s": 30.0, "L days": 7.0, "L_max days": 14.0}
    wanted |= {"exclusion cap": 0.56, "N sessions per week": 2}
    for name, want in wanted.items():
        expect(name, got.get(name), want)
    x = Inputs(sd_item=0.25, icc=0.0, sd_week=0.0, rate=10.0, answer=1.0)
    pw = power(44, 0.54 * 0.25, x, 0.0, 400, 200, 7)
    expect("power 44 tasks g 0.54 within 0.72 to 0.88", 0.72 <= pw <= 0.88, True)
    print(f"simulated power {pw:.3f}; {'PASS' if not bad else 'FAIL'}")
    return 1 if bad else 0


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
        with tempfile.TemporaryDirectory() as tmp:
            return selfcheck(Path(tmp))
    sim = (args.sims, args.draws, args.seed)
    show(derive(args.home, when(args.now), args.projects, sim))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
