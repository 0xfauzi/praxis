"""E13 Rust adapter: crate edges from ``cargo metadata`` path dependencies, and the
files each target compiles from rustc dep-info against stub externs."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path

from shapes import (  # experiments/E13/shapes.py, beside this script on sys.path
    AdapterError,
    Oracle,
    Pair,
    head_of,
    listed_files,
    relative,
    run,
)


def _cargo_metadata(repo: Path) -> dict[str, object]:
    argv = ["cargo", "metadata", "--no-deps", "--offline", "--format-version", "1"]
    done = run(argv, repo)
    if done.returncode != 0:
        raise AdapterError("cargo metadata failed")
    data: dict[str, object] = json.loads(done.stdout)
    return data


def _features(declared: dict[str, list[str]], start: list[str]) -> set[str]:
    """The feature closure cargo would enable from ``start``, within the crate."""
    enabled: set[str] = set()
    stack = list(start)
    while stack:
        name = stack.pop()
        if name in declared and name not in enabled:
            enabled.add(name)
            stack += declared[name]
    return enabled


def _crate_root(pkg: dict[str, object]) -> str:
    targets = pkg["targets"]
    assert isinstance(targets, list)
    libs = [t for t in targets if {"lib", "rlib", "proc-macro"} & set(t["kind"])]
    bins = [t for t in targets if "bin" in t["kind"]]
    chosen = (libs or bins or targets)[0]
    return str(chosen["src_path"])


def parse_dep_info(text: str) -> list[str]:
    """The prerequisites of the first rule in a Makefile-style .d file."""
    first = text.split("\n", 1)[0]
    _, _, deps = first.partition(": ")
    words = re.split(r"(?<!\\) ", deps.strip())
    return [w.replace("\\ ", " ") for w in words if w]


def _extern_names(pkg: dict[str, object]) -> set[str]:
    """Crate names the package's sources may name with ``extern crate``: every
    declared dependency, and the package's own lib for its bins and tests."""
    deps = pkg["dependencies"]
    assert isinstance(deps, list)
    names = {str(d.get("rename") or d["name"]).replace("-", "_") for d in deps}
    return names | {str(pkg["name"]).replace("-", "_")}


def _stubs(packages: list[dict[str, object]], tmp: Path) -> dict[str, Path]:
    """An empty rlib per extern name, so ``extern crate`` loads and rustc gets past
    expansion to write dep-info without any dependency being built or run."""
    stubs = tmp / "stubs"
    stubs.mkdir()
    empty = stubs / "empty.rs"
    empty.write_text("")
    names = set().union(*(_extern_names(p) for p in packages)) if packages else set()
    found: dict[str, Path] = {}
    for name in sorted(names):
        out = stubs / f"lib{name}.rlib"
        argv = ["rustc", "--crate-type", "rlib", "--crate-name", name, "-o", str(out)]
        if run([*argv, str(empty)], stubs).returncode == 0:
            found[name] = out
    return found


def _rustc_argv(
    pkg: dict[str, object], target: dict[str, object], externs: dict[str, Path]
) -> list[str]:
    declared = pkg["features"]
    assert isinstance(declared, dict)
    kinds = target["kind"]
    assert isinstance(kinds, list)
    argv = ["rustc", "--edition", str(pkg["edition"])]
    argv += ["--crate-type", "bin" if "bin" in kinds else "lib", "--crate-name", "e13"]
    for feature in sorted(_features(declared, ["default"])):
        argv += ["--cfg", f'feature="{feature}"']
    for name in sorted(_extern_names(pkg)):
        argv += ["--extern", f"{name}={externs[name]}"] if name in externs else []
    return argv


def _rustc_files(
    pkg: dict[str, object],
    target: dict[str, object],
    tmp: Path,
    externs: dict[str, Path],
) -> tuple[list[str] | None, int]:
    """Source files rustc loads for one target, union of the normal and --test cfg,
    and how many of the two rustc runs exited non-zero."""
    argv = _rustc_argv(pkg, target, externs)
    files: set[str] = set()
    failed = 0
    for extra in ([], ["--test"]):
        out = tmp / "dep.d"
        out.unlink(missing_ok=True)
        emit = ["--emit=dep-info", "-o", str(out), str(target["src_path"])]
        done = run([*argv, *extra, *emit], tmp)
        failed += done.returncode != 0
        if not out.is_file():
            return None, failed
        files |= set(parse_dep_info(out.read_text()))
    return sorted(files), failed


