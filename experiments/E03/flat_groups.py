"""E03 addendum: does the flat-package rule give a flat repository 5 to 30 components
the owner would recognise?

Acceptance rule, written before the run (2026-10-19). For each repository named on the
command line, at its committed HEAD (a `git clone --local` under a temporary
$PRAXIS_HOME, deleted at exit), `components.derive` is run as praxis ships it. Where the
depth-1 cut gives more than 30 components, derive groups the product root's children
by the name token before the first underscore and attaches each one-file group to the
multi-file group it shares the most import edges with (unique maximum, to a fixed
point).

  PASS iff (a) telltale and systemap each land in 5 to 34 module groups, a module group
  being a product component holding at least one Python file (34 is the count the
  measurement behind the rule gave systemap; freeze still refuses above 30 and prints
  the draft for the owner to merge), and (b) systemap's module-weighted purity against
  its hand map is at least 0.75.

Purity: each Python file directly under the product root that the repository's hand
map (the `implemented_by` module ids of the `Component(...)` calls in map/model.py, read
with `ast` here and only here, never by praxis) assigns gets that hand component as its
label.
A group's purity is its most common label's share of its labelled files; module-weighted
purity is the sum of those top counts over the sum of labelled files. The full product
component count, which also holds non-Python children (py.typed, a data directory), is
printed beside the module groups and is what freeze checks.

Prediction, written before the run: telltale about 17 module groups, systemap about 34,
systemap purity about 0.78, and praxis unchanged at depth 1 with no grouping warning.

Usage: uv run python experiments/E03/flat_groups.py REPO [REPO ...]
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

from praxis import components as comp
from praxis.extract import scratch_clone
from praxis.unassisted import locate

LOW, HIGH, PURITY = 5, 34, 0.75


def hand_labels(root: Path, top: tuple[str, ...]) -> dict[str, str]:
    """Path -> hand component, for the modules map/model.py's components name."""
    model = root / "map" / "model.py"
    if not model.is_file() or not top:
        return {}
    labels: dict[str, str] = {}
    for node in ast.walk(ast.parse(model.read_bytes())):
        if not (
            isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Component"
        ):
            continue
        fields = {kw.arg: kw.value for kw in node.keywords}
        if "id" not in fields or "implemented_by" not in fields:
            continue
        cid = ast.literal_eval(fields["id"])
        for module in ast.literal_eval(fields["implemented_by"]):
            rel = PurePosixPath(*top, *module.split(".")[1:])
            for path in (rel.with_suffix(".py"), rel / comp.INIT):
                labels[path.as_posix()] = cid
    return labels


def purity(groups: dict[str, list[str]], labels: dict[str, str]) -> tuple[float, float]:
    """(mean per group, module-weighted) over groups with a labelled file."""
    tops = [
        (Counter(labels[p] for p in paths if p in labels).most_common(1), paths)
        for paths in groups.values()
    ]
    pairs = [(top[0][1], sum(p in labels for p in paths)) for top, paths in tops if top]
    mean = sum(k / n for k, n in pairs) / len(pairs)
    return mean, sum(k for k, _ in pairs) / sum(n for _, n in pairs)


def measure(repo: Path, home: Path) -> dict[str, object]:
    found = locate(repo)
    if found is None:
        raise SystemExit(f"{repo}: not a git repository")
    root = scratch_clone(found.root, home)
    try:
        return _measure(root, found.repo_id)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _measure(root: Path, repo_id: str) -> dict[str, object]:
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    paths = comp._ls_files(root)
    product = [p for p in paths if comp._match(comp.FIXED, p) is None]
    top = comp._product_root(product)
    cmap = comp.derive(root, repo_id, paths)
    names = comp.product_components(cmap)
    files: dict[str, list[str]] = {c: [] for c in names}
    for p in product:
        if comp.assign(cmap, p) in files:
            files[comp.assign(cmap, p)].append(p)
    modules = {c: ps for c, ps in files.items() if any(p.endswith(".py") for p in ps)}
    root_py = [p for p in product if PurePosixPath(p).parent.parts == top]
    labels = hand_labels(root, top)
    result: dict[str, object] = {
        "head": head,
        "product_root": "/".join(top) or ".",
        "python_files_in_root": sum(p.endswith(".py") for p in root_py),
        "warnings": list(cmap.warnings),
        "product_components": len(names),
        "module_groups": len(modules),
        "one_file_groups": sorted(c for c, ps in modules.items() if len(ps) == 1),
        "largest": sorted(((len(ps), c) for c, ps in modules.items()), reverse=True)[
            :10
        ],
    }
    if comp.GROUPED in cmap.warnings:
        result.update(_prefix_stage(root, top, paths))
    if labels:
        root_groups = {c: [p for p in ps if p in root_py] for c, ps in modules.items()}
        mean, weighted = purity(root_groups, labels)
        result |= {
            "hand_components": len(set(labels.values())),
            "labelled_files": sum(p in labels for p in root_py),
            "purity_mean": round(mean, 3),
            "purity_weighted": round(weighted, 3),
        }
    return result


def _prefix_stage(
    root: Path, top: tuple[str, ...], paths: list[str]
) -> dict[str, object]:
    """The prefix groups before any one-file group is attached."""
    files = comp._files(root, paths, top)
    rules = comp._cut(files, top, 1)
    sizes = comp._sizes(rules, files)
    groups = comp._by_prefix(rules)
    multi = [t for t, us in groups.items() if sum(sizes[u] for u in us) > 1]
    return {
        "children_at_depth_1": len(rules),
        "prefix_groups": len(groups),
        "prefix_multi": len(multi),
        "prefix_one_file": len(groups) - len(multi),
    }


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    home = Path(tempfile.mkdtemp(prefix="e03-flat-"))
    try:
        results = {Path(a).name: measure(Path(a), home) for a in argv}
    finally:
        shutil.rmtree(home, ignore_errors=True)
    for name, r in results.items():
        print(f"== {name}")
        for key, value in r.items():
            print(f"  {key}: {value}")
    flat = [results[n] for n in ("telltale", "systemap") if n in results]
    counts = [int(str(r["module_groups"])) for r in flat]
    a = len(flat) == 2 and all(LOW <= n <= HIGH for n in counts)
    weighted = results.get("systemap", {}).get("purity_weighted")
    b = isinstance(weighted, float) and weighted >= PURITY
    print(f"== (a) telltale and systemap in {LOW} to {HIGH} module groups: {a}")
    print(f"== (b) systemap module-weighted purity >= {PURITY}: {b}")
    print(f"== rule: {'PASS' if a and b else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
