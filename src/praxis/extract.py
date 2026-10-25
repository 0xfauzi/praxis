"""Ground-truth extractors at component grain: X1 static imports, X2 runtime component
traces, X3 SQLite schema references.

Every function here reads source, traces or DDL in memory and returns identifiers and
counts only: a path, a component id, a table name, a count. No line, value or argument
leaves this module. X1 truth is for the working tree (tracked and untracked-not-ignored
files that exist on disk); X2 runs in a scratch clone of HEAD, so the owner's repository
is never written. A static graph misses dynamic, configuration and SQL wiring: an absent
edge may be a blind spot, which is why blind spots are counted rather than assumed zero.
An import under ``if TYPE_CHECKING:`` that resolves is an edge and not a blind spot: X1
resolves it statically, so the owner can be asked about it (design 3.1).

Design: docs/design/03-measurement.md, sections 3.1 to 3.3 and 3.5. Each language's
import reader lives in praxis.lang; the listing, the lift to components and the counts
stay here (docs/design/04-languages.md section 2.3).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from praxis.lang import BLIND, PYTHON, REGISTERED, Unparsed, claimant, file_class

if TYPE_CHECKING:
    from collections.abc import Callable

    from praxis.lang import ImportSite, Language

# Python's X1 version. A file_edges row carries its source file's language version.
EXTRACTOR_VERSION = PYTHON.version
# The runtime tracer's version (X2, run_scenario); scenario_runs rows carry it.
X2_VERSION = "x2-v1"
# A cap against a hung git, not a measured bound: clone time on the owner's largest
# repository needs measuring (E05).
GIT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    sites: int
    module_level: bool


@dataclass(frozen=True)
class StaticGraph:
    """``blind_spots`` and ``type_only`` each have a key for every component with at
    least one parsed file, so a 0 there was looked for; a component absent from them was
    never parsed. A blind spot is what X1 cannot resolve: an importlib/__import__ call,
    or an import statement whose module name matches two files. String plugin registries
    are not detected at all, so each count is a lower bound.

    A TYPE_CHECKING import that resolves into another component is an edge (design 3.1:
    any import that resolves) and is counted in ``type_only``, one per statement, so a
    report can stratify by it; nothing reads that count yet. It never runs, so its sites
    never make ``module_level`` true: that flag means a site that executes when the
    module is imported."""

    edges: tuple[Edge, ...]
    blind_spots: dict[str, int]
    type_only: dict[str, int]
    parse_failures: tuple[str, ...]
    extractor_version: str
    languages: tuple[Coverage, ...] = ()
    # Whole-tree scans only, else None: per component, its working-tree files by class
    # (a registered language's name, unmapped_language, non_code or unknown; design 6).
    file_classes: dict[str, dict[str, int]] | None = None


@dataclass(frozen=True)
class Coverage:
    """One language's files in a scan: ``listed`` in the working tree, and of the files
    read, ``parsed`` and ``failed``."""

    language: str
    extractor_version: str
    listed: int
    parsed: int
    failed: int


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


def _git(cwd: Path | None, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=True, timeout=GIT_TIMEOUT_S
    )
    return done.stdout.decode("utf-8", "surrogateescape")


def _listed(repo_root: Path) -> list[str]:
    listed = _git(
        repo_root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    # --cached lists an unmerged path once per stage; a path is an identifier here.
    return list(dict.fromkeys(p for p in listed.split("\0") if p))


@dataclass
class _Lift:
    """File-level import sites lifted to component edges, one site per statement."""

    assign: Callable[[str], str]
    sites: Counter[tuple[str, str]] = field(default_factory=Counter)
    at_import: set[tuple[str, str]] = field(default_factory=set)
    blind: Counter[str] = field(default_factory=Counter)
    typed: Counter[str] = field(default_factory=Counter)

    def add(self, path: str, statements: list[_Statement]) -> None:
        src = self.assign(path)
        self.blind[src] += 0
        self.typed[src] += 0
        for found, blind, deferred, type_only in statements:
            dsts = {self.assign(f) for f in found} - {src}
            self.blind[src] += blind
            self.typed[src] += type_only and bool(dsts)
            for dst in dsts:
                self.sites[src, dst] += 1
                if not deferred:
                    self.at_import.add((src, dst))


# Per statement: the files it resolves to, whether it is a blind spot, deferred, and
# type-only. A statement is the sites one reader gave the same offsets.
_Statement = tuple[list[str], bool, bool, bool]


def _statements(
    language: Language[Any], roots: object, path: str, sites: list[ImportSite]
) -> list[_Statement]:
    spans: dict[tuple[int, int], list[ImportSite]] = {}
    for site in sites:
        spans.setdefault((site.start, site.end), []).append(site)
    out: list[_Statement] = []
    for group in spans.values():
        blind = any(s.blind is not None for s in group)
        done = [language.resolve(s, path, roots) for s in group if s.blind is None]
        found = [r.target for r in done if r.target is not None]
        blind |= any(r.reason in BLIND for r in done)
        out.append((found, blind, group[0].deferred, group[0].type_only))
    return out


@dataclass
class _Reader:
    """One language's share of a scan: its reader, its roots and its counts."""

    language: Language[Any]
    roots: object
    present: list[str]
    parsed: int = 0
    failed: int = 0


