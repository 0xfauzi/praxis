"""Item supply: `praxis set components` fixes (freezes) a repository's component map and
fills its D1 pool; `praxis show components` shows the map and what the pool has left
(design 03 sections 2.4, 3.1 and 3.5). The map frozen is the owner's toml when there is
one, else the mechanical map; `praxis make components` prints the mechanical map in the
toml's format, so the owner overrides it by editing the draft. Freeze refuses fewer than
5 or more than 30 product components (design 2.2), printing a mechanical map's draft.
The first freeze of a repository also draws its start week (task.draw_start, design
6.4), silently: the owner must not learn the schedule.

A D1 item exists only for an ordered pair of product components that each have a
Python file X1 parsed: a component it never parsed is unknown, so its pairs are counted
as not generated and never written as truth 0. An item touching a component with a
blind spot or a parse failure is `partial`. Truth is X1 over the working tree at
freeze time, epoch 0. A pair that already has an item under the map keeps it, so a
second freeze writes only the missing pairs and a partial first run is completed, not
duplicated. A pair whose X1 truth differs from its current epoch's item is
re-admitted as a new item at the next epoch (design 3.5); an unflipped item, a pair X1
did not parse this time and every exposure are left as they are. Git is only read:
`--no-optional-locks` keeps `git status` from refreshing the owner's index.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TextIO

from praxis.components import (
    MAX_COMPONENTS,
    MIN_COMPONENTS,
    RESERVED,
    UNMAPPED,
    ComponentMap,
    TooFewComponents,
    assign,
    derive,
    from_json,
    load_owner,
    owner_path,
    product_components,
    to_json,
    to_toml,
)
from praxis.extract import GIT_TIMEOUT_S, Edge, StaticGraph, reach, static_imports
from praxis.schema import MAP_REASONS
from praxis.store import Evidence, Store
from praxis.task import K_E, K_P, K_R, draw_start
from praxis.unassisted import Repo, locate

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

REDUCER_VERSION = "pool-v1"
ITEMS_PER_TASK = K_E + K_R + K_P
ASSUMPTIONS = (
    "truth is X1 static imports over the working tree at truth_ts: tracked and"
    " untracked-not-ignored Python files, resolved against the whole tree",
    "an absent edge may be an extractor blind spot: dynamic imports, string plugin"
    " registries, configuration and SQL wiring are not seen",
    "a pair is an item only when both components have a Python file X1 parsed",
)
COUNT_KEYS = ("components", "pairs", "items", "positive", "unexposed", "tasks_left")
FREEZE = "praxis set components"
DERIVE = "praxis make components"
SHOW = "praxis show components"
NO_REPO = "run this command in a git repository."
Row = tuple[str, str, str, bool, int, int | None, bool | None]


@dataclass(frozen=True)
class _Supply:
    """X1 at component grain over the product components it parsed."""

    parsed: list[str]
    edges: dict[tuple[str, str], Edge]
    reach: set[tuple[str, str]]
    partial: dict[str, list[str]]
    extractor_version: str

    @property
    def pairs(self) -> int:
        return len(self.parsed) * (len(self.parsed) - 1)


def freeze(
    repo_root: Path,
    praxis_home: Path,
    stdout: TextIO,
    now: str,
    reason: str | None = None,
) -> int:
    repo = locate(repo_root)
    if repo is None:
        return _refuse(stdout, FREEZE, NO_REPO)
    if reason is not None and reason not in MAP_REASONS:
        return _refuse(stdout, FREEZE, f"write --reason as one of {_reasons()}.")
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        return _freeze(ledger, repo, praxis_home, (stdout, now, reason))
    finally:
        ledger.close()


def show(repo_root: Path, praxis_home: Path, stdout: TextIO) -> int:
    repo = locate(repo_root)
    if repo is None:
        return _refuse(stdout, SHOW, NO_REPO)
    path = praxis_home / "ledger.db"
    frozen, counts = None, dict.fromkeys(COUNT_KEYS)
    if path.exists():  # a read never creates the ledger
        ledger = Store(path).open()
        try:
            frozen = ledger.component_map_frozen(repo.repo_id)
            counts = _counts(ledger, repo.repo_id)
        finally:
            ledger.close()
    if frozen is None:
        stdout.write(
            f"{SHOW}: repository {repo.repo_id} has no set component map. To set one,"
            f" run `{FREEZE}`.\n"
        )
        return 0
    cmap = from_json(frozen)
    stdout.write(
        f"Set component map {cmap.version} ({cmap.source}):"
        f" {len(product_components(cmap))} product components\n"
    )
    for rule in cmap.rules:
        fixed = " (fixed component)" if rule.id in RESERVED else ""
        stdout.write(f"  {rule.id}{fixed}: {' '.join(rule.globs)}\n")
    for warning in cmap.warnings:
        stdout.write(f"Warning: {warning}\n")
    for line in _unmapped(repo.root, cmap):
        stdout.write(line + "\n")
    stdout.write(_supply_line(counts) + "\n")
    return 0


def draft(repo_root: Path, praxis_home: Path, stdout: TextIO, write: bool) -> int:
    """`praxis make components`: the mechanical map in the owner's toml format,
    printed, or with ``write`` saved where load_owner reads it, never over a file."""
    repo = locate(repo_root)
    if repo is None:
        return _refuse(stdout, DERIVE, NO_REPO)
    try:
        text = to_toml(derive(repo.root, repo.repo_id))
    except TooFewComponents as exc:
        return _refuse(
            stdout, DERIVE, f"the repository has {exc}. Praxis made no draft."
        )
    if not write:
        stdout.write(text)
        return 0
    path = owner_path(praxis_home, repo.repo_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        return _refuse(stdout, DERIVE, f"{path} exists. Edit that file, or delete it.")
    stdout.write(f"Praxis wrote {path}. Edit it, then run `{FREEZE}`.\n")
    return 0


def pool_counts(praxis_home: Path, repo_id: str) -> dict[str, int | None]:
    """The supply under the frozen map: product components, ordered pairs, items of
    the current epoch, positives among them, unexposed items and the tasks those last.
    Every value is None without a ledger or a frozen map: not observable, not zero."""
    path = praxis_home / "ledger.db"
    if not path.exists():
        return dict.fromkeys(COUNT_KEYS)
    ledger = Store(path).open()
    try:
        return _counts(ledger, repo_id)
    finally:
        ledger.close()


def _refuse(stdout: TextIO, verb: str, why: str) -> int:
    stdout.write(f"{verb}: {why}\n")
    return 2


def _reasons() -> str:
    return ", ".join(MAP_REASONS)


def _candidate(repo: Repo, praxis_home: Path) -> ComponentMap | None:
    owner = load_owner(praxis_home, repo.repo_id)
    if owner is not None:
        return owner
    try:
        return derive(repo.root, repo.repo_id)
    except TooFewComponents:
        return None


def _freeze(
    ledger: Store,
    repo: Repo,
    praxis_home: Path,
    run: tuple[TextIO, str, str | None],
) -> int:
    stdout, now, reason = run
    accepted = _accept(ledger, repo, praxis_home, run)
    if isinstance(accepted, int):
        return accepted
    cmap, old = accepted
    try:
        graph = static_imports(repo.root, partial(assign, cmap))
        tree, dirty = _tree(repo.root)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return _refuse(stdout, FREEZE, "git cannot list the working tree.")
    try:
        ledger.component_map_put(to_json(cmap), now)
        ledger.component_map_freeze(
            repo.repo_id, cmap.version, now, reason=None if old is None else reason
        )
    except sqlite3.IntegrityError:
        return _refuse(
            stdout, FREEZE, f"map {cmap.version} was replaced. You cannot set it again."
        )
    except ValueError:  # an unedited derive draft: the mechanical map's own rules
        return _refuse(
            stdout,
            FREEZE,
            f"map {cmap.version} has the same rules as a stored map from a different"
            " source. Edit the owner map file, or delete it.",
        )
    draw_start(ledger, repo.repo_id, cmap.version, now)
    product = product_components(cmap)
    supply = _supply(graph, product, partial(assign, cmap))
    sources = [f"extractor:{graph.extractor_version}", f"map:{cmap.version}"]
    sources += [] if tree is None else [f"tree:{tree}"]
    notes = ["no_commit"] * (tree is None) + ["working_tree_dirty"] * dirty
    # Epoch re-admission reads the items present before this freeze writes any.
    flipped = readmit(ledger, repo.repo_id, cmap, supply, (now, sources, notes))
    written, present = _put(ledger, repo.repo_id, cmap, supply, (now, sources, notes))
    stdout.write(
        f"Praxis set component map {cmap.version} ({cmap.source}):"
        f" {len(product)} product components\n"
    )
    for line in _report(cmap, old, reason, supply, product):
        stdout.write(line + "\n")
    stdout.write(f"D1 items: {written} written at epoch 0, {present} already present\n")
    if present:
        stdout.write(
            f"D1 items added at the next epoch for a changed answer: {flipped}\n"
        )
    stdout.write(_supply_line(_counts(ledger, repo.repo_id)) + "\n")
    return 0


def _accept(
    ledger: Store,
    repo: Repo,
    praxis_home: Path,
    run: tuple[TextIO, str, str | None],
) -> tuple[ComponentMap, str | None] | int:
    """The map to freeze and the frozen version it replaces (None when it replaces
    nothing), or the exit code of a refusal."""
    stdout, _, reason = run
    try:
        candidate = _candidate(repo, praxis_home)
    except ValueError as exc:  # a malformed owner map, named by its path
        return _refuse(stdout, FREEZE, str(exc))
    cmap = _size(ledger, repo, praxis_home, candidate, run)
    if isinstance(cmap, int):
        return cmap
    frozen = ledger.component_map_frozen(repo.repo_id)
    old = None if frozen is None else from_json(frozen).version
    if old is None or old == cmap.version:
        return cmap, None
    if reason is None:
        return _refuse(
            stdout,
            FREEZE,
            f"map {old} is set. To change to map {cmap.version}, write --reason as one"
            f" of {_reasons()}.",
        )
    return cmap, old


def _size(
    ledger: Store,
    repo: Repo,
    praxis_home: Path,
    cmap: ComponentMap | None,
    run: tuple[TextIO, str, str | None],
) -> ComponentMap | int:
    """The map when it has 5 to 30 product components, else the exit code of a
    refusal. Too few is flagged; a mechanical map's draft is printed."""
    stdout, now, _ = run
    product = [] if cmap is None else product_components(cmap)
    if len(set(product)) != len(product):
        raise ValueError("a product component id is listed twice")
    if cmap is not None and MIN_COMPONENTS <= len(product) <= MAX_COMPONENTS:
        return cmap
    source = "mechanical" if cmap is None else cmap.source
    why = f"more than {MAX_COMPONENTS}"
    if len(product) < MIN_COMPONENTS:
        detail: dict[str, object] = {"source": source}
        if cmap is not None:
            detail["components"] = len(product)
        ledger.flag(
            ts=now, code="too_few_components", detail=detail, repo_id=repo.repo_id
        )
        why = f"fewer than {MIN_COMPONENTS}"
    code = _refuse(stdout, FREEZE, f"the {source} map has {why} product components.")
    if cmap is not None and source == "mechanical":
        stdout.write(to_toml(cmap))
        stdout.write(
            f"{FREEZE}: to write this draft to"
            f" {owner_path(praxis_home, repo.repo_id)}, run `{DERIVE} --write`."
            f" Merge or split components in that file. Then run `{FREEZE}` again.\n"
        )
    return code


