"""E13: toolchain oracles for import edges, and an edge-for-edge comparison against X1.

Each language's own toolchain already knows the module graph. Every adapter here asks
it, and returns one shape: an ``Oracle`` holding a sorted list of (src, dst)
repository-relative POSIX paths at file level, the same at directory level, and a count
of in-repository references it could not resolve to a file. External packages are
dropped. ``compare`` scores a praxis extractor's edges against an oracle's.

Acceptance rule, written before the first run (2026-10-19).

Python (gated). For each of praxis, telltale and systemap at its committed HEAD, read
from a fresh ``git clone`` under the scratch directory with its own ``uv sync --frozen``
(deleted at exit), the oracle is the set of distinct (importer file, imported file)
pairs that the tracer records while the repository's own test suite runs
(``uv run --frozen pytest -q -p no:cacheprovider``, every test, pass or fail). If the
suite writes no trace at all, a module import sweep replaces it and the write-up says
so. X1 is ``praxis.extract.static_imports(clone, lambda p: p)`` over the same clone.
Both sides keep only git-listed ``.py`` files and drop self-pairs.

  PASS iff, on each of the three repositories, X1's recall of the oracle's file-level
  edges is at least 0.95. Recall counts every oracle edge, including those made by
  ``importlib.import_module`` or an explicit ``__import__(...)`` call. An empty oracle
  is a FAIL for that repository, since its recall is undefined. Precision is reported
  and not gated: static analysis sees imports the run never executes. Directory-level
  recall and precision are reported and not gated.

Other languages (gated on the harness only; no praxis extractor exists yet). TypeScript
and JavaScript, Go, Rust and Java, each on at least two public repositories cloned over
HTTPS and named with their commit in the write-up:

  PASS iff each adapter returns a non-empty edge list with zero adapter errors on each
  of its repositories, with its edge counts and wall time recorded. An adapter error is
  an exception in the adapter, or a toolchain run that leaves nothing to parse.
  Diagnostics the toolchain reports about the repository (type errors, unresolved
  external packages, compile errors from absent dependencies) are counted and reported,
  and are not adapter errors, as long as an edge list came out.

TypeScript route choice, written before the run: on pmndrs/zustand both routes run, the
``tsc --traceResolution`` parse and the compiler-API script. The route whose edge set
contains the other's is kept; if they are equal, the compiler API is kept, because it
reads a structure instead of parsing English trace lines.

Prediction, written before the run: Python recall 0.97 to 1.0 on all three, the misses
being importlib calls and imports made through a path a test put on sys.path; precision
0.5 to 0.8, the extra edges being TYPE_CHECKING imports, function-level imports on paths
no test runs, and test modules the suite deselects.

Python tracer choice. ``builtins.__import__`` is wrapped. A ``sys.meta_path`` finder is
consulted only when a module is first loaded: the second importer of a module is served
from ``sys.modules`` and never reaches a finder, so it would lose every edge after the
first. ``sys.setprofile`` gets no event for an ``import`` statement, which CPython runs
through ``IMPORT_NAME`` with no Python-level call. Once ``builtins.__import__`` is
replaced, ``IMPORT_NAME`` calls the replacement for every executed import statement,
cached or not, with the importer's globals. ``importlib.import_module`` is wrapped too.
This oracle sees only what executes: an import on a path no test reaches is invisible
to it, and so is an import under ``if TYPE_CHECKING:``. Route 'pth' (the default)
installs the tracer in the clone's own venv, so a process the suite starts with a
scrubbed environment is traced too; route 'pythonpath' reaches only processes that
inherit PYTHONPATH.

Usage:
  oracle.py --selfcheck
  oracle.py python REPO [REPO ...] [--route pth|pythonpath]
  oracle.py ts|go|rust|java REPO [REPO ...] [--route api|trace|both]
Options: --json PATH writes every result; --scratch DIR holds temporaries.

Beside this file: shapes.py (the Oracle shape and shared helpers), py_tracer.py,
ts.py, go_lang.py, rust.py and java.py, one adapter each.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

# Siblings of this script, found on sys.path because it is run by path.
from go_lang import go_oracle
from java import java_oracle
from py_tracer import python_clone, python_oracle
from rust import rust_oracle
from shapes import AdapterError, Oracle, Pair, run, to_dirs
from ts import ts_oracle

if TYPE_CHECKING:
    from collections.abc import Iterable

RECALL_GATE = 0.95


# --- comparison -------------------------------------------------------------------


def _distinct(edges: list[Pair], side: str) -> set[Pair]:
    found = set(edges)
    if len(found) != len(edges):
        raise ValueError(f"{side} lists an edge twice; an edge list is distinct pairs")
    return found


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def compare(oracle_edges: list[Pair], praxis_edges: list[Pair]) -> dict[str, object]:
    """Recall and precision of ``praxis_edges`` against ``oracle_edges``. A ratio over
    an empty side is None, never 0 or 1: there was nothing to score."""
    oracle = _distinct(oracle_edges, "oracle")
    praxis = _distinct(praxis_edges, "praxis")
    both = oracle & praxis
    return {
        "oracle": len(oracle),
        "praxis": len(praxis),
        "both": len(both),
        "recall": _ratio(len(both), len(oracle)),
        "precision": _ratio(len(both), len(praxis)),
        "missing": sorted(oracle - praxis),
        "extra": sorted(praxis - oracle),
    }


def compare_levels(oracle: Oracle, praxis_edges: list[Pair]) -> dict[str, object]:
    return {
        "file": compare(oracle.edges, praxis_edges),
        "dir": compare(oracle.dir_edges, to_dirs(praxis_edges)),
    }


# --- Python: X1 against the tracer ---------------------------------------------


def x1(root: Path) -> tuple[list[Pair], dict[str, object]]:
    """praxis's X1 at file level over ``root``."""
    from praxis.extract import EXTRACTOR_VERSION, static_imports

    start = time.perf_counter()
    graph = static_imports(root, lambda p: p)
    seconds = time.perf_counter() - start
    edges = sorted((e.src, e.dst) for e in graph.edges)
    return edges, {
        "version": EXTRACTOR_VERSION,
        "seconds": round(seconds, 3),
        "blind_spots": sum(graph.blind_spots.values()),
        "type_only_statements": sum(graph.type_only.values()),
        "parse_failures": len(graph.parse_failures),
    }


