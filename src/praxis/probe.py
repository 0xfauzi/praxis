"""`praxis probe <kind>`: unassisted elicitation, design 03 sections 4.1 (P1
structural), 4.2 (S1 drift), 4.6 (S3 decision recall), 4.7 (K knockout) and 4.8 (C1
Pathfinder).

Every kind refuses while an agent process works in the repository, records the refusal
as an `agent_attached` flag and writes no response. An answer is closed vocabulary;
anything else typed is asked again and dropped, and a free recall is read and dropped,
not even its length kept. End of input stops the administration with an `abandoned`
flag; what was not answered is not written, so an unanswered item stays due. A
structural item whose truth X1 cannot give now (the tree unreadable, its source
component unparsed or without Python files, a relation with no reader yet) is not
asked: a response row needs a truth and a guessed one is worse than a late probe.
Structural answers get no feedback (P1 is the criterion, 4.1 c); drift and decision
answers do (4.2, 4.6). Pathfinder ratings live in memory until PFSIM is computed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from functools import partial
from itertools import combinations
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, TextIO

from praxis import transcript
from praxis.components import ComponentMap, assign, from_json, product_components
from praxis.extract import GIT_TIMEOUT_S, StaticGraph, reach, static_imports
from praxis.scoring import pfnet, pfsim
from praxis.store import Evidence, Store
from praxis.unassisted import Repo, attached_agents, locate

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Sequence

# docs/preregistration.md 1.4, provisional until Stage 2 replaces them from derive.py.
L_DAYS, L_MAX_DAYS, DEADLINE_S = 7.0, 28.0, 60.0
# Claude Code deletes transcripts after cleanupPeriodDays, default 30 (design 4.6).
DECISION_MAX_DAYS = 30.0
DRIFT_DAYS = 7
REDUCER_VERSION = "probe-v1"
SKIP = "s"
GRID_KEYS = {"1": 0.1, "3": 0.3, "5": 0.5, "7": 0.7, "9": 0.9}
YES_NO = {"y": True, "n": False, SKIP: None}
SAID = {True: "yes", False: "no", None: "unknown"}
QUESTION = {
    "D1": "Does code in {} import code in {}?",
    "D2": "Does code in {} reach code in {} through other components' imports?",
}
DECISION_VOIDS = ("transcript_changed", "transcript_swept", "reversed")
STRUCTURAL_ASSUMPTIONS = (
    "truth is X1 on the working tree at probe time; an absent edge may be an "
    "extractor blind spot",
    "truth_changed compares with the item's truth at truth_ts, at or before exposure",
    "the 60 s deadline is provisional; it needs measuring from the pilot's "
    "response-time distribution (preregistration 1.4)",
    "a lookup during the probe cannot be detected",
    "seconds run from the question to the accepted answer, re-asks included",
    "typed 1 3 5 7 9 is read as 0.1 0.3 0.5 0.7 0.9; s is a skip",
)
DRIFT_ASSUMPTIONS = (
    "changed means a file of the component gained or lost an import of another "
    "component between consecutive recorder scans, the later one in the 7 days "
    "before the probe",
    "the edge history holds only files agent segments changed: edits outside a "
    "recorded session are not seen",
    "a component no scan touched is unchanged when the recorder scanned anything in "
    "the window, and unknown otherwise",
)
DECISION_ASSUMPTIONS = (
    "the option text is never shown; the owner recalls the index among k options",
    "a reread of the transcript before the probe cannot be detected: gameable",
    "a reversed decision is not detected yet: the survival threshold needs measuring",
)
KNOCKOUT_ASSUMPTIONS = (
    "simulated: a scenario fails when its latest ok X2 run entered the knocked "
    "component; design 4.7 executes the knockout in a scratch clone",
    "scenario runs carry no tree hash and may predate HEAD",
    "the knocked component is a uniform draw keyed by repository and probe time",
)
PATHFINDER_ASSUMPTIONS = (
    "ratings 1 to 9 become distances 10 - rating; PFnet(r = infinity, q = n - 1)",
    "referent N_imp only: a link wherever X1 has an edge in either direction; N_co "
    "is not computed",
    "chance is the S of E[C] shared links: E[C] / (|L1| + |L2| - E[C])",
    "a code-derived referent standing in for an expert's is unvalidated (design 4.8)",
)


class _Quit(Exception):
    """End of input: the owner stopped the administration."""


@dataclass
class _Probe:
    ledger: Store
    repo: Repo
    cmap: ComponentMap
    stdin: TextIO
    out: TextIO
    start: dt.datetime
    clock: float = field(default_factory=time.monotonic)

    def now(self) -> dt.datetime:
        return self.start + dt.timedelta(seconds=time.monotonic() - self.clock)

    def ask[T](self, prompt: str, parse: Callable[[str], T]) -> T:
        while True:
            self.out.write(prompt)
            self.out.flush()
            line = self.stdin.readline()
            if not line:
                self.out.write("\n")
                raise _Quit
            try:
                return parse(line.strip())
            except ValueError:
                self.out.write("  not one of the answers offered\n")

    def abandon(self, kind: str, **counts: int) -> int:
        self.ledger.flag(ts=_stamp(self.now()), code="abandoned",
                         detail={"kind": kind, **counts},
                         repo_id=self.repo.repo_id)  # fmt: skip
        self.out.write("stopped; what was not answered is not recorded\n")
        return 0


@dataclass(frozen=True)
class _Tree:
    graph: StaticGraph
    failed: frozenset[str]
    head_sha: str | None
    tree_hash: str | None
    dirty: bool | None

    def sources(self) -> list[str]:
        return [f"x1:{self.graph.extractor_version}", f"tree:{self.tree_hash}"]

    def warnings(self, components: Iterable[str], whole: bool) -> list[str]:
        """A parse failure anywhere matters only to a relation over whole paths."""
        spots = self.graph.blind_spots
        found = [("blind_spot", any(spots.get(c, 0) for c in components)),
                 ("parse_failure", whole and bool(self.failed))]  # fmt: skip
        return [code for code, hit in found if hit]


def due(
    praxis_home: Path, repo_id: str, now: str, lag_days: float
) -> list[sqlite3.Row]:
    """Exposed items on the frozen map, unanswered, exposed at least ``lag_days`` and
    at most L_MAX_DAYS before ``now``, oldest exposure first."""
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        frozen = ledger.component_map_frozen(repo_id)
        if frozen is None:
            return []
        version = from_json(frozen).version
        return _due(ledger, repo_id, version, _when(now), lag_days)
    finally:
        ledger.close()


def administer(
    repo_root: Path,
    praxis_home: Path,
    stdin: TextIO,
    stdout: TextIO,
    now: str,
    kind: str,
) -> int:
    if kind not in _KINDS:
        raise ValueError(f"probe kinds are {', '.join(_KINDS)}")
    repo = locate(repo_root)
    if repo is None:
        stdout.write("praxis probe: not inside a git repository\n")
        return 2
    start = _when(now)
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        return _administer(ledger, repo, (stdin, stdout), start, kind)
    finally:
        ledger.close()


def _administer(
    ledger: Store,
    repo: Repo,
    io: tuple[TextIO, TextIO],
    start: dt.datetime,
    kind: str,
) -> int:
    stdin, out = io
    agents = attached_agents(repo.root)
    if agents:
        ledger.flag(ts=_stamp(start), code="agent_attached",
                    detail={"kind": kind, "agents": len(agents)},
                    repo_id=repo.repo_id)  # fmt: skip
        out.write("praxis probe: an agent is attached to this repository; nothing "
                  "was asked and the refusal is recorded\n")  # fmt: skip
        out.writelines(f"  {a}\n" for a in agents)
        return 2
    frozen = ledger.component_map_frozen(repo.repo_id)
    if frozen is None:
        out.write("praxis probe: no frozen component map; run `praxis components "
                  "freeze` first\n")  # fmt: skip
        return 2
    return _KINDS[kind](_Probe(ledger, repo, from_json(frozen), stdin, out, start))


# -- P1 structural ------------------------------------------------------------------


def _structural(p: _Probe) -> int:
    items = _due(p.ledger, p.repo.repo_id, p.cmap.version, p.start, L_DAYS)
    if not items:
        p.out.write("no structural item is due\n")
        return 0
    tree = _read_tree(p.repo.root, p.cmap)
    if tree is None:
        p.out.write("the working tree could not be read; every item stays due\n")
        return 0
    product = product_components(p.cmap)
    p.out.write(f"{len(items)} items due, one at a time, {DEADLINE_S:.0f} s each. "
                "No answer is shown until the block's debrief.\n")  # fmt: skip
    asked = unread = 0
    for item in items:
        truth = _truth(tree, item, product)
        if truth is None:
            unread += 1
            continue
        try:
            _answer(p, tree, item, truth)
        except _Quit:
            return p.abandon("structural", answered=asked, due=len(items))
        asked += 1
    p.out.write(f"answered {asked}; {unread} could not be scored now and stay due\n")
    return 0


def _answer(p: _Probe, tree: _Tree, item: sqlite3.Row, truth: bool) -> None:
    shown = time.monotonic()
    prompt = f"{_question(item)} [1 3 5 7 9, s skips; {DEADLINE_S:.0f} s] "
    answer = p.ask(prompt, _grid)
    seconds = time.monotonic() - shown
    when = p.now()
    d1 = item["relation"] == "D1"
    pair = (item["src_component"], item["dst_component"])
    warnings = tree.warnings(pair if d1 else tree.graph.blind_spots, whole=not d1)
    ev = Evidence("partial" if warnings else "full",
                  [f"probe_item:{item['item_id']}", *tree.sources()],
                  REDUCER_VERSION, STRUCTURAL_ASSUMPTIONS, warnings)  # fmt: skip
    p.ledger.probe_response(
        ev=ev, item_id=item["item_id"], task_id=item["task_id"], ts=_stamp(when),
        p=answer, seconds=round(seconds, 3), lag_days=_days(item["exposed_ts"], when),
        deadline_met=seconds <= DEADLINE_S, truth_at_probe=truth,
        truth_changed=truth != bool(item["truth"]), head_sha=tree.head_sha,
        tree_hash=tree.tree_hash, dirty=tree.dirty,
        extractor_version=tree.graph.extractor_version,
    )  # fmt: skip


def _truth(tree: _Tree, item: sqlite3.Row, product: Sequence[str]) -> bool | None:
    """D1: a imports b. D2: a reaches b along a path of two or more component edges,
    which never revisits a. None when the source component was not parsed."""
    a, b, relation = item["src_component"], item["dst_component"], item["relation"]
    graph = tree.graph
    if relation not in QUESTION or a not in graph.blind_spots or a in tree.failed:
        return None
    edges = {(e.src, e.dst) for e in graph.edges}
    if relation == "D1":
        return (a, b) in edges
    beyond = reach(graph, [c for c in product if c != a])
    return any((a, c) in edges and (c, b) in beyond for c in product if c not in (a, b))


def _read_tree(root: Path, cmap: ComponentMap) -> _Tree | None:
    lift = partial(assign, cmap)
    try:
        graph = static_imports(root, lift)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    status = _git(root, "status", "--porcelain")
    return _Tree(
        graph,
        frozenset(map(lift, graph.parse_failures)),
        _git(root, "rev-parse", "--verify", "-q", "HEAD"),
        _git(root, "rev-parse", "--verify", "-q", "HEAD^{tree}"),
        None if status is None else status != "",
    )


def _git(root: Path, *args: str) -> str | None:
    # --no-optional-locks: a plain status refreshes .git/index, a write to the
    # owner's repository.
    argv = ["git", "-C", str(root), "--no-optional-locks", *args]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _due(
    ledger: Store, repo_id: str, version: str, now: dt.datetime, lag_days: float
) -> list[sqlite3.Row]:
    rows = ledger.exposures_unanswered(repo_id, version)
    return [r for r in rows if lag_days <= _days(r["exposed_ts"], now) <= L_MAX_DAYS]


def _question(item: sqlite3.Row) -> str:
    return QUESTION[item["relation"]].format(item["src_component"],
                                             item["dst_component"])  # fmt: skip


# -- S1 drift -----------------------------------------------------------------------


def _drift(p: _Probe) -> int:
    repo_id, version = p.repo.repo_id, p.cmap.version
    iso = p.start.astimezone(dt.UTC).isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    if p.ledger.drift_responses_for(repo_id, version, week):
        p.out.write(f"the drift probe for {week} is already answered\n")
        return 0
    since = p.start - dt.timedelta(days=DRIFT_DAYS)
    history = p.ledger.edge_history(repo_id)
    changed, sessions = _changed(history, version, since, p.start)
    product = product_components(p.cmap)
    p.out.write(f"In the {DRIFT_DAYS} days before now, did the agent change which "
                "other components code in each of these imports?\n")  # fmt: skip
    said: list[bool | None] = []
    for c in product:
        try:
            said.append(p.ask(f"  {c} [y n, s skips] ", _yes_no))
        except _Quit:
            return p.abandon("drift", answered=len(said), of=len(product))
    sources = [f"edge_history:{_stamp(since)}/{_stamp(p.start)}", *sessions]
    for c, s in zip(product, said, strict=True):
        did = changed(c)
        ev = Evidence("unobservable" if did is None else "full", sources,
                      REDUCER_VERSION, DRIFT_ASSUMPTIONS, [])  # fmt: skip
        p.ledger.drift_response(ev=ev, repo_id=repo_id, map_version=version,
                                ts=_stamp(p.now()), week=week, component=c,
                                said_changed=s, did_change=did)  # fmt: skip
        p.out.write(f"  {c}: you said {SAID[s]}, changed: {SAID[did]}\n")
    return 0


def _changed(
    history: Sequence[sqlite3.Row],
    version: str,
    since: dt.datetime,
    until: dt.datetime,
) -> tuple[Callable[[str], bool | None], list[str]]:
    """Per component, whether a scan in [since, until] found a file's set of imported
    components different from that file's previous scan; and the scans' sessions."""
    scans: dict[tuple[str, str], set[str]] = {}
    owner: dict[str, str] = {}
    sessions: set[str] = set()
    for row in history:
        if row["map_version"] != version:
            continue
        dsts = scans.setdefault((row["ts"], row["src_file"]), set())
        if row["dst_component"] is not None:
            dsts.add(row["dst_component"])
        owner[row["src_file"]] = row["src_component"]
        if row["session_id"] is not None and since <= _when(row["ts"]) <= until:
            sessions.add(row["session_id"])
    before: dict[str, frozenset[str]] = {}
    found: dict[str, bool | None] = {}
    active = False
    for (ts, path), dsts in scans.items():  # history is ordered by ts
        state = frozenset(dsts)
        if since <= _when(ts) <= until:
            active = True
            last = before.get(path)
            verdict = None if last is None else last != state
            found[owner[path]] = _either(found.get(owner[path], False), verdict)
        before[path] = state
    return (lambda c: found.get(c, False) if active else None), sorted(sessions)


def _either(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


# -- S3 decision recall -------------------------------------------------------------


def _decision(p: _Probe) -> int:
    voided = p.ledger.decisions_flagged(DECISION_VOIDS)
    rows = [
        d
        for d in p.ledger.decisions_unprobed(p.repo.repo_id)
        if d["id"] not in voided
        and d["chosen_index"] is not None
        and L_DAYS <= _days(d["ts"], p.start) <= DECISION_MAX_DAYS
    ]
    if not rows:
        p.out.write("no decision is due\n")
        return 0
    projects = Path.home() / ".claude" / "projects"
    asked = 0
    for d in rows:
        state = _transcript_state(d, projects)
        if state in DECISION_VOIDS:
            p.ledger.flag(ts=_stamp(p.now()), code=state,
                          detail={"decision_id": d["id"]}, repo_id=p.repo.repo_id,
                          session_id=d["session_id"])  # fmt: skip
        if state != "ok":
            continue
        try:
            _recall(p, d)
        except _Quit:
            return p.abandon("decision", answered=asked, due=len(rows))
        asked += 1
    p.out.write(f"answered {asked} of {len(rows)} due decisions\n")
    return 0


def _transcript_state(decision: sqlite3.Row, projects: Path) -> str:
    """ok, a void reason, or unverifiable (an ambiguous or unreadable transcript,
    which leaves the decision unprobed and unflagged)."""
    try:
        path = transcript.locate(decision["session_id"], projects)
        if path is None:
            return "transcript_swept"
        tools = transcript.read(path).tools
    except transcript.TranscriptError:
        return "unverifiable"
    same = [t for t in tools if t.tool_use_id == decision["tool_use_id"]]
    held = len(same) == 1 and same[0].options_hash == decision["options_hash"]
    return "ok" if held else "transcript_changed"


def _recall(p: _Probe, d: sqlite3.Row) -> None:
    k = d["option_count"]
    p.out.write(f"On {d['ts'][:10]} the agent's {d['tool_name']} offered {k} "
                "options.\n")  # fmt: skip
    p.ask("  What was decided, and why? (shown, then discarded) ", _discard)
    recalled = p.ask(f"  Which option was chosen? [1-{k}, s skips] ", partial(_pick, k))
    when = p.now()
    ev = Evidence("full", [f"decision:{d['session_id']}:{d['tool_use_id']}"],
                  REDUCER_VERSION, DECISION_ASSUMPTIONS, [])  # fmt: skip
    p.ledger.decision_response(ev=ev, decision_id=d["id"], ts=_stamp(when),
                               recalled_index=recalled,
                               lag_days=_days(d["ts"], when))  # fmt: skip
    p.out.write(f"  It was option {d['chosen_index'] + 1}.\n")


# -- K knockout ---------------------------------------------------------------------


def _knockout(p: _Probe) -> int:
    runs: dict[str, tuple[set[str], int] | None] = {}
    for run in p.ledger.scenario_runs_for(p.repo.repo_id, p.cmap.version):
        ok = run["outcome"] == "ok"
        seen = (set(json.loads(run["components_json"])), run["id"])
        runs[run["scenario_id"]] = seen if ok else None
    traced = {s: r for s, r in sorted(runs.items()) if r is not None}
    if not traced:
        p.out.write("no scenario has an ok run on the frozen map; the knockout needs "
                    "one\n")  # fmt: skip
        return 2
    product = product_components(p.cmap)
    key = json.dumps([p.repo.repo_id, _stamp(p.start), "knockout"])
    knocked = Random(int(hashlib.sha256(key.encode()).hexdigest(), 16)).choice(product)
    failed = [s for s, (comps, _) in traced.items() if knocked in comps]
    p.out.write("One component is knocked out: every public name in it raises.\n"
                f"Scenarios that fail: {', '.join(failed) or 'none'} "
                f"(of {len(traced)})\n")  # fmt: skip
    p.out.writelines(f"  {i} {c}\n" for i, c in enumerate(product, 1))
    prompt = "Which was knocked out? [numbers, space-separated; s skips] "
    try:
        named = p.ask(prompt, partial(_choices, len(product)))
    except _Quit:
        return p.abandon("knockout")
    if named is None:
        p.out.write("skipped\n")
        return 0
    names = [product[i] for i in named]
    ev = Evidence("full", [f"scenario_run:{r[1]}" for r in traced.values()],
                  REDUCER_VERSION, KNOCKOUT_ASSUMPTIONS, [])  # fmt: skip
    p.ledger.knockout(components_named=names, ev=ev, repo_id=p.repo.repo_id,
                      map_version=p.cmap.version, ts=_stamp(p.now()),
                      component_knocked=knocked, scenario_id=None,
                      hit=knocked in names)  # fmt: skip
    p.out.write("recorded\n")
    return 0


# -- C1 Pathfinder ------------------------------------------------------------------


def _pathfinder(p: _Probe) -> int:
    product = product_components(p.cmap)
    tree = _read_tree(p.repo.root, p.cmap)
    if tree is None:
        p.out.write("the working tree could not be read; nothing was asked\n")
        return 2
    pairs = list(combinations(range(len(product)), 2))
    p.out.write(f"How related is each pair, 1 (unrelated) to 9 (closely)? "
                f"{len(pairs)} pairs.\n")  # fmt: skip
    dist = [[0.0] * len(product) for _ in product]
    for rated, (i, j) in enumerate(pairs):
        try:
            r = p.ask(f"  {product[i]} - {product[j]} [1-9, s stops] ", _rating)
        except _Quit:
            r = None
        if r is None:
            return p.abandon("pathfinder", rated=rated, pairs=len(pairs))
        dist[i][j] = dist[j][i] = 10.0 - r
    score = _similarity(pfnet(dist), tree.graph, product)
    del dist
    warnings = tree.warnings(product, whole=True)
    ev = Evidence("partial" if warnings else "full", tree.sources(),
                  REDUCER_VERSION, PATHFINDER_ASSUMPTIONS, warnings)  # fmt: skip
    s, beyond = (None, None) if score is None else score
    p.ledger.pathfinder_run(ev=ev, repo_id=p.repo.repo_id, map_version=p.cmap.version,
                            ts=_stamp(p.now()), n_components=len(product), pfsim=s,
                            pfsim_minus_chance=beyond)  # fmt: skip
    p.out.write("recorded; the ratings are discarded\n")
    return 0


def _similarity(
    owner: list[list[bool]], graph: StaticGraph, product: Sequence[str]
) -> tuple[float, float] | None:
    """(PFSIM, PFSIM minus the S of chance overlap) against N_imp, or None."""
    edges = {(e.src, e.dst) for e in graph.edges}
    imp = [[a != b and ((a, b) in edges or (b, a) in edges) for b in product]
           for a in product]  # fmt: skip
    found = pfsim(owner, imp)
    if found is None:
        return None
    s, expected = found
    l1, l2 = _links(owner), _links(imp)
    return s, s - expected / (l1 + l2 - expected)


def _links(net: list[list[bool]]) -> int:
    n = len(net)
    return sum(net[i][j] or net[j][i] for i in range(n) for j in range(i + 1, n))


# -- closed vocabularies and time ---------------------------------------------------


def _grid(key: str) -> float | None:
    if key != SKIP and key not in GRID_KEYS:
        raise ValueError
    return GRID_KEYS.get(key)


def _yes_no(key: str) -> bool | None:
    if key not in YES_NO:
        raise ValueError
    return YES_NO[key]


def _pick(k: int, key: str) -> int | None:
    if key == SKIP:
        return None
    if not key.isdecimal() or not 1 <= int(key) <= k:
        raise ValueError
    return int(key) - 1


def _choices(n: int, key: str) -> list[int] | None:
    """Distinct component numbers; a number typed twice is refused, not merged."""
    if key == SKIP:
        return None
    picks = [_pick(n, word) for word in key.split()]
    found = [i for i in picks if i is not None]
    if not found or len(found) != len(picks) or len(set(found)) != len(found):
        raise ValueError
    return sorted(found)


def _rating(key: str) -> int | None:
    if key == SKIP:
        return None
    if len(key) != 1 or key not in "123456789":
        raise ValueError
    return int(key)


def _discard(_typed: str) -> None:
    return None


def _when(ts: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(ts)
    if when.tzinfo is None:
        raise ValueError("a timestamp without a zone")
    return when


def _stamp(when: dt.datetime) -> str:
    text = when.astimezone(dt.UTC).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


def _days(ts: str, now: dt.datetime) -> float:
    return (now - _when(ts)).total_seconds() / 86400


_KINDS: dict[str, Callable[[_Probe], int]] = {
    "structural": _structural,
    "drift": _drift,
    "decision": _decision,
    "knockout": _knockout,
    "pathfinder": _pathfinder,
}
KINDS = tuple(_KINDS)
