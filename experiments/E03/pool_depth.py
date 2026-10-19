"""E03: how deep does the mechanical map cut, and how many D1 items does each repository
supply?

Acceptance rule, written before the run (2026-10-19). For each repository named on the
command line, at its committed HEAD (a `git clone --local` under a temporary
$PRAXIS_HOME, deleted at exit, so no owner tree is read beyond `git clone`):

  A repository can supply the confirmatory primary on its own iff
    (a) `components.derive` gives 5 to 30 product components with no warning, and
    (b) its full-coverage D1 pool holds at least 216 items: 54 tasks at 4 items per task
        (k_e + k_r + k_p), the registered-task count design 6.6 gives for 80 percent
        power at g = 0.54.
  The repositories pooled supply it iff their full-coverage pools sum to 216; the
  g = 0.28 count (200 tasks, 800 items) is reported as reached or not, and is not part
  of the rule.

A D1 item is an ordered pair of product components that both have a Python file X1
parsed; it is full-coverage when neither component has a blind spot or a parse failure.
D2 is reach without a direct edge, over the non-edge pairs. Also printed, descriptive
only: the components at every depth, the share of components that are one file, and the
in-sample Brier of R_base (the D1 density) and of a layer rule (forward pairs get the
forward edge rate snapped to the grid, backward pairs 0.1), which bounds how much
layering alone knows. Prediction, written before the run: praxis alone fails (b).

Usage: uv run python experiments/E03/pool_depth.py [--worktree] REPO [REPO ...]
(--worktree reads the working tree in place instead of HEAD; supplementary only.)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from praxis import components as comp
from praxis.extract import StaticGraph, reach, scratch_clone, static_imports
from praxis.scoring import GRID, snap
from praxis.unassisted import locate

ITEMS_PER_TASK = 4
Pool = dict[str, Any]
TASKS_G054, TASKS_G028 = 54, 200
NEED = TASKS_G054 * ITEMS_PER_TASK


def listed(root: Path, worktree: bool) -> list[str]:
    """HEAD's files in a clone; with --worktree, tracked plus untracked-not-ignored."""
    extra = ["--others", "--exclude-standard"] if worktree else []
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", *extra],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted({p for p in out.split("\0") if p and (root / p).is_file()})


def depth_profile(root: Path, paths: list[str]) -> tuple[tuple[str, ...], list[int]]:
    """Product components at every depth, as `components.derive` cuts them."""
    product = [p for p in paths if comp._match(comp.FIXED, p) is None]
    top = comp._product_root(product)
    files = [
        PurePosixPath(p).parts
        for p in product
        if PurePosixPath(p).parts[: len(top)] == top
        and len(PurePosixPath(p).parts) > len(top)
        and not (PurePosixPath(p).name == comp.INIT and comp._empty(root / p))
    ]
    deepest = max((len(f) - len(top) for f in files), default=0)
    return top, [len(comp._cut(files, top, d)) for d in range(1, deepest + 1)]


def layers(nodes: list[str], edges: set[tuple[str, str]]) -> dict[str, int]:
    """Longest path down to a sink over the strongly connected condensation; the members
    of one cycle share a layer."""
    closure = reach_pairs(nodes, edges)
    scc = {
        n: frozenset(
            {n} | {m for m in nodes if (n, m) in closure and (m, n) in closure}
        )
        for n in nodes
    }
    memo: dict[frozenset[str], int] = {}

    def height(group: frozenset[str]) -> int:
        if group not in memo:
            below = {scc[b] for a, b in edges if a in group and b not in group}
            memo[group] = 1 + max(map(height, below), default=-1)
        return memo[group]

    return {n: height(scc[n]) for n in nodes}


def reach_pairs(nodes: list[str], edges: set[tuple[str, str]]) -> set[tuple[str, str]]:
    succ = {n: {b for a, b in edges if a == n} for n in nodes}
    out: set[tuple[str, str]] = set()
    for start in nodes:
        seen: set[str] = set()
        stack = list(succ[start])
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(succ[node])
        out |= {(start, n) for n in seen if n != start}
    return out


def layer_brier(nodes: list[str], edges: set[tuple[str, str]]) -> float:
    height = layers(nodes, edges)
    pairs = [(a, b) for a in nodes for b in nodes if a != b]
    forward = [(a, b) for a, b in pairs if height[a] >= height[b]]
    rate = snap(sum((a, b) in edges for a, b in forward) / len(forward))
    p = {pair: (rate if pair in forward else GRID[0]) for pair in pairs}
    return sum((p[pr] - (pr in edges)) ** 2 for pr in pairs) / len(pairs)