def site_kinds(root: Path, files: Iterable[str]) -> dict[Pair, set[str]]:
    """Per X1 edge, where its statements sit: 'top' (runs at import), 'function' or
    'typing' (under TYPE_CHECKING). Reads X1's own resolver, so the classes describe
    exactly the edges X1 produced."""
    from praxis.extract import _Index, _parse, _python_files, _Sites

    index = _Index.build(_python_files(root))
    found: dict[Pair, set[str]] = {}
    for path in files:
        tree = _parse(root / path)
        if tree is None:
            continue
        visitor = _Sites()
        visitor.visit(tree)
        for node, runs, typing_only in visitor.imports:
            where = "typing" if typing_only else ("top" if runs else "function")
            for dst in index.targets(path, node)[0]:
                found.setdefault((path, dst), set()).add(where)
    return found


def is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return "tests" in parts or name.startswith("test_") or name == "conftest.py"


def _class_of(sites: set[str]) -> str:
    if "top" in sites:
        return "top"
    return "function" if "function" in sites else "typing"


def explain_extra(
    oracle: Oracle, extra: list[Pair], kinds: dict[Pair, set[str]]
) -> dict[str, int]:
    """Why X1 edges went unseen: the source never ran, or the statement is typing-only
    or function-level, split by test and product files."""
    ran = {s for s, _ in oracle.edges} | {d for _, d in oracle.edges}
    reasons: Counter[str] = Counter()
    for src, dst in extra:
        where = "test" if is_test(src) else "product"
        if src not in ran:
            reasons[f"{where}: source never imported or run"] += 1
            continue
        reasons[f"{where}: {_class_of(kinds.get((src, dst), set()))}"] += 1
    return dict(sorted(reasons.items()))


def explain_missing(oracle: Oracle, missing: list[Pair]) -> dict[str, int]:
    kinds = oracle.detail.get("edge_kinds", {})
    assert isinstance(kinds, dict)
    reasons: Counter[str] = Counter()
    for src, dst in missing:
        where = "test" if is_test(src) else "product"
        reasons[f"{where}: {'+'.join(kinds.get(f'{src} -> {dst}', []))}"] += 1
    return dict(sorted(reasons.items()))


