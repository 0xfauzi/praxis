"""`praxis report`: references, scores and the randomization tests of design 03
sections 4.1 (P1), 4.4 (F1) and 6.3, computed at analysis time from the ledger.

Nothing here writes: every number is recomputed from rows store.py holds, and carries
its Evidence fields in memory. A reference is fitted only on data dated strictly before
the forecast it is compared with, and one that cannot be fitted is NULL, never another
reference. A block below its minimum is refused with a reason code, not scored. The
prompt text R1 needs is read from the transcript, intersected with component ids and
dropped. The rendered text refuses the words cause, impact and would unless a
pre-registered rule says met, and then names the rule (AGENTS.md invariant 3); no
component id, path or prompt reaches the text, only counts, numbers and reason codes.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from functools import partial
from graphlib import CycleError, TopologicalSorter
from itertools import pairwise, permutations
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from praxis import components, scoring, transcript
from praxis.components import ComponentMap, assign, from_json, product_components
from praxis.probe import L_DAYS, L_MAX_DAYS
from praxis.store import Evidence, Store

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

REDUCER_VERSION = "report-v1"
ALPHA = 0.05  # docs/preregistration.md 1.4, one-sided
# The decomposition's floor; the Stage 2 minimum from the power simulation replaces it.
MIN_FORECASTS = 2
# Research answers 2026-10-19: a 0.10 miscalibration to a 95 percent half-width.
BIN_MIN = {0.1: 35, 0.3: 81, 0.5: 97, 0.7: 81, 0.9: 35}
BAND_DRAWS, BAND_SEED, BAND_LEVEL = 1000, 0, 0.9
# Monte Carlo draws per randomization test past exact enumeration: the committed draw
# count of preregistration 2.4; Stage 2 restates it.
DRAWS = 2000
F1_EXCLUDING = ("agent_previewed", "late_prediction", "version_change")
ARMS = ("treatment", "control")
ROLES = ("bound", "related", "drawn_not_shown")
P1_REFS = ("R_base", "R_layer", "R_fam")
F1_REFS = ("R1", "R0", "R_last")
CONTRASTS = {"specific": "bound", "near": "related", "untargeted": "drawn_not_shown"}
TESTS = (*CONTRASTS, "pipeline_placebo", "f1_shuffle")
CLAIMS = {"brier": "derived", "decomposition": "derived", "bss": "comparative",
          "arm contrast": "comparative"}  # fmt: skip
PR1_CLAIM = "the instrument changed delayed structural accuracy on bound items"
_REFUSED = re.compile(r"\b(caus\w*|impact\w*|would)\b", re.IGNORECASE)
_PAIR_TIE = 1e-12

P1_ASSUMPTIONS = (
    "first probe of an item only; skipped, slow, truth-changed, out-of-window, "
    "unexposed answers and tasks not closed are excluded and counted by arm",
    f"the probe window is {L_DAYS:g} to {L_MAX_DAYS:g} days after exposure",
    "R_base and R_layer read the D1 graph strictly before the probe: per pair the "
    "latest X1 read (pool or probe truth) unless a later edge-history scan shows a "
    "file importing the target; a scan covers only changed files, so it never "
    "removes an edge",
    "R_layer is NULL when the earlier graph has a cycle (no topological order)",
    "R_fam is PAV of earlier first-probe truths on co-touch share over all earlier "
    "closed tasks (the window needs measuring); NULL until both truths occurred",
    "D2 and runtime items have no reference yet; summed Brier is blind to "
    "dependence between the items of one task",
)
F1_ASSUMPTIONS = (
    "closed treatment tasks only; the outcome is o_first, o_final is not scored yet",
    "unanswered, flagged, no-outcome and empty-segment forecasts are excluded and "
    "counted; the exclusion cap needs measuring (derive.py)",
    "R1 is (k + 1) / (n + 2) over earlier closed tasks with the same prompt-named "
    "state (a component id as a whole word or a path the map assigns to it); an "
    "unreadable prompt is unknown, never unnamed; the burn-in needs measuring",
    "R0 is the earlier touch share; R_last is 0.9 if the previous closed task "
    "touched the component, else 0.1",
)
SCORE_ASSUMPTIONS = (
    f"a block needs {MIN_FORECASTS} forecasts, the decomposition's floor; the Stage 2 "
    "minimum from the power simulation needs measuring and replaces it",
    "REL, RES, UNC bias-corrected after Ferro and Fricker (2012) as Siegert (2013) "
    "writes them; REL and RES may be negative",
    f"CORP consistency bands: {BAND_DRAWS} bootstrap draws of whole tasks (seed "
    f"{BAND_SEED}, level {BAND_LEVEL:g}), outcomes redrawn as Bernoulli(p) and PAV "
    "refitted as reliabilitydiag does (default 100 draws of single forecasts); "
    "tasks, because forecasts within one are not independent; no bound correction",
    "a CORP bin is read at 35 forecasts (p 0.1, 0.9), 81 (0.3, 0.7) or 97 (0.5) if "
    "independent: divide by 1 + (m - 1) ICC once the ICC is measured",
)
TEST_ASSUMPTIONS = (
    "arm contrasts: control minus treatment, per-task mean Brier as the unit, "
    "one-sided, treated task count fixed (C(n, k), exact at 70 or fewer); Stage 2 "
    "names the rule re-drawn",
    "the pipeline placebo needs probes of never-drawn items, which no probe asks",
    "f1_shuffle: share of task pairings of forecasts whose BSS against R1 reaches "
    "the true pairing's",
)

Pair = tuple[str, str]
Refs = dict[str, float | None]
Count = tuple[str, str, str]  # (measure, reason, arm)


@dataclass(frozen=True)
class Bin:
    p: float
    rate: float
    n: int
    band: tuple[float, float] | None
    interpretable: bool


@dataclass(frozen=True)
class Block:
    measure: str
    arm: str
    role: str
    n: int
    tasks: int
    refused: str | None
    brier: float | None
    skill: Mapping[str, tuple[float | None, int]]  # reference -> (BSS, forecasts)
    decomposition: tuple[float, float, float] | None
    bins: tuple[Bin, ...]
    ev: Evidence


@dataclass(frozen=True)
class Rule:
    name: str
    met: bool
    reasons: tuple[str, ...]
    claim: str


@dataclass(frozen=True)
class Scores:
    repo_id: str
    map_version: str | None
    refused: str | None
    blocks: tuple[Block, ...]
    counts: Mapping[Count, int]
    weeks: Mapping[str, int]
    tests: Mapping[str, tuple[float, int] | None]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class _Scored:
    p: float
    o: int
    task: str
    arm: str
    role: str
    component: str | None
    refs: Refs
    source: str
    coverage: str
    warnings: tuple[str, ...]


def _t(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)  # a naive one raises when compared


@dataclass(frozen=True)
class _Graph:
    """The D1 graph as the ledger knew it at a time, from full X1 reads and scans."""

    pairs: frozenset[Pair]
    reads: tuple[tuple[dt.datetime, Pair, bool], ...]
    scans: tuple[tuple[dt.datetime, str, str, str | None], ...]

    def at(self, t: dt.datetime) -> dict[Pair, bool]:
        known: dict[Pair, tuple[dt.datetime, bool]] = {}
        for when, pair, truth in self.reads:
            if when >= t:
                break
            known[pair] = (when, truth)
        for when, pair in self._imports(t):
            if pair in self.pairs and (pair not in known or when > known[pair][0]):
                known[pair] = (when, True)
        return {pair: truth for pair, (_, truth) in known.items()}

    def _imports(self, t: dt.datetime) -> list[tuple[dt.datetime, Pair]]:
        latest: dict[str, tuple[dt.datetime, str, list[str]]] = {}
        for when, file, component, dst in self.scans:
            if when >= t:
                break
            if file not in latest or latest[file][0] != when:
                latest[file] = (when, component, [])
            if dst is not None:
                latest[file][2].append(dst)
        return [(w, (c, d)) for w, c, dsts in latest.values() for d in dsts]


@dataclass(frozen=True)
class _View:
    store: Store
    repo_id: str
    cmap: ComponentMap
    tasks: Mapping[str, sqlite3.Row]  # every task of the repository
    closed: tuple[sqlite3.Row, ...]  # closed tasks on the map, by closed_ts
    touched: Mapping[str, Mapping[str, int]]  # closed task -> component -> o_first
    flagged: frozenset[str]
    first: tuple[sqlite3.Row, ...]
    repeats: Counter[str]  # arm -> responses after an item's first
    graph: _Graph
    names: _Names


def _view(store: Store, repo_id: str) -> _View | tuple[str, str | None]:
    frozen = store.component_map_frozen(repo_id)
    if frozen is None:
        return ("no_frozen_map", None)
    cmap = from_json(frozen)
    if len(product_components(cmap)) < components.MIN_COMPONENTS:
        return ("too_few_components", cmap.version)
    tasks = {r["task_id"]: r for r in store.task_rows(repo_id)}
    closed = sorted(
        (r for r in tasks.values()
         if r["status"] == "closed" and r["map_version"] == cmap.version),
        key=lambda r: (_t(r["closed_ts"]), r["ordinal"]),
    )  # fmt: skip
    touched = {
        r["task_id"]: {
            o["component"]: o["o_first"] for o in store.outcomes_of(r["task_id"])
        }
        for r in closed
    }
    flagged = store.tasks_flagged(F1_EXCLUDING)
    rows = store.probe_responses_of(repo_id, cmap.version)
    first = {r["item_id"]: r for r in reversed(rows)}  # the earliest per item wins
    repeats = Counter(_arm(r, tasks) for r in rows if first[r["item_id"]] is not r)
    ordered = tuple(r for r in rows if first[r["item_id"]] is r)
    return _View(store, repo_id, cmap, tasks, tuple(closed), touched, flagged,
                 ordered, repeats, _graph(store, cmap, rows),
                 _Names(cmap, Path.home() / ".claude" / "projects"))  # fmt: skip


def _graph(store: Store, cmap: ComponentMap, rows: Sequence[sqlite3.Row]) -> _Graph:
    product = product_components(cmap)
    pairs = frozenset(permutations(product, 2))
    items = store.probe_items_d1(cmap.repo_id, cmap.version)
    reads = [(_t(i["truth_ts"]), (i[0], i[1]), bool(i["truth"])) for i in items]
    reads += [
        (_t(r["ts"]), (r["src_component"], r["dst_component"]),
         bool(r["truth_at_probe"]))
        for r in rows
        if r["relation"] == "D1"
    ]  # fmt: skip
    scans = [
        (_t(e["ts"]), e["src_file"], e["src_component"], e["dst_component"])
        for e in store.edge_history(cmap.repo_id)
        if e["map_version"] == cmap.version
    ]
    known = [r for r in reads if r[1] in pairs]
    return _Graph(pairs, tuple(sorted(known, key=lambda r: r[0])), tuple(scans))


def _arm(row: sqlite3.Row, tasks: Mapping[str, sqlite3.Row]) -> str:
    exposed = row["exposed_task"]
    return "unexposed" if exposed is None else str(tasks[exposed]["arm"])


# -- references ---------------------------------------------------------------------


def _layer(graph: Mapping[Pair, bool], pair: Pair) -> float | None:
    """Forward pairs (the importer after its target in a topological order of the
    earlier graph) get its forward edge rate on the grid; backward pairs 0.1."""
    nodes = sorted({n for pair in graph for n in pair})
    deps = {n: sorted(b for (a, b), e in graph.items() if a == n and e) for n in nodes}
    try:
        pos = {n: i for i, n in enumerate(TopologicalSorter(deps).static_order())}
    except CycleError:
        return None
    a, b = pair
    forward = [edge for (x, y), edge in graph.items() if pos[y] < pos[x]]
    if not forward or a not in pos or b not in pos:
        return None
    return scoring.snap(sum(forward) / len(forward)) if pos[b] < pos[a] else 0.1


def _cotouch(view: _View, pair: Pair, t: dt.datetime) -> float | None:
    """Share of earlier closed tasks touching both components; a task with no outcome
    row for either is unknown there and left out."""
    rows = [view.touched[r["task_id"]] for r in view.closed if _t(r["closed_ts"]) < t]
    both = [o for o in rows if pair[0] in o and pair[1] in o]
    if not both:
        return None
    return sum(1 for o in both if o[pair[0]] and o[pair[1]]) / len(both)


def _fam(prefix: Sequence[tuple[float, int]], x: float | None) -> float | None:
    if x is None or len({o for _, o in prefix}) < 2:
        return None
    fit = scoring.corp_reliability([a for a, _ in prefix], [o for _, o in prefix])
    below = [rate for value, rate, _ in fit if value <= x]
    return below[-1] if below else fit[0][1]


def _p1_refs(view: _View) -> dict[int, Refs]:
    """References per first D1 response id, each fitted strictly before it."""
    refs: dict[int, Refs] = {}
    history: list[tuple[dt.datetime, float, int]] = []
    for r in view.first:
        if r["relation"] != "D1":
            continue
        t, pair = _t(r["ts"]), (r["src_component"], r["dst_component"])
        graph = view.graph.at(t)
        x = _cotouch(view, pair, t)
        prefix = [(a, o) for when, a, o in history if when < t]
        refs[r["id"]] = {
            "R_base": sum(graph.values()) / len(graph) if graph else None,
            "R_layer": _layer(graph, pair),
            "R_fam": _fam(prefix, x),
        }
        if x is not None:
            history.append((t, x, int(r["truth_at_probe"])))
    return refs


class _Names:
    """The product components each task's bound prompt names, read from the
    transcript and dropped; None when the prompt cannot be read."""

    def __init__(self, cmap: ComponentMap, projects: Path) -> None:
        self.cmap = cmap
        self.projects = projects
        self.product = product_components(cmap)
        self.cache: dict[str, frozenset[str] | None] = {}

    def of(self, task: sqlite3.Row) -> frozenset[str] | None:
        if task["task_id"] not in self.cache:
            self.cache[task["task_id"]] = self._read(task)
        return self.cache[task["task_id"]]

    def _read(self, task: sqlite3.Row) -> frozenset[str] | None:
        sid, pid = task["bound_session_id"], task["bound_prompt_id"]
        if sid is None or pid is None:
            return None
        try:
            path = transcript.locate(sid, self.projects)
            session = None if path is None else transcript.read(path)
        except transcript.TranscriptError:
            return None
        found = [] if session is None else [
            p for p in session.prompts if p.prompt_id == pid
        ]  # fmt: skip
        if len(found) != 1:
            return None
        names = found[0].names
        paths = [n for n in names if "/" in n]
        return frozenset(
            c for c in self.product
            if c in names or any(assign(self.cmap, n) == c for n in paths)
        )  # fmt: skip


def _earlier(view: _View, task: sqlite3.Row) -> list[sqlite3.Row]:
    start = _t(task["ts"])
    return [u for u in view.closed if _t(u["closed_ts"]) < start]


def _r1(view: _View, task: sqlite3.Row, c: str) -> float | None:
    named = view.names.of(task)
    if named is None:
        return None
    n = k = 0
    for u in _earlier(view, task):
        theirs, outcome = view.names.of(u), view.touched[u["task_id"]]
        if theirs is None or c not in outcome or (c in theirs) != (c in named):
            continue
        n, k = n + 1, k + outcome[c]
    return (k + 1) / (n + 2)


def _f1_refs(view: _View, task: sqlite3.Row, c: str) -> Refs:
    rows = [view.touched[u["task_id"]] for u in _earlier(view, task)]
    seen = [o[c] for o in rows if c in o]
    last = rows[-1] if rows else {}
    return {
        "R1": _r1(view, task, c),
        "R0": sum(seen) / len(seen) if seen else None,
        "R_last": (0.9 if last[c] else 0.1) if c in last else None,
    }


def references(store: Store, repo_id: str) -> dict[str, dict[str, float | None]]:
    """Per first D1 probe, keyed by item id: R_base, R_layer, R_fam. Per F1 forecast
    of a closed treatment task, keyed `task_id:component`: R1, R0, R_last. Empty
    without a frozen map of at least five product components."""
    view = _view(store, repo_id)
    if isinstance(view, tuple):
        return {}
    refs = _p1_refs(view)
    out = {str(r["item_id"]): refs[r["id"]] for r in view.first if r["id"] in refs}
    for task, f in _forecasts(view):
        key = f"{task['task_id']}:{f['component']}"
        out[key] = _f1_refs(view, task, f["component"])
    return out


def _forecasts(view: _View) -> Iterator[tuple[sqlite3.Row, sqlite3.Row]]:
    """The F1 forecast rows of closed treatment tasks, shown and unanswered alike."""
    for task in view.closed:
        if task["arm"] == "treatment":
            for f in view.store.forecasts_of(task["task_id"]):
                if f["kind"] == "forecast":
                    yield task, f


# -- scoring ------------------------------------------------------------------------


def _p1_reason(view: _View, r: sqlite3.Row) -> str | None:
    exposed = r["exposed_task"]
    lag = r["lag_days"]
    checks = (
        ("not_exposed", exposed is None or r["task_id"] != exposed),
        ("task_not_closed", exposed is not None
         and view.tasks[exposed]["status"] != "closed"),
        ("other_relation", r["relation"] != "D1"),
        ("outside_window", lag is None or not L_DAYS <= lag <= L_MAX_DAYS),
        ("skipped", r["p"] is None),
        ("slow", not r["deadline_met"]),
        ("truth_changed", bool(r["truth_changed"])),
    )  # fmt: skip
    return next((code for code, hit in checks if hit), None)


def _p1(view: _View) -> tuple[list[_Scored], Counter[Count]]:
    refs = _p1_refs(view)
    scored: list[_Scored] = []
    counts: Counter[Count] = Counter(
        {("P1", "repeat_probe", arm): n for arm, n in view.repeats.items()}
    )
    for r in view.first:
        reason, arm = _p1_reason(view, r), _arm(r, view.tasks)
        if reason is not None:
            counts["P1", reason, arm] += 1
            continue
        scored.append(_Scored(
            float(r["p"]), int(r["truth_at_probe"]), r["task_id"], arm, r["role"],
            None, refs[r["id"]], f"probe_responses:{r['id']}", r["coverage"],
            tuple(json.loads(r["warnings"])),
        ))  # fmt: skip
    return scored, counts


def _f1_reason(view: _View, task: sqlite3.Row, f: sqlite3.Row) -> str | None:
    outcome = view.touched[task["task_id"]]
    checks = (
        ("flagged", task["task_id"] in view.flagged),
        ("unanswered", f["p"] is None),
        ("no_outcome", f["component"] not in outcome),
        ("empty_segment", not any(outcome.values())),
    )
    return next((code for code, hit in checks if hit), None)


def _f1(view: _View) -> tuple[list[_Scored], Counter[Count]]:
    scored: list[_Scored] = []
    counts: Counter[Count] = Counter()
    for task, f in _forecasts(view):
        reason = _f1_reason(view, task, f)
        if reason is not None:
            counts["F1", reason, "treatment"] += 1
            continue
        tid, c = task["task_id"], f["component"]
        scored.append(_Scored(
            float(f["p"]), int(view.touched[tid][c]), tid, "treatment", "forecast", c,
            _f1_refs(view, task, c), f"forecasts:{f['id']}", "full", (),
        ))  # fmt: skip
    return scored, counts


def _skill(rows: Sequence[_Scored], ref: str) -> tuple[float | None, int]:
    fitted = [(s.p, s.o, s.refs[ref]) for s in rows if s.refs[ref] is not None]
    if not fitted:
        return None, 0
    owner = math.fsum((p - o) ** 2 for p, o, _ in fitted)
    against = math.fsum((r - o) ** 2 for _, o, r in fitted if r is not None)
    return scoring.bss(owner, against), len(fitted)


def _at(fit: Sequence[tuple[float, float, int]], v: float) -> float | None:
    """The PAV line at v, linear between fitted values; None outside them."""
    for (x0, y0, _), (x1, y1, _) in pairwise(fit):
        if x0 <= v <= x1:
            return y0 + (v - x0) / (x1 - x0) * (y1 - y0)
    return fit[0][1] if len(fit) == 1 and fit[0][0] == v else None


def _bands(rows: Sequence[_Scored]) -> dict[float, tuple[float, float] | None]:
    clusters: dict[str, list[float]] = {}
    for s in rows:
        clusters.setdefault(s.task, []).append(s.p)
    drawn = _resampled(clusters, sorted({s.p for s in rows}))
    # "inclusive" is R's type 7, reliabilitydiag's quantile; n cut points at 1/n steps.
    cuts = round(2 / (1 - BAND_LEVEL))
    edges = {v: statistics.quantiles(d, n=cuts, method="inclusive")
             for v, d in drawn.items() if len(d) > 1}  # fmt: skip
    return {v: (edges[v][0], edges[v][-1]) if v in edges else None for v in drawn}


def _resampled(
    clusters: Mapping[str, Sequence[float]], values: Sequence[float]
) -> dict[float, list[float]]:
    """Per forecast value, its PAV rate in each task-cluster resample that spans it."""
    keys = sorted(clusters)
    drawn: dict[float, list[float]] = {v: [] for v in values}
    rng = random.Random(BAND_SEED)
    for _ in range(BAND_DRAWS):
        xs = [p for k in rng.choices(keys, k=len(keys)) for p in clusters[k]]
        fit = scoring.corp_reliability(xs, [int(rng.random() < p) for p in xs])
        for v in values:
            rate = _at(fit, v)
            if rate is not None:
                drawn[v].append(rate)
    return drawn


def _evidence(rows: Sequence[_Scored], fallback: str, notes: Sequence[str]) -> Evidence:
    if not rows:
        return Evidence("unobservable", [fallback], REDUCER_VERSION, list(notes), [])
    partial = any(s.coverage != "full" for s in rows)
    warnings = sorted({w for s in rows for w in s.warnings})
    return Evidence("partial" if partial else "full", [s.source for s in rows],
                    REDUCER_VERSION, list(notes), warnings)  # fmt: skip


def _block(
    key: tuple[str, str, str],
    rows: Sequence[_Scored],
    refs: Sequence[str],
    ev: Evidence,
) -> Block:
    measure, arm, role = key
    tasks = len({s.task for s in rows})
    if len(rows) < MIN_FORECASTS:
        return Block(measure, arm, role, len(rows), tasks, "below_minimum", None, {},
                     None, (), ev)  # fmt: skip
    ps, os = [s.p for s in rows], [s.o for s in rows]
    bands = _bands(rows)
    bins = tuple(
        Bin(p, rate, n, bands[p], n >= BIN_MIN[p])
        for p, rate, n in scoring.corp_reliability(ps, os)
    )
    brier = math.fsum((p - o) ** 2 for p, o in zip(ps, os, strict=True)) / len(rows)
    skill = {ref: _skill(rows, ref) for ref in refs}
    return Block(measure, arm, role, len(rows), tasks, None, brier, skill,
                 scoring.decomposition(ps, os), bins, ev)  # fmt: skip


def _blocks(view: _View, p1: Sequence[_Scored], f1: Sequence[_Scored]) -> list[Block]:
    fallback = f"component_maps:{view.repo_id}:{view.cmap.version}"
    p1_notes = (*P1_ASSUMPTIONS, *SCORE_ASSUMPTIONS)
    blocks: list[Block] = []
    for role in ROLES:
        for arm in ARMS:
            rows = [s for s in p1 if s.role == role and s.arm == arm]
            ev = _evidence(rows, fallback, p1_notes)
            blocks.append(_block(("P1", arm, role), rows, P1_REFS, ev))
    ev = _evidence(f1, fallback, (*F1_ASSUMPTIONS, *SCORE_ASSUMPTIONS))
    blocks.append(_block(("F1", "treatment", "forecast"), f1, F1_REFS, ev))
    return blocks


def _weeks(view: _View) -> dict[str, int]:
    weeks: Counter[str] = Counter()
    for r in view.first:
        year, week, _ = _t(r["ts"]).isocalendar()
        weeks[f"{year}-W{week:02d}"] += 1
    return dict(sorted(weeks.items()))


def _pr1(tests: Mapping[str, tuple[float, int] | None], extra: Sequence[str]) -> Rule:
    """docs/preregistration.md 2.2. V1, V5 and V6 are pilot results the ledger does
    not hold, so the rule cannot be met from the ledger alone yet."""
    reasons = [*extra, "pilot_validity_not_recorded"]
    if tests.get("pipeline_placebo") is None:
        reasons.append("pipeline_placebo_not_computable")
    specific = tests.get("specific")
    if not tests:
        reasons.append("tests_not_run")
    elif specific is None:
        reasons.append("specific_test_not_computable")
    elif specific[0] >= ALPHA:
        reasons.append("specific_p_not_below_alpha")
    return Rule("PR-1", not reasons, tuple(reasons), PR1_CLAIM)


def scores(
    store: Store, repo_id: str, *, seed: int | None = None, draws: int | None = None
) -> Scores:
    """Every block, the exclusion counts and the weekly counts; with a seed and a draw
    count also the randomization tests and the rule they feed."""
    view = _view(store, repo_id)
    if isinstance(view, tuple):
        reason, version = view
        return Scores(repo_id, version, reason, (), {}, {}, {}, (_pr1({}, [reason]),))
    p1, p1_counts = _p1(view)
    f1, f1_counts = _f1(view)
    tests: dict[str, tuple[float, int] | None] = {}
    if seed is not None and draws is not None:
        tests = _tests(view, (p1, f1), seed, draws)
    return Scores(repo_id, view.cmap.version, None, tuple(_blocks(view, p1, f1)),
                  dict(sorted((p1_counts + f1_counts).items())), _weeks(view), tests,
                  (_pr1(tests, []),))  # fmt: skip


# -- randomization ------------------------------------------------------------------


def _contrast(
    rows: Sequence[_Scored], seed: int, draws: int
) -> tuple[float, int] | None:
    per_task: dict[str, list[float]] = {}
    arm: dict[str, int] = {}
    for s in rows:
        per_task.setdefault(s.task, []).append((s.p - s.o) ** 2)
        arm[s.task] = int(s.arm == "treatment")
    tasks = sorted(per_task)
    treated = sum(arm[t] for t in tasks)
    if not 0 < treated < len(tasks):
        return None
    values = [math.fsum(per_task[t]) / len(per_task[t]) for t in tasks]
    return scoring.randomization_test(values, [arm[t] for t in tasks], seed, draws)


def _shuffle(
    view: _View, rows: Sequence[_Scored], seed: int, draws: int
) -> tuple[float, int] | None:
    """The task-shuffle placebo of design 4.4 g."""
    tasks = sorted({s.task for s in rows})
    if len(tasks) < 2:
        return None
    given = {t: [(s.component, s.p) for s in rows if s.task == t and s.component]
             for t in tasks}  # fmt: skip
    r1 = {t: {c: _r1(view, view.tasks[t], c) for c in view.names.product}
          for t in tasks}  # fmt: skip
    stat = partial(_shuffled_skill, view.touched, tasks, given, r1)
    return _placebo(stat, tasks, seed, draws)


def _shuffled_skill(
    touched: Mapping[str, Mapping[str, int]],
    tasks: Sequence[str],
    given: Mapping[str, Sequence[tuple[str, float]]],
    r1: Mapping[str, Mapping[str, float | None]],
    order: Sequence[str],
) -> float | None:
    """Skill of each task's forecasts against the outcome of the task order puts in
    its place."""
    owner, against = [], []
    for t, u in zip(tasks, order, strict=True):
        outcome = touched[u]
        for c, p in given[t]:
            ref = r1[u][c]
            if c in outcome and ref is not None and any(outcome.values()):
                owner.append((p - outcome[c]) ** 2)
                against.append((ref - outcome[c]) ** 2)
    return scoring.bss(math.fsum(owner), math.fsum(against)) if owner else None


def _placebo(
    stat: Callable[[Sequence[str]], float | None],
    tasks: Sequence[str],
    seed: int,
    draws: int,
) -> tuple[float, int] | None:
    observed = stat(tasks)
    if observed is None:
        return None
    exact = math.factorial(len(tasks)) <= scoring.EXACT_MAX
    if exact:
        orders: Iterable[Sequence[str]] = permutations(tasks)
    else:
        rng = random.Random(seed)
        orders = (rng.sample(list(tasks), len(tasks)) for _ in range(draws))
    found = [s for s in map(stat, orders) if s is not None]
    hits = sum(s >= observed - _PAIR_TIE for s in found)
    if exact:
        return hits / len(found), len(found)
    return (1 + hits) / (len(found) + 1), len(found)


def _tests(
    view: _View,
    scored: tuple[Sequence[_Scored], Sequence[_Scored]],
    seed: int,
    draws: int,
) -> dict[str, tuple[float, int] | None]:
    p1, f1 = scored
    tests = {
        name: _contrast([s for s in p1 if s.role == role], seed, draws)
        for name, role in CONTRASTS.items()
    }
    tests["pipeline_placebo"] = None
    tests["f1_shuffle"] = _shuffle(view, f1, seed, draws)
    return tests


def randomization(
    store: Store, repo_id: str, seed: int, draws: int
) -> dict[str, tuple[float, int] | None]:
    """(one-sided p, allocations evaluated) per test in TESTS; None where it cannot
    be computed (an arm without data, fewer than two tasks, no probe of a never-drawn
    item for the pipeline placebo)."""
    view = _view(store, repo_id)
    if isinstance(view, tuple):
        return dict.fromkeys(TESTS)
    return _tests(view, (_p1(view)[0], _f1(view)[0]), seed, draws)


# -- text ---------------------------------------------------------------------------


def _num(x: float | None, digits: int = 3) -> str:
    return "null" if x is None else f"{round(x, digits) + 0.0:.{digits}f}"


def _block_lines(b: Block) -> list[str]:
    head = f"{b.measure} {b.role} {b.arm}: n {b.n}, tasks {b.tasks}"
    if b.refused is not None:
        return [f"{head}, refused {b.refused} (needs {MIN_FORECASTS})"]
    skill = "; ".join(f"{ref} {_num(v)} (n {n})" for ref, (v, n) in b.skill.items())
    lines = [f"{head}, Brier {_num(b.brier)}, coverage {b.ev.coverage}",
             f"  skill: {skill}"]  # fmt: skip
    if b.decomposition is not None:
        rel, res, unc = b.decomposition
        lines.append(f"  REL {_num(rel)} RES {_num(res)} UNC {_num(unc)}")
    for c in b.bins:
        band = "null" if c.band is None else f"{_num(c.band[0])}-{_num(c.band[1])}"
        read = "read" if c.interpretable else f"not read below {BIN_MIN[c.p]}"
        lines.append(f"  CORP {c.p:g}: rate {_num(c.rate)}, n {c.n}, band {band}, "
                     f"{read}")  # fmt: skip
    return lines


def _test_lines(tests: Mapping[str, tuple[float, int] | None]) -> list[str]:
    if not tests:
        return ["tests: not run (needs the committed seed and draw count)"]
    return ["tests: " + "; ".join(
        f"{name} not computable" if r is None
        else f"{name} p {_num(r[0], 4)} over {r[1]} allocations"
        for name, r in tests.items()
    )]  # fmt: skip


def _lines(s: Scores) -> list[str]:
    lines = [f"praxis report: repository {s.repo_id}, component map "
             f"{s.map_version or 'none'}"]  # fmt: skip
    if s.refused is not None:
        lines.append(f"refused: {s.refused}")
    lines.append("claims: " + ", ".join(f"{k} {v}" for k, v in CLAIMS.items()))
    lines.append(
        "P1 is accuracy on structural items about components bound to "
        "tasks, no more; F1 is calibration on forecasts of agent work"
    )
    weeks = ", ".join(f"{w} {n}" for w, n in s.weeks.items()) or "none"
    lines.append(f"first probes by week: {weeks}")
    excluded = "; ".join(" ".join((*k, str(n))) for k, n in s.counts.items())
    lines.append(f"excluded: {excluded or 'none'}")
    for b in s.blocks:
        lines += _block_lines(b)
    lines += _test_lines(s.tests)
    for r in s.rules:
        lines.append(f"rule {r.name} met: {r.claim}" if r.met
                     else f"rule {r.name} not met: {', '.join(r.reasons)}")  # fmt: skip
    notes = dict.fromkeys(a for b in s.blocks for a in b.ev.assumptions)
    if s.tests:
        notes.update(dict.fromkeys(TEST_ASSUMPTIONS))
    lines += ["assumptions:", *(f"  - {a}" for a in notes)] if notes else []
    return lines


def render(scores: Scores, out: TextIO) -> None:
    """Write the report, or nothing: a refused word without a met rule raises."""
    text = "\n".join(_lines(scores)) + "\n"
    found = _REFUSED.search(text)
    if found is not None and not any(r.met for r in scores.rules):
        word = found.group(0).lower()
        raise ValueError(f"report refused: {word!r} needs a met pre-registered rule")
    out.write(text)