def scan(repo: Path, home: Path, worktree: bool) -> tuple[Any, ...]:
    """(HEAD, product root, components by depth, map, listed paths, X1 graph). With
    ``worktree`` the owner's tree is read in place (supplementary, never the rule)."""
    found = locate(repo)
    if found is None:
        raise SystemExit(f"{repo}: not a git repository")
    root = found.root if worktree else scratch_clone(found.root, home)
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        paths = listed(root, worktree)
        top, profile = depth_profile(root, paths)
        cmap = comp.derive(root, found.repo_id, paths)
        graph = static_imports(root, lambda p: comp.assign(cmap, p))
    finally:
        if not worktree:
            shutil.rmtree(root, ignore_errors=True)
    return head, top, profile, cmap, paths, graph


def measure(repo: Path, home: Path, worktree: bool) -> dict[str, object]:
    """The rule's inputs for one repository; ``d1_full`` is always an int."""
    head, top, profile, cmap, paths, graph = scan(repo, home, worktree)
    product = comp.product_components(cmap)
    files_per = Counter(comp.assign(cmap, p) for p in paths)
    found = pool(cmap, graph, product)
    fits = 5 <= len(product) <= 30 and not cmap.warnings
    return {
        "head": head,
        "product_root": "/".join(top) or ".",
        "components_by_depth": profile,
        "source": cmap.source,
        "warnings": list(cmap.warnings),
        "product": len(product),
        "single_file_share": sum(files_per[c] == 1 for c in product) / len(product),
        **found,
        "supplies_alone": fits and found["d1_full"] >= NEED,
    }


def coverage(
    cmap: comp.ComponentMap, graph: StaticGraph, product: list[str]
) -> tuple[list[str], set[str]]:
    """(components X1 parsed a file of, those with a blind spot or a parse failure)."""
    failed = Counter(comp.assign(cmap, p) for p in graph.parse_failures)
    parsed = [c for c in product if c in graph.blind_spots]
    return parsed, {c for c in parsed if graph.blind_spots[c] > 0 or failed[c] > 0}


def pool(cmap: comp.ComponentMap, graph: StaticGraph, product: list[str]) -> Pool:
    parsed, partial = coverage(cmap, graph, product)
    edges = {(e.src, e.dst) for e in graph.edges if e.src in parsed and e.dst in parsed}
    pairs = [(a, b) for a in parsed for b in parsed if a != b]
    full = [pr for pr in pairs if not (set(pr) & partial)]
    through = reach(graph, parsed) - edges
    base = len(edges) / len(pairs) if pairs else None
    return {
        "parsed": len(parsed),
        "partial_components": sorted(partial),
        "blind_spots": sum(graph.blind_spots.values()),
        "d1_pairs": len(pairs),
        "d1_positive": len(edges),
        "d1_full": len(full),
        "d1_full_positive": sum(pr in edges for pr in full),
        "d2_positive": len(through),
        "d2_negative": len(pairs) - len(edges) - len(through),
        "tasks_full": len(full) // ITEMS_PER_TASK,
        "brier_base": None if base is None else base * (1 - base),
        "brier_layer": layer_brier(parsed, edges) if pairs else None,
    }


def main(argv: list[str]) -> int:
    worktree = "--worktree" in argv
    repos = [a for a in argv if a != "--worktree"]
    if not repos:
        raise SystemExit(__doc__)
    home = Path(tempfile.mkdtemp(prefix="e03-"))
    try:
        results = {Path(a).name: measure(Path(a), home, worktree) for a in repos}
    finally:
        shutil.rmtree(home, ignore_errors=True)
    for name, r in results.items():
        print(f"== {name}")
        for key, value in r.items():
            print(f"  {key}: {value}")
    total = sum(int(str(r["d1_full"])) for r in results.values())
    print(f"== pooled full-coverage D1 items: {total}")
    print(f"  pooled >= {NEED} (g = 0.54): {total >= NEED}")
    print(
        f"  pooled >= {TASKS_G028 * ITEMS_PER_TASK} (g = 0.28): "
        f"{total >= TASKS_G028 * ITEMS_PER_TASK}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
