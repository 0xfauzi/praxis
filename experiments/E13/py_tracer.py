"""E13 Python adapter: the runtime import tracer, installed into the clone's own venv
as a .pth (route 'pth') or put first on PYTHONPATH as sitecustomize (route
'pythonpath'), and the oracle it yields while the repository's suite runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from collections.abc import Iterator

# Caps against a hung toolchain, not measured bounds.
SUITE_TIMEOUT_S = 1800.0


# Loaded as sitecustomize by every Python process the suite starts. Written for any
# Python 3, because the interpreter is the repository's, not this one. Each process
# writes one file of distinct (importer, imported, kind) at exit. A process ended by
# os._exit, or started with -I, -E or -S, writes nothing.
_PLACEHOLDER = "ROOT = OUT = None"
_TRACER = """\
import atexit, builtins, importlib, importlib.util, json, os, sys, tempfile


ROOT = OUT = None


def _install():
    root = os.environ.get("E13_TRACE_ROOT") or ROOT
    out = os.environ.get("E13_TRACE_OUT") or OUT
    if not root or not out:
        return
    prefix = os.path.realpath(root) + os.sep
    where, seen, unresolved = {}, set(), set()
    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def rel(path):
        if not isinstance(path, str):
            return None
        if path not in where:
            full = os.path.realpath(path)
            where[path] = full[len(prefix):] if full.startswith(prefix) else None
        return where[path]

    def note(importer, name, kind):
        module = sys.modules.get(name)
        if module is None:
            return
        dst = rel(getattr(module, "__file__", None))
        if dst is not None:
            seen.add((importer, dst, kind))
        elif any(rel(p) for p in list(getattr(module, "__path__", None) or ())):
            unresolved.add((importer, name))

    def package_of(g):
        package = g.get("__package__")
        if package is None:
            package = getattr(g.get("__spec__"), "parent", None)
        return package

    def targets(name, fromlist):
        names = [i for i in (fromlist or ()) if isinstance(i, str) and i != "*"]
        found = set()
        for item in names:
            sub = name + "." + item
            found.add(sub if sub in sys.modules else name)
        return found or {name}

    def record(name, g, fromlist, level, kind):
        importer = rel(g.get("__file__")) if g else None
        if importer is None:
            return
        if level:
            name = importlib.util.resolve_name("." * level + name, package_of(g))
        for target in targets(name, fromlist):
            note(importer, target, kind)

    def traced_import(name, globals=None, locals=None, fromlist=(), level=0):
        module = real_import(name, globals, locals, fromlist, level)
        try:
            if globals is None:
                record(name, sys._getframe(1).f_globals, fromlist, level, "call")
            else:
                record(name, globals, fromlist, level, "statement")
        except Exception:
            pass
        return module

    def traced_import_module(name, package=None):
        module = real_import_module(name, package)
        try:
            g = sys._getframe(1).f_globals
            importer = rel(g.get("__file__"))
            if importer is not None:
                target = importlib.util.resolve_name(name, package)
                note(importer, target, "import_module")
        except Exception:
            pass
        return module

    def dump():
        builtins.__import__ = real_import
        try:
            fd, part = tempfile.mkstemp(dir=out, suffix=".part")
            with os.fdopen(fd, "w") as fh:
                json.dump({"edges": sorted(seen), "unresolved": sorted(unresolved)}, fh)
            os.replace(part, part[:-5] + ".json")
        except OSError:
            pass

    atexit.register(dump)
    builtins.__import__ = traced_import
    importlib.import_module = traced_import_module


_install()
"""
# Only for the sitecustomize route: that file shadows any sitecustomize the interpreter
# would have run, so it runs that one too.
_CHAIN = """\
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _here]
del sys.modules[__name__]
try:
    import sitecustomize
except ImportError:
    pass
