"""E13 Java adapter: javac into a scratch directory, class files mapped back to sources
through their SourceFile attribute, and ``jdeps -verbose:class`` for the edges."""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path, PurePosixPath

from shapes import (  # experiments/E13/shapes.py, beside this script on sys.path
    AdapterError,
    Oracle,
    Pair,
    head_of,
    keep_listed,
    listed_files,
    run,
    to_dirs,
)

_CP_SIZES = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4}
_CP_SIZES |= {15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}
_JDEPS = re.compile(r"^\s+(\S+)\s+->\s+(\S+)\s+(\S+)\s*$")


def _constant_pool(data: bytes) -> tuple[dict[int, object], int]:
    """Index -> utf8 string or class name index, and the offset after the pool."""
    count = int.from_bytes(data[8:10], "big")
    pool: dict[int, object] = {}
    at, index = 10, 1
    while index < count:
        tag = data[at]
        if tag == 1:
            size = int.from_bytes(data[at + 1 : at + 3], "big")
            pool[index] = data[at + 3 : at + 3 + size].decode("utf-8", "replace")
            at += 3 + size
        else:
            if tag == 7:
                pool[index] = int.from_bytes(data[at + 1 : at + 3], "big")
            at += 1 + _CP_SIZES[tag]
        index += 2 if tag in (5, 6) else 1
    return pool, at


def _skip_members(data: bytes, at: int) -> int:
    """Skip a fields or methods table: each entry is 6 bytes then attributes."""
    count = int.from_bytes(data[at : at + 2], "big")
    at += 2
    for _ in range(count):
        at = _skip_attributes(data, at + 6)
    return at


def _skip_attributes(data: bytes, at: int) -> int:
    count = int.from_bytes(data[at : at + 2], "big")
    at += 2
    for _ in range(count):
        at += 6 + int.from_bytes(data[at + 2 : at + 6], "big")
    return at


def class_source(data: bytes) -> tuple[str, str | None]:
    """(binary class name, SourceFile attribute) read from a class file."""
    pool, at = _constant_pool(data)
    this = pool[int.from_bytes(data[at + 2 : at + 4], "big")]
    name = str(pool[int(str(this))])
    interfaces = int.from_bytes(data[at + 6 : at + 8], "big")
    at = _skip_members(data, _skip_members(data, at + 8 + 2 * interfaces))
    count = int.from_bytes(data[at : at + 2], "big")
    at += 2
    for _ in range(count):
        size = int.from_bytes(data[at + 2 : at + 6], "big")
        if pool.get(int.from_bytes(data[at : at + 2], "big")) == "SourceFile":
            return name, str(pool[int.from_bytes(data[at + 6 : at + 8], "big")])
        at += 6 + size
    return name, None


def _java_roots(listed: set[str]) -> list[str]:
    roots = sorted(
        {p.split("/src/main/java/")[0] for p in listed if "/src/main/java/" in p}
    )
    found = [f"{r}/src/main/java" for r in roots]
    found += (
        ["src/main/java"] if any(p.startswith("src/main/java/") for p in listed) else []
    )
    return sorted(set(found))


def _class_files(
    classes: Path, roots: list[str], repo: Path
) -> tuple[dict[str, str], int]:
    """Class name (dotted) -> repository-relative source file, and classes with none."""
    found: dict[str, str] = {}
    unmapped = 0
    for path in classes.rglob("*.class"):
        name, source = class_source(path.read_bytes())
        package = PurePosixPath(name).parent.as_posix()
        hits = [f"{r}/{package}/{source}" for r in roots if source]
        hit = next((h for h in hits if (repo / h).is_file()), None)
        if hit is None:
            unmapped += 1
            continue
        found[name.replace("/", ".")] = hit
    return found, unmapped


def _javac(repo: Path, sources: list[str], classes: Path) -> dict[str, int]:
    args = classes.parent / "sources.txt"
    args.write_text("\n".join(str(repo / s) for s in sources))
    argv = ["javac", "-d", str(classes), "-proc:none", "-nowarn", "-encoding", "UTF-8"]
    done = run([*argv, "-implicit:none", f"@{args}"], repo)
    if not any(classes.rglob("*.class")):
        raise AdapterError("javac wrote no class file")
    errors = re.search(r"^(\d+) errors?$", done.stderr, re.MULTILINE)
    return {
        "javac_exit": done.returncode,
        "javac_errors": int(errors[1]) if errors else 0,
    }


def _jdeps(classes: Path, names: dict[str, str]) -> set[Pair]:
    argv = ["jdeps", "-verbose:class", "-filter:none", str(classes)]
    done = run(argv, classes.parent)
    if done.returncode != 0:
        raise AdapterError("jdeps failed")
    edges: set[Pair] = set()
    for line in done.stdout.splitlines():
        m = _JDEPS.match(line)
        if m and m[1] in names and m[2] in names and names[m[1]] != names[m[2]]:
            edges.add((names[m[1]], names[m[2]]))
    return edges


def java_oracle(repo: Path) -> Oracle:
    listed = listed_files(repo)
    roots = _java_roots(listed)
    sources = sorted(
        p
        for p in listed
        if p.endswith(".java") and any(p.startswith(r + "/") for r in roots)
    )
    if not sources:
        raise AdapterError("no src/main/java source")
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="e13-java-") as tmp:
        classes = Path(tmp, "classes")
        counts = _javac(repo, sources, classes)
        names, unmapped = _class_files(classes, roots, repo)
        edges = _jdeps(classes, names)
    seconds = time.perf_counter() - start
    detail: dict[str, object] = {
        "source_roots": roots,
        "sources": len(sources),
        "classes": len(names) + unmapped,
        "source_files_with_class": len(set(names.values())),
        **counts,
    }
    kept, dropped = keep_listed(edges, listed)
    detail["dropped_unlisted"] = dropped
    return Oracle(
        "java", repo.name, head_of(repo), kept, to_dirs(kept), unmapped, seconds, detail
    )