def _tree(root: Path) -> tuple[str | None, bool]:
    """HEAD's tree hash (None before the first commit) and whether the working tree,
    which X1 read, differs from it."""
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{tree}", check=False)
    status = _git(root, "--no-optional-locks", "status", "--porcelain", "-z")
    return (head.strip() or None), bool(status)


def _git(root: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=check,
        timeout=GIT_TIMEOUT_S,
    )
    return done.stdout.decode("utf-8", "surrogateescape")


def _supply(
    graph: StaticGraph, product: list[str], place: Callable[[str], str]
) -> _Supply:
    # A blind_spots key is a component with at least one parsed file (extract.py).
    parsed = [c for c in product if c in graph.blind_spots]
    inside = set(parsed)
    edges = {
        (e.src, e.dst): e for e in graph.edges if e.src in inside and e.dst in inside
    }
    failed: dict[str, int] = dict.fromkeys(parsed, 0)
    for path in graph.parse_failures:
        if place(path) in inside:
            failed[place(path)] += 1
    notes = {
        c: [f"blind_spots:{c}:{graph.blind_spots[c]}"] * (graph.blind_spots[c] > 0)
        + [f"parse_failures:{c}:{failed[c]}"] * (failed[c] > 0)
        for c in parsed
    }
    return _Supply(parsed, edges, reach(graph, parsed), notes, graph.extractor_version)