"""

SUITE = ("uv", "run", "--frozen", "pytest", "-q", "-p", "no:cacheprovider")


@contextmanager
def python_clone(repo: Path, scratch: Path) -> Iterator[Path]:
    """A fresh clone of HEAD with its own environment; deleted on exit."""
    dest = Path(tempfile.mkdtemp(prefix="e13-py-", dir=scratch)) / repo.name
    try:
        done = run(["git", "clone", "--quiet", str(repo), str(dest)], scratch)
        if done.returncode != 0:
            raise AdapterError(f"git clone of {repo.name} failed")
        synced = run(["uv", "sync", "--frozen"], dest)
        if synced.returncode != 0:
            raise AdapterError(f"uv sync failed in {repo.name}")
        yield dest
    finally:
        shutil.rmtree(dest.parent, ignore_errors=True)


def _read_traces(out: Path) -> tuple[list[tuple[str, str, str]], set[Pair], int]:
    edges: list[tuple[str, str, str]] = []
    unresolved: set[Pair] = set()
    files = sorted(out.glob("*.json"))
    for record in files:
        data = json.loads(record.read_text())
        edges += [(a, b, k) for a, b, k in data["edges"]]
        unresolved |= {(a, b) for a, b in data["unresolved"]}
    return edges, unresolved, len(files)


def _sweep_argv(root: Path, listed: set[str]) -> list[str]:
    """Import every listed module under src/ in one process: the fallback when the
    suite writes no trace."""
    names = sorted(
        ".".join(PurePosixPath(p).with_suffix("").parts[1:]).removesuffix(".__init__")
        for p in listed
        if p.startswith("src/") and p.endswith(".py")
    )
    script = (
        "import importlib, sys\nfor m in sys.argv[1:]:\n    importlib.import_module(m)"
    )
    return [str(root / ".venv" / "bin" / "python"), "-c", script, *names]


def _install_tracer(root: Path, out: Path, boot: Path, route: str) -> dict[str, str]:
    """Install the tracer; return the environment the run needs. 'pth' writes it into
    the clone's own venv, so a process the suite starts with a scrubbed environment is
    still traced; 'pythonpath' reaches only processes that inherit PYTHONPATH."""
    site = sorted(root.glob(".venv/lib/python3*/site-packages"))
    if route == "pth" and site:
        baked = f"ROOT, OUT = {str(root.resolve())!r}, {str(out)!r}"
        (site[0] / "e13_tracer.py").write_text(_TRACER.replace(_PLACEHOLDER, baked))
        (site[0] / "e13_tracer.pth").write_text("import e13_tracer\n")
        return {}
    (boot / "sitecustomize.py").write_text(_TRACER + _CHAIN)
    return {
        "PYTHONPATH": str(boot),
        "E13_TRACE_ROOT": str(root.resolve()),
        "E13_TRACE_OUT": str(out),
    }


def _remove_tracer(root: Path) -> None:
    for site in root.glob(".venv/lib/python3*/site-packages"):
        (site / "e13_tracer.pth").unlink(missing_ok=True)


_SUMMARY = re.compile(r"^=*\s*(\d+ (?:passed|failed).*?)\s*=*$", re.MULTILINE)


def _trace_run(
    root: Path, argv: list[str], tmp: Path, route: str
) -> tuple[int | None, Path, str]:
    boot, out = tmp / "boot", tmp / "out"
    boot.mkdir(parents=True)
    out.mkdir()
    env = {**os.environ, **_install_tracer(root, out, boot, route)}
    env.pop("VIRTUAL_ENV", None)
    try:
        done = run(argv, root, env, SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, out, "timeout"
    finally:
        _remove_tracer(root)
    found = _SUMMARY.findall(done.stdout)
    return done.returncode, out, found[-1] if found else ""


def python_oracle(
    root: Path, argv: list[str] | None = None, route: str = "pth"
) -> Oracle:
    """Runtime oracle over ``root``, which the caller owns and may be written."""
    listed = {p for p in listed_files(root) if p.endswith(".py")}
    command = list(argv or SUITE)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="e13-trace-") as tmp:
        code, out, summary = _trace_run(root, command, Path(tmp), route)
        raw, unresolved, processes = _read_traces(out)
        mode = "suite"
        if processes == 0 and argv is None:
            mode = "sweep"
            sweep = _sweep_argv(root, listed)
            code, out, summary = _trace_run(root, sweep, Path(tmp, "s"), route)
            raw, unresolved, processes = _read_traces(out)
    seconds = time.perf_counter() - start
    if processes == 0:
        raise AdapterError("no Python process wrote a trace")
    edges, dropped = keep_listed(((a, b) for a, b, _ in raw), listed)
    kinds: dict[Pair, set[str]] = {}
    for a, b, k in raw:
        kinds.setdefault((a, b), set()).add(k)
    detail: dict[str, object] = {
        "mode": mode,
        "tracer_route": route,
        "exit_code": code,
        "summary": summary,
        "processes": processes,
        "dropped_unlisted": dropped,
        "kinds": dict(Counter("+".join(sorted(kinds[e])) for e in edges)),
        "edge_kinds": {f"{s} -> {d}": sorted(kinds[s, d]) for s, d in edges},
    }
    return Oracle(
        "python",
        root.name,
        head_of(root),
        edges,
        to_dirs(edges),
        len(unresolved),
        seconds,
        detail,
    )
