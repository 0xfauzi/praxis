"""Item supply: `praxis components freeze` fixes a repository's component map and fills
its D1 pool; `praxis components` shows the map and what the pool has left (design 03
sections 2.4, 3.1 and 3.5).

A D1 item exists only for an ordered pair of product components that each have a
Python file X1 parsed: a component it never parsed is unknown, so its pairs are counted
as not generated and never written as truth 0. An item touching a component with a
blind spot or a parse failure is `partial`. Truth is X1 over the working tree at
freeze time, epoch 0. A pair that already has an item under the map keeps it, so a
second freeze writes only the missing pairs and a partial first run is completed, not
duplicated; a truth flip is a new epoch, which is not this module's to write. Git is
only read: `--no-optional-locks` keeps `git status` from refreshing the owner's index.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, TextIO

from praxis.components import (
    MIN_COMPONENTS,
    RESERVED,
    UNMAPPED,
    ComponentMap,
    TooFewComponents,
    assign,
    derive,
    from_json,
    load_owner,
    product_components,
    to_json,
)
from praxis.extract import GIT_TIMEOUT_S, Edge, StaticGraph, reach, static_imports
from praxis.schema import MAP_REASONS
from praxis.store import Evidence, Store
from praxis.task import K_E, K_P, K_R
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
FREEZE = "praxis components freeze"
SHOW = "praxis components"
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
        return _refuse(stdout, FREEZE, "not inside a git repository")
    if reason is not None and reason not in MAP_REASONS:
        return _refuse(stdout, FREEZE, f"--reason is one of {' '.join(MAP_REASONS)}")
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        return _freeze(ledger, repo, praxis_home, (stdout, now, reason))
    finally:
        ledger.close()


def show(repo_root: Path, praxis_home: Path, stdout: TextIO) -> int:
    repo = locate(repo_root)
    if repo is None:
        return _refuse(stdout, SHOW, "not inside a git repository")
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
            f"{SHOW}: no frozen map for repository {repo.repo_id};"
            f" `{FREEZE}` derives and freezes one\n"
        )
        return 0
    cmap = from_json(frozen)
    stdout.write(
        f"map {cmap.version} ({cmap.source}),"
        f" {len(product_components(cmap))} product components\n"
    )
    for rule in cmap.rules:
        fixed = " (fixed class)" if rule.id in RESERVED else ""
        stdout.write(f"  {rule.id}{fixed}: {' '.join(rule.globs)}\n")
    for warning in cmap.warnings:
        stdout.write(f"warning: {warning}\n")
    stdout.write(_unmapped(repo.root, cmap) + "\n")
    stdout.write(_supply_line(counts) + "\n")
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
        return _refuse(stdout, FREEZE, "git could not list the working tree")
    try:
        ledger.component_map_put(to_json(cmap), now)
        ledger.component_map_freeze(
            repo.repo_id, cmap.version, now, reason=None if old is None else reason
        )
    except sqlite3.IntegrityError:
        return _refuse(
            stdout, FREEZE, f"map {cmap.version} was superseded; it cannot be refrozen"
        )
    product = product_components(cmap)
    supply = _supply(graph, product, partial(assign, cmap))
    sources = [f"extractor:{graph.extractor_version}", f"map:{cmap.version}"]
    sources += [] if tree is None else [f"tree:{tree}"]
    notes = ["no_commit"] * (tree is None) + ["working_tree_dirty"] * dirty
    written, present = _put(ledger, repo.repo_id, cmap, supply, (now, sources, notes))
    stdout.write(
        f"map {cmap.version} frozen ({cmap.source}),"
        f" {len(product)} product components\n"
    )
    for line in _report(cmap, old, reason, supply, product):
        stdout.write(line + "\n")
    stdout.write(f"D1 items: {written} written at epoch 0, {present} already present\n")
    stdout.write(_supply_line(_counts(ledger, repo.repo_id)) + "\n")
    return 0


def _accept(
    ledger: Store,
    repo: Repo,
    praxis_home: Path,
    run: tuple[TextIO, str, str | None],
) -> tuple[ComponentMap, str | None] | int:
    """The map to freeze and the frozen version it replaces (None when it replaces
    nothing), or the exit code of a refusal. Too few components is flagged."""
    stdout, now, reason = run
    try:
        cmap = _candidate(repo, praxis_home)
    except ValueError as exc:  # a malformed owner map, named by its path
        return _refuse(stdout, FREEZE, str(exc))
    product = [] if cmap is None else product_components(cmap)
    if len(set(product)) != len(product):
        raise ValueError("a product component id is listed twice")
    if cmap is None or len(product) < MIN_COMPONENTS:
        detail: dict[str, object] = {"source": "mechanical"}
        if cmap is not None:
            detail = {"source": cmap.source, "components": len(product)}
        ledger.flag(
            ts=now, code="too_few_components", detail=detail, repo_id=repo.repo_id
        )
        return _refuse(
            stdout, FREEZE, f"fewer than {MIN_COMPONENTS} product components"
        )
    frozen = ledger.component_map_frozen(repo.repo_id)
    old = None if frozen is None else from_json(frozen).version
    if old is None or old == cmap.version:
        return cmap, None
    if reason is None:
        return _refuse(
            stdout,
            FREEZE,
            f"map {old} is frozen; changing to {cmap.version} needs --reason,"
            f" one of {' '.join(MAP_REASONS)}",
        )
    return cmap, old


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
    """Write the D1 items of every parsed pair that has none under this map, one
    batch per distinct set of partial-coverage warnings; (written, already present)."""
    now, sources, notes = run
    have = {
        (r[0], r[1])
        for r in ledger.db.execute(
            "SELECT src_component, dst_component FROM probe_items"
            " WHERE repo_id = ? AND map_version = ? AND relation = 'D1'",
            (repo_id, cmap.version),
        )
    }
    groups: dict[tuple[str, ...], list[Row]] = {}
    for a in supply.parsed:
        for b in supply.parsed:
            if a == b or (a, b) in have:
                continue
            key = tuple(sorted({*supply.partial[a], *supply.partial[b]}))
            groups.setdefault(key, []).append(_row(supply, a, b))
    for key, rows in groups.items():
        ev = Evidence(
            "partial" if key else "full",
            sources,
            REDUCER_VERSION,
            ASSUMPTIONS,
            [*key, *notes],
        )
        ledger.probe_items_put(
            rows=rows,
            ev=ev,
            repo_id=repo_id,
            map_version=cmap.version,
            truth_ts=now,
            extractor_version=supply.extractor_version,
        )
    written = sum(map(len, groups.values()))
    return written, supply.pairs - written


def _row(supply: _Supply, a: str, b: str) -> Row:
    if (a, b) not in supply.edges:
        return ("D1", a, b, False, 0, None, None)
    edge = supply.edges[a, b]
    return ("D1", a, b, True, 0, edge.sites, edge.module_level)


def _report(
    cmap: ComponentMap,
    old: str | None,
    reason: str | None,
    supply: _Supply,
    product: list[str],
) -> list[str]:
    """The E03 figures for this map: pair pool, base rate, reach-through, gaps."""
    lines = [f"warning: {w}" for w in cmap.warnings]
    if old is not None:
        lines.append(f"superseded map {old} ({reason})")
    pairs, edges = supply.pairs, len(supply.edges)
    rate = "unknown" if pairs == 0 else f"{edges / pairs:.3f}"
    through = len(supply.reach) - edges
    unknown = len(product) * (len(product) - 1) - pairs
    lines += [
        f"X1 {supply.extractor_version}: {edges} edges among {pairs} ordered pairs,"
        f" base rate {rate}",
        f"  non-edge pairs: {through} reach through another component,"
        f" {pairs - edges - through} no path",
        f"  pairs not generated (a component with no parsed Python file): {unknown}",
    ]
    partial_ids = [c for c in supply.parsed if supply.partial[c]]
    if partial_ids:
        lines.append(
            f"  partial (blind spot or parse failure): {' '.join(partial_ids)}"
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
        return "pool: unknown (no frozen map)"
    return (
        f"pool: {counts['items']} items ({counts['positive']} true),"
        f" {counts['unexposed']} unexposed, {counts['tasks_left']} tasks at"
        f" {ITEMS_PER_TASK} items per task"
    )


def _unmapped(root: Path, cmap: ComponentMap) -> str:
    """G15: the share of tracked paths the frozen map no longer assigns."""
    try:
        listed = _git(root, "ls-files", "-z")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unmapped tracked paths: unknown (git could not list them)"
    paths = [p for p in listed.split("\0") if p]
    lost = sum(assign(cmap, p) == UNMAPPED for p in paths)
    return f"unmapped tracked paths: {lost} of {len(paths)}"