def _put(
    ledger: Store,
    repo_id: str,
    cmap: ComponentMap,
    supply: _Supply,
    run: tuple[str, list[str], list[str]],
) -> tuple[int, int]:
    """Write the D1 items of every parsed pair that has none under this map;
    (written, already present)."""
    have = {
        (r[0], r[1])
        for r in ledger.db.execute(
            "SELECT src_component, dst_component FROM probe_items"
            " WHERE repo_id = ? AND map_version = ? AND relation = 'D1'",
            (repo_id, cmap.version),
        )
    }
    rows = [
        _row(supply, a, b, 0)
        for a in supply.parsed
        for b in supply.parsed
        if a != b and (a, b) not in have
    ]
    _write(ledger, repo_id, cmap, supply, run, rows)
    return len(rows), supply.pairs - len(rows)


_EPOCHS = (
    "SELECT src_component, dst_component, truth, epoch FROM probe_items p"
    " WHERE p.repo_id = ? AND p.map_version = ? AND p.relation = 'D1'"
    " AND p.epoch = (SELECT MAX(q.epoch) FROM probe_items q"
    "  WHERE q.repo_id = p.repo_id AND q.map_version = p.map_version"
    "  AND q.relation = 'D1' AND q.src_component = p.src_component"
    "  AND q.dst_component = p.dst_component)"
)


