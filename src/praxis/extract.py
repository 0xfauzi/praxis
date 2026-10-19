"""Ground-truth extractors at component grain: X1 static imports, X2 runtime component
traces, X3 SQLite schema references.

Every function here reads source, traces or DDL in memory and returns identifiers and
counts only: a path, a component id, a table name, a count. No line, value or argument
leaves this module. X1 truth is for the working tree (tracked and untracked-not-ignored
files that exist on disk); X2 runs in a scratch clone of HEAD, so the owner's repository
is never written. A static graph misses dynamic, configuration and SQL wiring: an absent
edge may be a blind spot, which is why blind spots are counted rather than assumed zero.

Design: docs/design/03-measurement.md, sections 3.1 to 3.3 and 3.5.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import warnings
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

EXTRACTOR_VERSION = "x1-v1"
# The runtime tracer's version (X2, run_scenario); scenario_runs rows carry it.
X2_VERSION = "x2-v1"
# A cap against a hung git, not a measured bound: clone time on the owner's largest
# repository needs measuring (E05).
GIT_TIMEOUT_S = 60.0
_DYNAMIC = frozenset({"__import__", "import_module"})

Import = ast.Import | ast.ImportFrom


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    sites: int
    module_level: bool


@dataclass(frozen=True)
class StaticGraph:
    """``blind_spots`` has a key for every component with at least one parsed file, so
    a 0 there was looked for; a component absent from it was never parsed. Its counts
    are importlib/__import__ calls, TYPE_CHECKING imports that resolve into another
    component, and import statements whose module name matches two files. String plugin
    registries are not detected at all.

    A TYPE_CHECKING import is an edge (design 3.1: any import that resolves) but never
    runs, so its sites never make ``module_level`` true: that flag means a site that
    executes when the module is imported."""

    edges: tuple[Edge, ...]
    blind_spots: dict[str, int]
    parse_failures: tuple[str, ...]
    extractor_version: str


@dataclass(frozen=True)
class Scenario:
    id: str
    argv: tuple[str, ...]
    command_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("a scenario needs a command")
        digest = hashlib.sha256(" ".join(self.argv).encode()).hexdigest()[:16]
        object.__setattr__(self, "command_hash", digest)


@dataclass(frozen=True)
class Trace:
    """``components`` and ``call_edges`` were observed only when ``outcome`` is 'ok' or
    'failed'; on 'timeout' and 'not_python' they are empty because nothing was seen.
    'not_python' means no Python process in the run loaded the tracer (a non-Python
    command, or Python started with -I, -E or -S, or ended by os._exit)."""

    scenario_id: str
    components: frozenset[str]
    call_edges: dict[tuple[str, str], int]
    exit_code: int | None
    duration_ms: float
    outcome: str


class _Sites(ast.NodeVisitor):
    """Import statements as (node, runs at import time, under TYPE_CHECKING)."""

    def __init__(self) -> None:
        self.imports: list[tuple[Import, bool, bool]] = []
        self.dynamic = 0
        self._depth = 0
        self._typing = False

    def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = _scope

    def visit_If(self, node: ast.If) -> None:
        if _name(node.test) != "TYPE_CHECKING":
            self.generic_visit(node)
            return
        outer, self._typing = self._typing, True
        for stmt in node.body:
            self.visit(stmt)
        self._typing = outer
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Import(self, node: Import) -> None:
        runs = self._depth == 0 and not self._typing
        self.imports.append((node, runs, self._typing))

    visit_ImportFrom = visit_Import

    def visit_Call(self, node: ast.Call) -> None:
        self.dynamic += _name(node.func) in _DYNAMIC
        self.generic_visit(node)


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _git(cwd: Path | None, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=True, timeout=GIT_TIMEOUT_S
    )
    return done.stdout.decode("utf-8", "surrogateescape")


def _python_files(repo_root: Path) -> list[str]:
    listed = _git(
        repo_root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    # --cached lists an unmerged path once per stage; a path is an identifier here.
    paths = dict.fromkeys(p for p in listed.split("\0") if p.endswith(".py"))
    return [p for p in paths if (repo_root / p).is_file()]


def _module_names(path: str, paths: set[str]) -> list[str]:
    """Package-rooted name first (how sys.path sees it), then the repo-root dotted
    name (how a namespace package run from the root sees it)."""
    parts = path[:-3].split("/")
    dirs = parts[:-1]
    if parts[-1] == "__init__":
        parts = dirs
    top = len(dirs)
    while top > 0 and "/".join([*dirs[:top], "__init__.py"]) in paths:
        top -= 1
    return [n for n in dict.fromkeys([".".join(parts[top:]), ".".join(parts)]) if n]


def _prefixes(dotted: str) -> list[str]:
    parts = dotted.split(".")
    return [".".join(parts[:k]) for k in range(len(parts), 0, -1)]


def _relative_base(name: str, is_init: bool, node: ast.ImportFrom) -> str | None:
    package = name.split(".") if is_init else name.split(".")[:-1]
    keep = len(package) - (node.level - 1)
    if keep <= 0:
        return None
    return ".".join([*package[:keep], *([node.module] if node.module else [])])


def _bases(node: ast.ImportFrom, names: list[str], is_init: bool) -> list[str]:
    if node.level == 0:
        return [node.module or ""]
    found = (_relative_base(n, is_init, node) for n in names)
    return [b for b in found if b is not None]


def _candidates(node: Import, names: list[str], is_init: bool) -> list[list[str]]:
    """Per alias, the module names to try in order; the first one indexed wins."""
    if isinstance(node, ast.Import):
        return [_prefixes(alias.name) for alias in node.names]
    bases = _bases(node, names, is_init)
    return [
        [c for base in bases for c in _prefixes(f"{base}.{alias.name}")]
        for alias in node.names
    ]


@dataclass(frozen=True)
class _Index:
    modules: dict[str, list[str]]
    names: dict[str, list[str]]

    @classmethod
    def build(cls, paths: list[str]) -> _Index:
        known = set(paths)
        names = {p: _module_names(p, known) for p in known}
        modules: dict[str, list[str]] = {}
        for path in sorted(known):
            for name in names[path]:
                modules.setdefault(name, []).append(path)
        return cls(modules, names)

    def targets(self, path: str, node: Import) -> tuple[list[str], bool]:
        """The files one statement resolves to, and whether a name matched two."""
        is_init = path.rsplit("/", 1)[-1] == "__init__.py"
        found: list[str] = []
        ambiguous = False
        for tries in _candidates(node, self.names[path], is_init):
            hits = next((self.modules[c] for c in tries if c in self.modules), [])
            found += hits if len(hits) == 1 else []
            ambiguous |= len(hits) > 1
        return found, ambiguous


@dataclass
class _Lift:
    """File-level import sites lifted to component edges, one site per statement."""

    index: _Index
    assign: Callable[[str], str]
    sites: Counter[tuple[str, str]] = field(default_factory=Counter)
    at_import: set[tuple[str, str]] = field(default_factory=set)
    blind: Counter[str] = field(default_factory=Counter)

    def add(self, path: str, tree: ast.Module) -> None:
        src, visitor = self.assign(path), _Sites()
        visitor.visit(tree)
        self.blind[src] += visitor.dynamic
        for node, runs, typing_only in visitor.imports:
            found, ambiguous = self.index.targets(path, node)
            dsts = {self.assign(f) for f in found} - {src}
            self.blind[src] += ambiguous or (typing_only and bool(dsts))
            for dst in dsts:
                self.sites[src, dst] += 1
                if runs:
                    self.at_import.add((src, dst))


def _parse(path: Path) -> ast.Module | None:
    try:
        source = path.read_bytes()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return ast.parse(source)
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None


def static_imports(
    repo_root: Path, assign: Callable[[str], str], files: list[str] | None = None
) -> StaticGraph:
    """X1 over the working tree's Python files, or over ``files`` only. Either way,
    imports resolve against every Python file in the working tree."""
    present = _python_files(repo_root)
    parse = present if files is None else files
    if len(set(parse)) != len(parse):
        raise ValueError("files names a path twice")
    lift = _Lift(_Index.build([*present, *parse]), assign)
    failures: list[str] = []
    for path in parse:
        tree = _parse(repo_root / path)
        if tree is None:
            failures.append(path)
        else:
            lift.add(path, tree)
    edges = tuple(
        Edge(s, d, n, (s, d) in lift.at_import)
        for (s, d), n in sorted(lift.sites.items())
    )
    return StaticGraph(
        edges, dict(sorted(lift.blind.items())), tuple(failures), EXTRACTOR_VERSION
    )


def reach(graph: StaticGraph, nodes: list[str]) -> set[tuple[str, str]]:
    """Ordered pairs (a, b), a != b, with a path a -> b through ``nodes`` only."""
    succ: dict[str, set[str]] = {n: set() for n in nodes}
    for edge in graph.edges:
        if edge.src in succ and edge.dst in succ:
            succ[edge.src].add(edge.dst)
    pairs: set[tuple[str, str]] = set()
    for start in succ:
        seen: set[str] = set()
        stack = list(succ[start])
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(succ[node])
        pairs.update((start, n) for n in seen - {start})
    return pairs


# Loaded as sitecustomize by every Python process the scenario starts, so a shell
# wrapper that feeds a fixture on stdin is traced as well. Written for any Python 3,
# because the scenario's interpreter is the owner's, not this one. Each process writes
# one file of (caller path, callee path, count) at exit; the caller is the nearest frame
# under the root, so a call routed through the standard library still names its origin.
_TRACER = """\
import atexit, json, os, sys, tempfile, threading


