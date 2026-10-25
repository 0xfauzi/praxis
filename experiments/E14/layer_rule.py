"""E14 supplement: how much of an L3 catch item does the layer order alone answer?

Descriptive, no gate; written before its first run (2026-10-25). For each repository,
at HEAD in a scratch clone (deleted at exit), over the ordered pairs (A, B) of product
components that hold a non-empty Python file, with no X1 edge A -> B and not already in
one cycle (the L3 item pool of faults.py): a pair is a fault when B reaches A, else a
decoy. A component's layer is the longest path from it down to a sink over the
condensation of the X1 graph. The layer rule says "wrong" (0.9) when layer(B) > layer(A)
and "clean" (0.1) otherwise. Printed per repository: the pool by label, the decoys the
rule calls wrong (layer-incongruent decoys), and the rule's Brier score on faults and on
decoys. A fault always has layer(B) > layer(A), because B reaches A, so the rule can
miss only on decoys. A first run counted components with no Python file and gave
systemap a larger pool than faults.py draws from; the pool was aligned and the run
repeated.

Usage: uv run python experiments/E14/layer_rule.py REPO [REPO ...]
"""

# ruff: noqa: T201 - a runner's printed output is the record its write-up quotes.
from __future__ import annotations

import shutil
import sys
import tempfile
from functools import partial
from pathlib import Path

from praxis import components as comp
from praxis.extract import _python_files, reach, scratch_clone, static_imports


def layers(nodes: list[str], closure: set[tuple[str, str]]) -> dict[str, int]:
    """Longest path down to a sink; members of one cycle share a layer."""
    above = {
        n: {m for m in nodes if (n, m) in closure and (m, n) not in closure}
        for n in nodes
    }
    memo: dict[str, int] = {}

    def depth(n: str) -> int:
        if n not in memo:
            memo[n] = 1 + max((depth(m) for m in above[n]), default=-1)
        return memo[n]

    return {n: depth(n) for n in nodes}


def _with_python(clone: Path, cmap: comp.ComponentMap) -> set[str]:
    """Components holding a non-empty Python file: the components faults.py draws."""
    return {
        comp.assign(cmap, p)
        for p in _python_files(clone)
        if (clone / p).read_bytes().strip()
    }


def measure(repo: Path, home: Path) -> str:
    clone = scratch_clone(repo, home)
    try:
        cmap = comp.derive(clone, "e14")
        product = set(comp.product_components(cmap))
        nodes = sorted(product & _with_python(clone, cmap))
        graph = static_imports(clone, partial(comp.assign, cmap))
        edges = {(e.src, e.dst) for e in graph.edges}
        closure = reach(graph, nodes)
    finally:
        shutil.rmtree(clone, ignore_errors=True)
    layer = layers(nodes, closure)
    faults = decoys = incongruent = 0
    for a in nodes:
        for b in nodes:
            both = (a, b) in closure and (b, a) in closure
            if a == b or (a, b) in edges or both:
                continue
            if (b, a) in closure:
                faults += 1
            else:
                decoys += 1
                incongruent += layer[b] > layer[a]
    rule_decoy = (incongruent * 0.81 + (decoys - incongruent) * 0.01) / max(decoys, 1)
    return (
        f"{repo.name}: faults {faults}, decoys {decoys}, layer-incongruent decoys"
        f" {incongruent} ({incongruent / max(decoys, 1):.2f}); layer-rule Brier on"
        f" faults 0.01, on decoys {rule_decoy:.3f}"
    )


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="e14-layers-"))
    try:
        for arg in sys.argv[1:]:
            print(measure(Path(arg).resolve(), home))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