def readmit(
    ledger: Store,
    repo_id: str,
    cmap: ComponentMap,
    supply: _Supply,
    run: tuple[str, list[str], list[str]],
) -> int:
    """Design 3.5: an item is consumed by its first exposure and re-admitted as a new
    item only when its truth flips. Every D1 pair whose X1 truth now differs from the
    truth of its current epoch's item gets an item at epoch + 1; returns how many. A
    pair with a component X1 did not parse this time is unknown, so it is not compared.
    No exposure is read or written: the older epoch stays consumed, or unexposed, as
    it was, and the pool reads only the current epoch."""
    parsed = set(supply.parsed)
    # UNIQUE (repo, map, relation, src, dst, epoch): one current row per pair.
    flipped = [
        _row(supply, a, b, epoch + 1)
        for a, b, truth, epoch in ledger.db.execute(_EPOCHS, (repo_id, cmap.version))
        if a in parsed and b in parsed and bool(truth) != ((a, b) in supply.edges)
    ]
    _write(ledger, repo_id, cmap, supply, run, flipped)
    return len(flipped)


def _write(
    ledger: Store,
    repo_id: str,
    cmap: ComponentMap,
    supply: _Supply,
    run: tuple[str, list[str], list[str]],
    rows: list[Row],
) -> None:
    """One probe_items batch per distinct set of partial-coverage warnings."""
    now, sources, notes = run
    groups: dict[tuple[str, ...], list[Row]] = {}
    for row in rows:
        key = tuple(sorted({*supply.partial[row[1]], *supply.partial[row[2]]}))
        groups.setdefault(key, []).append(row)
    for key, batch in groups.items():
        ev = Evidence(
            "partial" if key else "full",
            sources,
            REDUCER_VERSION,
            ASSUMPTIONS,
            [*key, *notes],
        )
        ledger.probe_items_put(
            rows=batch,
            ev=ev,
            repo_id=repo_id,
            map_version=cmap.version,
            truth_ts=now,
            extractor_version=supply.extractor_version,
        )


def _row(supply: _Supply, a: str, b: str, epoch: int) -> Row:
    if (a, b) not in supply.edges:
        return ("D1", a, b, False, epoch, None, None)
    edge = supply.edges[a, b]
    return ("D1", a, b, True, epoch, edge.sites, edge.module_level)