def _install():
    root = os.environ.get("PRAXIS_TRACE_ROOT")
    out = os.environ.get("PRAXIS_TRACE_OUT")
    if not root or not out:
        return
    prefix = os.path.realpath(root) + os.sep
    where, counts = {}, {}

    def rel(filename):
        if filename not in where:
            path = "" if filename.startswith("<") else os.path.realpath(filename)
            where[filename] = path[len(prefix):] if path.startswith(prefix) else None
        return where[filename]

    def profile(frame, event, arg):
        if event != "call":
            return
        callee = rel(frame.f_code.co_filename)
        if callee is None:
            return
        caller, back = None, frame.f_back
        while caller is None and back is not None:
            caller, back = rel(back.f_code.co_filename), back.f_back
        counts[caller, callee] = counts.get((caller, callee), 0) + 1

    def dump():
        sys.setprofile(None)
        threading.setprofile(None)
        try:
            fd, part = tempfile.mkstemp(dir=out, suffix=".part")
            with os.fdopen(fd, "w") as fh:
                json.dump([[a, b, n] for (a, b), n in counts.items()], fh)
            os.replace(part, part[:-5] + ".json")
        except OSError:
            pass

    atexit.register(dump)
    threading.setprofile(profile)
    sys.setprofile(profile)


_install()
# This file shadows any sitecustomize the interpreter would have run: run that one too.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _here]
del sys.modules[__name__]
try:
    import sitecustomize