def _rust_members(
    packages: list[dict[str, object]], repo: Path, tmp: Path
) -> tuple[dict[str, set[str]], int, int]:
    """Package name -> source files its targets compile (build scripts excluded)."""
    externs = _stubs(packages, tmp)
    members: dict[str, set[str]] = {}
    missing = nonzero = 0
    for pkg in packages:
        files: set[str] = set()
        targets = pkg["targets"]
        assert isinstance(targets, list)
        for target in (t for t in targets if "custom-build" not in t["kind"]):
            got, failed = _rustc_files(pkg, target, tmp, externs)
            nonzero += failed
            missing += got is None
            files |= {r for r in (relative(f, repo) for f in got or []) if r}
        members[str(pkg["name"])] = files
    return members, missing, nonzero


def _manifest_dir(pkg: dict[str, object], repo: Path) -> str:
    return relative(str(Path(str(pkg["manifest_path"])).parent), repo) or "."


def _path_deps(
    pkg: dict[str, object], by_dir: dict[str, dict[str, object]]
) -> tuple[list[tuple[dict[str, object], str]], int]:
    """(workspace package, dependency kind) per path dependency, and how many path
    dependencies point outside the workspace."""
    deps = pkg["dependencies"]
    assert isinstance(deps, list)
    found: list[tuple[dict[str, object], str]] = []
    outside = 0
    for dep in (d for d in deps if d.get("path") is not None):
        target = by_dir.get(os.path.realpath(dep["path"]))
        if target is None:
            outside += 1
        else:
            found.append((target, str(dep.get("kind") or "normal")))
    return found, outside


def _rust_crate_edges(
    packages: list[dict[str, object]], repo: Path
) -> tuple[set[Pair], set[Pair], Counter[str], int]:
    """Crate root -> dependency crate root over path dependencies inside the
    workspace, the same between manifest directories, kinds, and path deps that
    leave the workspace."""
    by_dir = {
        os.path.realpath(Path(str(p["manifest_path"])).parent): p for p in packages
    }
    files: set[Pair] = set()
    dirs: set[Pair] = set()
    kinds: Counter[str] = Counter()
    outside = 0
    for pkg in packages:
        found, away = _path_deps(pkg, by_dir)
        outside += away
        for target, kind in found:
            kinds[kind] += 1
            a, b = relative(_crate_root(pkg), repo), relative(_crate_root(target), repo)
            if a and b and a != b:
                files.add((a, b))
            dirs.add((_manifest_dir(pkg, repo), _manifest_dir(target, repo)))
    return files, {(s, d) for s, d in dirs if s != d}, kinds, outside


def rust_oracle(repo: Path) -> Oracle:
    listed = listed_files(repo)
    start = time.perf_counter()
    meta = _cargo_metadata(repo)
    packages = meta["packages"]
    assert isinstance(packages, list)
    files, dirs, kinds, outside = _rust_crate_edges(packages, repo)
    with tempfile.TemporaryDirectory(prefix="e13-rs-") as tmp:
        members, missing, nonzero = _rust_members(packages, repo, Path(tmp))
    seconds = time.perf_counter() - start
    if missing:
        raise AdapterError(f"rustc wrote no dep-info for {missing} targets")
    in_crates = set().union(*members.values()) if members else set()
    rs_files = {p for p in listed if p.endswith(".rs")}
    detail: dict[str, object] = {
        "crates": len(packages),
        "dep_kinds": dict(kinds),
        "rs_files_in_crates": len(rs_files & in_crates),
        "other_files_in_crates": len(in_crates - rs_files),
        "rs_in_no_crate": sorted(rs_files - in_crates),
        "rs_files_listed": len(rs_files),
        "rs_files_in_no_crate": len(rs_files - in_crates),
        "rustc_nonzero_runs": nonzero,
        "members": {k: len(v) for k, v in sorted(members.items())},
        "edge_level": "crate root -> crate root (path dependencies)",
    }
    return Oracle(
        "rust",
        repo.name,
        head_of(repo),
        sorted(files),
        sorted(dirs),
        outside,
        seconds,
        detail,
    )