def _report(
    cmap: ComponentMap,
    old: str | None,
    reason: str | None,
    supply: _Supply,
    product: list[str],
) -> list[str]:
    """The E03 figures for this map: pair pool, base rate, reach-through, gaps."""
    lines = [f"Warning: {w}" for w in cmap.warnings]
    if old is not None:
        lines.append(f"This map replaces map {old}. Reason: {reason}")
    pairs, edges = supply.pairs, len(supply.edges)
    rate = "unknown" if pairs == 0 else f"{edges / pairs:.3f}"
    through = len(supply.reach) - edges
    unknown = len(product) * (len(product) - 1) - pairs
    lines += [
        f"X1 {supply.extractor_version}: imports in {edges} of {pairs} ordered pairs"
        f" of components, base rate {rate}",
        f"  pairs with no direct import: {through} connect through a different"
        f" component, {pairs - edges - through} have no path",
        "  pairs with no item, because a component has no parsed Python file:"
        f" {unknown}",
    ]
    partial_ids = [c for c in supply.parsed if supply.partial[c]]
    if partial_ids:
        lines.append(
            "  components with partial coverage (a blind spot or a parse failure):"
            f" {' '.join(partial_ids)}"
        )
    return lines


_CURRENT = (
    "SELECT COUNT(*), COUNT(CASE WHEN truth = 1 THEN 1 END) FROM probe_items p"
    " WHERE p.repo_id = ? AND p.map_version = ? AND NOT EXISTS (SELECT 1 FROM"
    " probe_items q WHERE q.repo_id = p.repo_id AND q.map_version = p.map_version"
    " AND q.relation = p.relation AND q.src_component = p.src_component"
    " AND q.dst_component = p.dst_component AND q.epoch > p.epoch)"
)


def _counts(ledger: Store, repo_id: str) -> dict[str, int | None]:
    frozen = ledger.component_map_frozen(repo_id)
    if frozen is None:
        return dict.fromkeys(COUNT_KEYS)
    cmap = from_json(frozen)
    n = len(product_components(cmap))
    items, positive = ledger.db.execute(_CURRENT, (repo_id, cmap.version)).fetchone()
    unexposed = len(ledger.probe_items_unexposed(repo_id, cmap.version))
    return {
        "components": n,
        "pairs": n * (n - 1),
        "items": int(items),
        "positive": int(positive),
        "unexposed": unexposed,
        "tasks_left": unexposed // ITEMS_PER_TASK,
    }


def _supply_line(counts: dict[str, int | None]) -> str:
    if counts["items"] is None:
        return "Items: unknown (no set component map)"
    return (
        f"Items: {counts['items']} ({counts['positive']} true),"
        f" {counts['unexposed']} not used by a task, enough for"
        f" {counts['tasks_left']} tasks at {ITEMS_PER_TASK} items each"
    )


_UNTIL = {
    "owner": "add it to the owner map file. Then run",
    "mechanical": "run",
}


def _unmapped(root: Path, cmap: ComponentMap) -> list[str]:
    """G15: the share of tracked paths the frozen map does not assign, then the
    non-empty Python files no rule assigns in a directory that holds a mapped product
    file: a module added since the freeze, which an owner map or a grouped mechanical
    map leaves unmapped until it is mapped and refrozen."""
    try:
        listed = _git(root, "ls-files", "-z")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ["Tracked paths with no component: unknown (git cannot list them)"]
    placed = [(p, assign(cmap, p)) for p in listed.split("\0") if p]
    lost = [p for p, cid in placed if cid == UNMAPPED]
    product = set(product_components(cmap))
    mapped = {PurePosixPath(p).parent for p, cid in placed if cid in product}
    new = [
        p
        for p in lost
        if p.endswith(".py") and PurePosixPath(p).parent in mapped and _code(root / p)
    ]
    lines = [
        f"Tracked paths with no component: {len(lost)} of {len(placed)}",
        f"Python files with no component, beside files that have one: {len(new)}",
    ]
    lines += [f"  {p}" for p in new]
    if new:
        lines.append(
            f"  To give each file a component, {_UNTIL[cmap.source]}"
            f" `{FREEZE} --reason new_module`."
        )
    return lines


def _code(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:  # listed by git, gone from the working tree
        return False