def python_measure(repo: Path, scratch: Path, route: str) -> dict[str, object]:
    with python_clone(repo, scratch) as root:
        oracle = python_oracle(root, None, route)
        praxis_edges, x1_detail = x1(root)
        levels = compare_levels(oracle, praxis_edges)
        file_level = levels["file"]
        assert isinstance(file_level, dict)
        extra = file_level["extra"]
        kinds = site_kinds(root, sorted({s for s, _ in extra}))
    return {
        "oracle": oracle,
        "x1": x1_detail,
        "levels": levels,
        "extra_reasons": explain_extra(oracle, extra, kinds),
        "missing_reasons": explain_missing(oracle, file_level["missing"]),
    }


# --- self-check -------------------------------------------------------------------


def _git_init(root: Path) -> None:
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=e13",
            "-c",
            "user.email=e13@invalid",
            "commit",
            "-qm",
            "p",
        ],
    ):
        if run(argv, root).returncode != 0:
            raise AdapterError("could not plant the self-check repository")


SELFCHECK_EXPECTED: dict[str, dict[str, object]] = {
    "file": {
        "oracle": 1,
        "praxis": 2,
        "both": 1,
        "recall": 1.0,
        "precision": 0.5,
        "missing": [],
        "extra": [("pkg/b.py", "a.py")],
    },
    "dir": {
        "oracle": 1,
        "praxis": 2,
        "both": 1,
        "recall": 1.0,
        "precision": 0.5,
        "missing": [],
        "extra": [("pkg", ".")],
    },
}