def _readers(
    repo_root: Path, listed: list[str], parse: list[str], whole: bool
) -> dict[str, _Reader]:
    """A reader for every registered language that claims a file in the scan, loaded
    only then; each resolves against all of its files in the tree."""
    wanted = {r.name: r for p in (listed if whole else parse) if (r := claimant(p))}
    unclaimed = [p for p in listed if claimant(p) is None]
    readers: dict[str, _Reader] = {}
    for name, entry in sorted(wanted.items()):
        language = entry.load()
        present = language.files(repo_root, listed)
        mine = [p for p in parse if claimant(p) is entry]
        roots = language.roots(repo_root, [*present, *mine, *unclaimed])
        readers[name] = _Reader(language, roots, present)
    return readers


def _language_of(path: str) -> str:
    entry = claimant(path)
    if entry is None:
        raise ValueError(f"no registered language reads {path}")
    return entry.name


def _whole(listed: list[str], readers: dict[str, _Reader]) -> list[str]:
    present = {p for r in readers.values() for p in r.present}
    return [p for p in listed if p in present]


def _version(readers: dict[str, _Reader]) -> str:
    """The sorted join of the versions that read a file; with none, every registered
    version, because each found nothing to read."""
    used = [r.language.VERSION for r in readers.values()]
    return "+".join(sorted(used or [r.version for r in REGISTERED]))


def static_imports(
    repo_root: Path, assign: Callable[[str], str], files: list[str] | None = None
) -> StaticGraph:
    """X1 over the working tree's files that a registered language reads, or over
    ``files`` only. Either way, imports resolve against every file of that language in
    the working tree. A path in ``files`` that no language reads is refused."""
    listed = _listed(repo_root)
    parse = [] if files is None else files
    if len(set(parse)) != len(parse):
        raise ValueError("files names a path twice")
    for path in parse:
        _language_of(path)
    readers = _readers(repo_root, listed, parse, files is None)
    if files is None:
        parse = _whole(listed, readers)
    lift = _Lift(assign)
    failures = [p for p in parse if not _read(repo_root, p, readers, lift)]
    edges = tuple(
        Edge(s, d, n, (s, d) in lift.at_import)
        for (s, d), n in sorted(lift.sites.items())
    )
    return StaticGraph(
        edges,
        dict(sorted(lift.blind.items())),
        dict(sorted(lift.typed.items())),
        tuple(failures),
        _version(readers),
        tuple(
            Coverage(name, r.language.VERSION, len(r.present), r.parsed, r.failed)
            for name, r in readers.items()
        ),
        _classes(repo_root, listed, readers, assign) if files is None else None,
    )


def _classes(
    repo_root: Path,
    listed: list[str],
    readers: dict[str, _Reader],
    assign: Callable[[str], str],
) -> dict[str, dict[str, int]]:
    """Each working-tree file's class by suffix, counted per component."""
    present = {p for r in readers.values() for p in r.present}
    found: dict[str, Counter[str]] = {}
    for path in listed:
        kind = file_class(path)
        here = path in present if claimant(path) else (repo_root / path).is_file()
        if here:
            found.setdefault(assign(path), Counter())[kind] += 1
    return {c: dict(sorted(n.items())) for c, n in sorted(found.items())}


def _read(repo_root: Path, path: str, readers: dict[str, _Reader], lift: _Lift) -> bool:
    """Lift one file's sites; False when it could not be read or parsed."""
    reader = readers[_language_of(path)]
    try:
        sites = reader.language.imports(path, (repo_root / path).read_bytes())
    except (OSError, Unparsed):
        reader.failed += 1
        return False
    reader.parsed += 1
    lift.add(path, _statements(reader.language, reader.roots, path, sites))
    return True


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