except ImportError:
    pass
"""


def _run(
    argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_s: float
) -> tuple[int | None, str]:
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None, "failed"
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # The whole session: a shell wrapper's Python child must not outlive the run.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return None, "timeout"
    return code, "ok" if code == 0 else "failed"


def run_scenario(
    scratch_root: Path,
    scenario: Scenario,
    assign: Callable[[str], str],
    timeout_s: float = 120,
) -> Trace:
    """X2: run ``scenario.argv`` in ``scratch_root`` and lift every Python frame under
    it to a component. Self-calls within a component are dropped from ``call_edges``."""
    with tempfile.TemporaryDirectory(prefix="praxis-x2-") as tmp:
        boot, out = Path(tmp, "boot"), Path(tmp, "out")
        boot.mkdir()
        out.mkdir()
        (boot / "sitecustomize.py").write_text(_TRACER)
        paths = [str(boot), os.environ.get("PYTHONPATH", "")]
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(p for p in paths if p),
            "PRAXIS_TRACE_ROOT": str(scratch_root.resolve()),
            "PRAXIS_TRACE_OUT": str(out),
        }
        start = time.perf_counter()
        code, outcome = _run(scenario.argv, scratch_root, env, timeout_s)
        duration_ms = (time.perf_counter() - start) * 1000
        records = sorted(out.glob("*.json")) if outcome != "timeout" else []
        if code is not None and not records:
            outcome = "not_python"
        components: set[str] = set()
        edges: Counter[tuple[str, str]] = Counter()
        for record in records:
            for caller, callee, n in json.loads(record.read_text()):
                dst = assign(callee)
                components.add(dst)
                if caller is not None and assign(caller) != dst:
                    edges[assign(caller), dst] += n
    return Trace(
        scenario.id, frozenset(components), dict(edges), code, duration_ms, outcome
    )


def scratch_clone(repo_root: Path, praxis_home: Path) -> Path:
    """A ``git clone --local`` of HEAD under ``$PRAXIS_HOME/scratch/``, with no remote,
    so nothing run in it can push to the owner's repository. The caller deletes it."""
    parent = praxis_home / "scratch"
    parent.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(dir=parent))
    try:
        _git(None, "clone", "--local", "--quiet", str(repo_root), str(dest))
        _git(dest, "remote", "remove", "origin")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def sqlite_schema(ddl: str) -> dict[str, list[str]]:
    """X3: table -> referenced tables, one entry per foreign key, so two keys into the
    same table list it twice. ATTACH is refused: the DDL must not reach a file."""
    with closing(sqlite3.connect(":memory:")) as db:
        db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        db.executescript(ddl)
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name"
            )
        ]
        return {table: _references(db, table) for table in tables}


def _references(db: sqlite3.Connection, table: str) -> list[str]:
    rows = db.execute('SELECT id, "table" FROM pragma_foreign_key_list(?)', (table,))
    # A composite key is one row per column, all sharing the key's id.
    return sorted(dict(rows).values())
