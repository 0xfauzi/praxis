"""Runtime scenarios, X2 (design 03 sections 3.2, 3.5 and 4.7): `praxis run scenarios`
runs each scenario the owner names in `$PRAXIS_HOME/scenarios/<repo_id>.toml` in a
scratch clone of HEAD and records which components it entered; `praxis show scenarios`
lists them with the last run of each. The file is a closed shape, refused whole when it
holds anything else:

    [[scenario]]
    id = "hook-edit"
    argv = ["praxis", "hook"]

An id is stored, so it is an identifier (a-z, 0-9, `.`, `_`, `-`), never text. The argv
is never stored, only its hash. It runs with no stdin and the clone's root as its
working directory, so a path in it is relative to the repository root, and it must
start a Python process, directly or through a wrapper, without -I, -E or -S (each
skips the tracer or PYTHONPATH). PYTHONPATH starts with the clone's root and the
directory above each top-level package (one with an __init__.py) that holds a product
file of the frozen map, so `-m <package>` and a console script both import the clone's
code ahead of an editable install of the owner's tree; a namespace package is found
from the root only. PRAXIS_HOME points into the clone, so a scenario that runs praxis
cannot write the owner's ledger. Each scenario gets its own clone, deleted after it
even when it fails, so what one scenario writes cannot change the next one's trace.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tomllib
from collections import Counter
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TextIO

from praxis.components import ComponentMap, assign, from_json, product_components
from praxis.extract import GIT_TIMEOUT_S, Scenario, Trace, run_scenario, scratch_clone
from praxis.extract import X2_VERSION as EXTRACTOR_VERSION
from praxis.schema import SCHEMA_VERSION
from praxis.store import Evidence, Store
from praxis.unassisted import Repo, locate

if TYPE_CHECKING:
    from collections.abc import Iterator

REDUCER_VERSION = "scenarios-v1"
# A cap against a hung scenario, not a measured bound: run times need measuring (E05).
TIMEOUT_S = 120.0
SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
COVERAGE = {
    "ok": "full",
    "failed": "partial",
    "timeout": "unobservable",
    "not_python": "unobservable",
}
ASSUMPTIONS = (
    "components are the files under the clone whose Python code a traced process"
    " entered, lifted through the frozen map; C code and untraced processes are unseen",
    "the scenario ran a clone of HEAD: uncommitted work in the owner's tree is not"
    " in it",
    "a failed run's trace ends where the run ended",
)
RUN = "praxis run scenarios"
SHOW = "praxis show scenarios"
NO_MAP = "no set component map. To set one, run `praxis set components`."
Ctx = tuple[Store, Repo, ComponentMap, str]


def config_path(praxis_home: Path, repo_id: str) -> Path:
    return praxis_home / "scenarios" / f"{repo_id}.toml"


def load(praxis_home: Path, repo_id: str) -> list[Scenario] | None:
    """The owner's scenarios in file order, or None when there is no file. Any other
    shape is a ValueError naming the file; a refused value is never echoed."""
    where = config_path(praxis_home, repo_id)
    if not where.is_file():
        return None
    try:
        with where.open("rb") as handle:
            doc = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{where}: not TOML ({exc})") from exc
    tables = doc.get("scenario") if set(doc) == {"scenario"} else None
    if not isinstance(tables, list) or not tables:
        raise ValueError(f"{where}: expected [[scenario]] tables and nothing else")
    found = [_scenario(table, where) for table in tables]
    ids = Counter(s.id for s in found)
    repeated = sorted(sid for sid, n in ids.items() if n > 1)
    if repeated:
        raise ValueError(f"{where}: scenario ids repeated: {repeated}")
    return found


def _scenario(table: object, where: Path) -> Scenario:
    if not isinstance(table, dict) or set(table) != {"id", "argv"}:
        raise ValueError(f"{where}: each [[scenario]] has exactly `id` and `argv`")
    sid, argv = table["id"], table["argv"]
    if not isinstance(sid, str) or SCENARIO_ID.fullmatch(sid) is None:
        raise ValueError(
            f"{where}: a scenario id is 1 to 64 of a-z 0-9 . _ - and starts with a"
            " letter or a digit"
        )
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError(f"{where}: scenario {sid!r} argv is not a list of strings")
    if not argv or not argv[0]:
        raise ValueError(f"{where}: scenario {sid!r} argv names no command")
    return Scenario(sid, tuple(argv))


def run(repo_root: Path, praxis_home: Path, stdout: TextIO, now: str) -> int:
    """`praxis run scenarios`: one scenario_runs row per scenario, or a refusal (exit
    2) that writes nothing: outside a repository, without a valid scenario file,
    without a frozen map or without a commit to clone."""
    repo = locate(repo_root)
    if repo is None:
        return _refuse(stdout, RUN, "not inside a git repository")
    try:
        scenarios = load(praxis_home, repo.repo_id)
    except ValueError as exc:
        return _refuse(stdout, RUN, str(exc))
    if scenarios is None:
        where = config_path(praxis_home, repo.repo_id)
        return _refuse(stdout, RUN, f"no scenarios; {where} names them")
    if not (praxis_home / "ledger.db").exists():  # a refusal never creates the ledger
        return _refuse(stdout, RUN, NO_MAP)
    ledger = Store(praxis_home / "ledger.db").open()
    try:
        frozen = ledger.component_map_frozen(repo.repo_id)
        if frozen is None:
            return _refuse(stdout, RUN, NO_MAP)
        ctx = (ledger, repo, from_json(frozen), now)
        return _run_all(ctx, praxis_home, scenarios, stdout)
    finally:
        ledger.close()


def _run_all(
    ctx: Ctx, praxis_home: Path, scenarios: list[Scenario], stdout: TextIO
) -> int:
    _, repo, cmap, _ = ctx
    if not _git(repo.root, "rev-parse", "--verify", "--quiet", "HEAD^{tree}"):
        return _refuse(stdout, RUN, "no commit; a scenario runs a clone of HEAD")
    stdout.write(f"scenarios on map {cmap.version}, each in a scratch clone of HEAD\n")
    for scenario in scenarios:
        tree, trace = _run_one(repo.root, praxis_home, cmap, scenario)
        _record(ctx, scenario, tree, trace)
        stdout.write(_line(trace, tree) + "\n")
    return 0


def _run_one(
    root: Path, praxis_home: Path, cmap: ComponentMap, scenario: Scenario
) -> tuple[str, Trace]:
    clone = scratch_clone(root, praxis_home)
    try:
        tree = _git(clone, "rev-parse", "HEAD^{tree}", check=True).strip()
        with _environment(clone, _roots(clone, cmap)):
            trace = run_scenario(clone, scenario, partial(assign, cmap), TIMEOUT_S)
    finally:
        shutil.rmtree(clone)
    return tree, trace


def _roots(clone: Path, cmap: ComponentMap) -> list[Path]:
    """The clone's root, then the directory above each top-level package that holds a
    product file of the frozen map."""
    listed = [p for p in _git(clone, "ls-files", "-z", check=True).split("\0") if p]
    tracked = set(listed)
    product = set(product_components(cmap))
    roots = {PurePosixPath(".")}
    for path in listed:
        package = PurePosixPath(path).parent
        in_package = str(package / "__init__.py") in tracked
        if not in_package or assign(cmap, path) not in product:
            continue
        while str(package.parent / "__init__.py") in tracked:
            package = package.parent
        roots.add(package.parent)
    return [clone / r for r in sorted(roots)]


@contextlib.contextmanager
def _environment(clone: Path, roots: list[Path]) -> Iterator[None]:
    """run_scenario hands this process's environment to the scenario, so the two
    variables are set here for the run and restored after it."""
    saved = {key: os.environ.get(key) for key in ("PYTHONPATH", "PRAXIS_HOME")}
    inherited = [saved["PYTHONPATH"]] if saved["PYTHONPATH"] else []
    os.environ["PYTHONPATH"] = os.pathsep.join([*map(str, roots), *inherited])
    os.environ["PRAXIS_HOME"] = str(clone / ".praxis-scenario-home")
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _record(ctx: Ctx, scenario: Scenario, tree: str, trace: Trace) -> None:
    ledger, repo, cmap, now = ctx
    ev = Evidence(
        COVERAGE[trace.outcome],
        [f"scenario:{scenario.id}", f"tree:{tree}", f"map:{cmap.version}"],
        REDUCER_VERSION,
        ASSUMPTIONS,
        [] if trace.outcome == "ok" else [f"outcome_{trace.outcome}"],
    )
    ledger.scenario_run(
        components=sorted(trace.components),
        call_edges=trace.call_edges,
        ev=ev,
        repo_id=repo.repo_id,
        map_version=cmap.version,
        ts=now,
        scenario_id=scenario.id,
        command_hash=scenario.command_hash,
        outcome=trace.outcome,
        exit_code=trace.exit_code,
        duration_ms=trace.duration_ms,
        extractor_version=EXTRACTOR_VERSION,
        tree_hash=tree,
    )


def _line(trace: Trace, tree: str) -> str:
    code = "unknown" if trace.exit_code is None else str(trace.exit_code)
    seen = " ".join(sorted(trace.components)) or "none seen"
    return (
        f"  {trace.scenario_id}: {trace.outcome} (exit {code}) on tree {tree[:12]},"
        f" components {seen}, {len(trace.call_edges)} call edges,"
        f" {trace.duration_ms:.0f} ms"
    )


def show(repo_root: Path, praxis_home: Path, stdout: TextIO) -> int:
    """`praxis show scenarios`: each configured scenario and its last run on the frozen
    map. Read-only: it never creates or migrates the ledger."""
    repo = locate(repo_root)
    if repo is None:
        return _refuse(stdout, SHOW, "not inside a git repository")
    try:
        scenarios = load(praxis_home, repo.repo_id)
    except ValueError as exc:
        return _refuse(stdout, SHOW, str(exc))
    where = config_path(praxis_home, repo.repo_id)
    if scenarios is None:
        stdout.write(f"no scenarios configured; {where} names them\n")
        return 0
    last = _last_runs(praxis_home, repo.repo_id)
    head = _git(repo.root, "rev-parse", "--verify", "--quiet", "HEAD^{tree}").strip()
    stdout.write(f"{len(scenarios)} scenarios in {where}\n")
    for scenario in scenarios:
        stdout.write(f"  {scenario.id}: {_last(last, scenario, head)}\n")
    return 0


def summary(praxis_home: Path, repo_id: str) -> str:
    """The `praxis show status` line: scenarios configured and their last run on the
    frozen map."""
    try:
        scenarios = load(praxis_home, repo_id)
    except ValueError:
        return f"scenarios: {config_path(praxis_home, repo_id)} refused; `{SHOW}`"
    if scenarios is None:
        return "scenarios: none configured"
    last = _last_runs(praxis_home, repo_id)
    if isinstance(last, str):
        return f"scenarios: {len(scenarios)} configured; last run {last}"
    version, runs = last
    ran = [runs[s.id] for s in scenarios if s.id in runs]
    if not ran:
        return f"scenarios: {len(scenarios)} configured; none run on map {version}"
    outcomes = Counter(str(r["outcome"]) for r in ran)
    counted = ", ".join(f"{k} {n}" for k, n in sorted(outcomes.items()))
    return (
        f"scenarios: {len(scenarios)} configured, {len(ran)} run on map {version};"
        f" last run {max(str(r['ts']) for r in ran)} ({counted})"
    )


def _last_runs(
    praxis_home: Path, repo_id: str
) -> tuple[str, dict[str, sqlite3.Row]] | str:
    """(frozen map version, latest run per scenario id on it), or why it is unknown."""
    path = praxis_home / "ledger.db"
    if not path.exists():
        return "unknown: no ledger"
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            (schema,) = db.execute("PRAGMA user_version").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return "unknown: the ledger cannot be read"
    if schema != SCHEMA_VERSION:  # Store.open would migrate it
        return "unknown until the next hook event migrates the ledger"
    ledger = Store(path).open()
    try:
        frozen = ledger.component_map_frozen(repo_id)
        if frozen is None:
            return "unknown: no set component map"
        version = from_json(frozen).version
        rows = ledger.scenario_runs_for(repo_id, version)
    finally:
        ledger.close()
    # Ordered by ts then id, so each scenario's later run replaces its earlier one.
    return version, {str(r["scenario_id"]): r for r in rows}


def _last(
    last: tuple[str, dict[str, sqlite3.Row]] | str, scenario: Scenario, head: str
) -> str:
    if isinstance(last, str):
        return f"last run {last}"
    version, runs = last
    if scenario.id not in runs:
        return f"never run on map {version}"
    row = runs[scenario.id]
    tree = row["tree_hash"]
    where = "unknown" if tree is None else "HEAD" if tree == head else str(tree)[:12]
    changed = "" if row["command_hash"] == scenario.command_hash else "; argv changed"
    return (
        f"last run {row['ts']} {row['outcome']} on tree {where}, components"
        f" {' '.join(json.loads(row['components_json'])) or 'none seen'}{changed}"
    )


def _refuse(stdout: TextIO, verb: str, why: str) -> int:
    stdout.write(f"{verb}: {why}\n")
    return 2


def _git(cwd: Path, *args: str, check: bool = False) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=check,
        timeout=GIT_TIMEOUT_S,
    )
    return done.stdout.decode("utf-8", "surrogateescape")