def selfcheck() -> int:
    """Two planted files. a.py imports pkg/b.py; pkg/b.py imports a.py only under
    TYPE_CHECKING. Worked by hand: running a.py executes one import, so the oracle is
    {(a.py, pkg/b.py)}; X1 resolves both statements, so praxis is that plus
    (pkg/b.py, a.py). File level: recall 1/1, precision 1/2, one extra. The two files
    sit in '.' and 'pkg', so directory level is the same with ('pkg', '.') extra."""
    pure = compare([("a", "b")], [])
    assert pure["recall"] == 0.0, pure
    assert pure["precision"] is None, pure
    assert compare([], [])["recall"] is None
    assert to_dirs([("p/b.py", "p/c.py"), ("b.py", "p/c.py")]) == [(".", "p")]
    with tempfile.TemporaryDirectory(prefix="e13-self-") as name:
        root = Path(name, "planted")
        (root / "pkg").mkdir(parents=True)
        (root / "a.py").write_text("from pkg import b\n")
        (root / "pkg" / "b.py").write_text(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import a\n"
        )
        (root / ".gitignore").write_text(".venv/\n")
        _git_init(root)
        praxis_edges, _ = x1(root)
        via_path = python_oracle(root, [sys.executable, "a.py"], "pythonpath")
        run([sys.executable, "-m", "venv", "--without-pip", ".venv"], root)
        venv_python = str(root / ".venv" / "bin" / "python")
        via_pth = python_oracle(root, [venv_python, "a.py"], "pth")
        got = compare_levels(via_path, praxis_edges)
        got_pth = compare_levels(via_pth, praxis_edges)
    say(json.dumps(got, indent=1))
    by_path, by_pth = got == SELFCHECK_EXPECTED, got_pth == SELFCHECK_EXPECTED
    ok = by_path and by_pth
    say(f"routes: pythonpath {by_path}, pth {by_pth}")
    say(f"selfcheck: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


# --- report -----------------------------------------------------------------------


def say(text: str) -> None:
    sys.stdout.write(text + "\n")


def versions() -> dict[str, str]:
    probes = {
        "python": [sys.executable, "--version"],
        "uv": ["uv", "--version"],
        "tsc": ["tsc", "--version"],
        "node": ["node", "--version"],
        "go": ["go", "version"],
        "cargo": ["cargo", "--version"],
        "rustc": ["rustc", "--version"],
        "javac": ["javac", "-version"],
        "jdeps": ["jdeps", "-version"],
    }
    found: dict[str, str] = {}
    for name, argv in probes.items():
        try:
            done = run(argv, Path.cwd(), timeout=60)
        except OSError:
            found[name] = "absent"
            continue
        lines = (done.stdout + done.stderr).splitlines()
        found[name] = next((x for x in lines if "JAVA_TOOL_OPTIONS" not in x), "")
    return found


def _fmt(value: object) -> str:
    return (
        "n/a"
        if value is None
        else f"{value:.3f}"
        if isinstance(value, float)
        else str(value)
    )


def report(results: list[dict[str, object]]) -> None:
    """One row per repository and language; a comparison block where X1 exists."""
    head = ("lang", "repo", "head", "file edges", "dir edges", "unresolved", "secs")
    say(" | ".join(head))
    for result in results:
        oracle = result.get("oracle")
        if not isinstance(oracle, Oracle):
            say(f"{result['language']} | {result['repo']} | ERROR {result['error']}")
            continue
        row = (oracle.language, oracle.repo, oracle.head, len(oracle.edges))
        row2 = (len(oracle.dir_edges), oracle.unresolved, round(oracle.seconds, 2))
        say(" | ".join(str(x) for x in (*row, *row2)))
        levels = result.get("levels")
        if isinstance(levels, dict):
            _report_levels(levels)


def _report_levels(levels: dict[str, object]) -> None:
    for level, cmp in levels.items():
        assert isinstance(cmp, dict)
        say(
            f"    X1 {level}: oracle {cmp['oracle']} praxis {cmp['praxis']}"
            f" both {cmp['both']} recall {_fmt(cmp['recall'])}"
            f" precision {_fmt(cmp['precision'])}"
        )


def _measure(lang: str, repo: Path, scratch: Path, route: str) -> dict[str, object]:
    if lang == "python":
        tracer = route if route in ("pth", "pythonpath") else "pth"
        return python_measure(repo, scratch, tracer)
    if lang == "ts" and route == "both":
        api, trace = ts_oracle(repo, "api"), ts_oracle(repo, "trace")
        return {
            "oracle": api,
            "routes": compare(trace.edges, api.edges),
            "trace": trace,
        }
    adapters = {
        "ts": lambda: ts_oracle(repo, route),
        "go": lambda: go_oracle(repo, scratch),
        "rust": lambda: rust_oracle(repo),
        "java": lambda: java_oracle(repo),
    }
    return {"oracle": adapters[lang]()}


def measure_all(
    lang: str, repos: list[Path], scratch: Path, route: str
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for repo in repos:
        try:
            result = _measure(lang, repo.resolve(), scratch, route)
        except (
            AdapterError,
            OSError,
            ValueError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            result = {
                "language": lang,
                "repo": repo.name,
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)
    return results


def verdict(lang: str, results: list[dict[str, object]]) -> str:
    if lang == "python":
        recalls = [_file_recall(r) for r in results]
        ok = bool(recalls) and all(r is not None and r >= RECALL_GATE for r in recalls)
        return f"python recall >= {RECALL_GATE} on each: {ok} ({recalls})"
    errors = sum("error" in r for r in results)
    oracles = [r["oracle"] for r in results if isinstance(r.get("oracle"), Oracle)]
    empty = sum(not o.edges for o in oracles if isinstance(o, Oracle))
    ok = len(results) >= 2 and errors == 0 and empty == 0
    counts = f"{len(results)} repositories, {errors} adapter errors, {empty} empty"
    return f"{lang}: {counts}: {ok}"


def _file_recall(result: dict[str, object]) -> float | None:
    levels = result.get("levels")
    if not isinstance(levels, dict):
        return None
    recall = levels["file"]["recall"]
    return float(recall) if recall is not None else None


def _jsonable(value: object) -> object:
    if isinstance(value, Oracle):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    return value


_ROUTES = ["api", "trace", "both", "pth", "pythonpath"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="E13 toolchain import-edge oracles")
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument(
        "lang", nargs="?", choices=["python", "ts", "go", "rust", "java"]
    )
    parser.add_argument("repos", nargs="*", type=Path)
    parser.add_argument("--route", choices=_ROUTES, default="api")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--scratch", type=Path)
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    if not args.lang or not args.repos:
        parser.error("name a language and at least one repository")
    scratch = args.scratch or Path(tempfile.gettempdir())
    scratch.mkdir(parents=True, exist_ok=True)
    results = measure_all(args.lang, args.repos, scratch.resolve(), args.route)
    report(results)
    say(verdict(args.lang, results))
    if args.json:
        payload = {"versions": versions(), "results": _jsonable(results)}
        args.json.write_text(json.dumps(payload, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
