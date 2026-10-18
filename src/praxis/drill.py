"""`praxis drill`: one write-it item, in place, scored by the project's own tests.

Design: docs/design/02-instrument.md. The owner's repository is changed in exactly one
way, the body of one function, and restored byte for byte from a named backup whatever
happens in between.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from praxis import spans
from praxis.gitdiff import repo_id as make_repo_id
from praxis.gitdiff import toplevel
from praxis.store import ItemRow, ResultRow, Store

DEFAULT_MIN_FRACTION = 0.5
TEST_TIMEOUT_S = 600
AGENT_NAMES = frozenset({"claude", "codex"})
VALIDATION_NOTE = "validating; do not edit, the file is restored in a moment"


@dataclass(frozen=True)
class Repo:
    root: Path
    repo_id: str


def _now() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def locate(cwd: Path) -> Repo | None:
    top = toplevel(cwd)
    return None if top is None else Repo(Path(top), make_repo_id(top))


# -- agent presence ----------------------------------------------------------------


def attached_agents(root: Path) -> list[str]:
    """Agent processes working inside the repository. From /proc where it exists; where
    it does not, any agent process anywhere, which is the stricter reading."""
    proc = Path("/proc")
    return _agents_from_proc(proc, root) if proc.is_dir() else _agents_from_ps()


def _agents_from_proc(proc: Path, root: Path) -> list[str]:
    found: list[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        name = _agent_name(entry)
        if name is None:
            continue
        try:
            cwd = (entry / "cwd").readlink()
        except OSError:
            continue
        if cwd == root or root in cwd.parents:
            found.append(f"{name} pid {entry.name} in {cwd}")
    return found


def _agents_from_ps() -> list[str]:
    try:
        listing = subprocess.run(
            ["ps", "-eo", "pid=,comm=,args="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return ["process listing unavailable; cannot rule an agent out"]
    found: list[str] = []
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and _basename_in(parts[1:]):
            found.append(f"{parts[1]} pid {parts[0]} (cwd unknown)")
    return found


def _agent_name(entry: Path) -> str | None:
    try:
        argv = (entry / "cmdline").read_bytes().split(b"\x00")
        comm = (entry / "comm").read_text().strip()
    except OSError:
        return None
    names = [comm, *(Path(a.decode("utf-8", "replace")).name for a in argv[:2] if a)]
    return next((n for n in names if n in AGENT_NAMES), None)


def _basename_in(fields: list[str]) -> bool:
    return any(Path(f.split()[0]).name in AGENT_NAMES for f in fields if f.strip())


# -- running the owner's tests -----------------------------------------------------


def tests_pass(root: Path, test_cmd: str) -> bool | None:
    """True, False, or None when the command could not be run at all."""
    try:
        done = subprocess.run(
            shlex.split(test_cmd),
            cwd=root,
            capture_output=True,
            timeout=TEST_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return done.returncode == 0


def validate(root: Path, test_cmd: str, cand: spans.Candidate) -> str:
    """'valid', or 'void: <reason>'. Runs the tests twice; leaves the file as found."""
    path = root / cand.file
    original = path.read_bytes()
    before = tests_pass(root, test_cmd)
    if before is None:
        return "void: test command could not run"
    if not before:
        return "void: tests fail on the untouched file"
    try:
        path.write_bytes(
            spans.strip_body(
                original.decode("utf-8", "surrogateescape"),
                cand.function,
                note=VALIDATION_NOTE,
            ).encode("utf-8", "surrogateescape")
        )
        stripped = tests_pass(root, test_cmd)
    finally:
        path.write_bytes(original)
    if stripped is None:
        return "void: test command could not run"
    if stripped:
        return "void: tests pass without the body"
    return "valid"


# -- the drill ---------------------------------------------------------------------


def backup_dir(home: Path) -> Path:
    return home / "drills"


def pending_backups(home: Path) -> list[Path]:
    root = backup_dir(home)
    return (
        sorted(p for p in root.glob("*/original") if p.is_file())
        if root.is_dir()
        else []
    )


def restore(home: Path, out: TextIO) -> int:
    restored = 0
    for original in pending_backups(home):
        target = (original.parent / "target").read_text().strip()
        Path(target).write_bytes(original.read_bytes())
        shutil.rmtree(original.parent)
        out.write(f"restored {target}\n")
        restored += 1
    if not restored:
        out.write("nothing to restore\n")
    return 0


@dataclass(frozen=True)
class Ready:
    repo: Repo
    cmd: str


def _preflight(
    cwd: Path, home: Path, store: Store, test_cmd: str | None, out: TextIO
) -> Ready | int:
    """The repository and the test command, or the exit code that explains why not."""
    repo = locate(cwd)
    if repo is None:
        out.write("praxis drill: not inside a git repository\n")
        return 2
    if pending_backups(home):
        out.write(
            "a previous drill left a backup; run `praxis drill --restore` first\n"
        )
        return 2
    cmd = test_cmd or store.test_cmd(repo.repo_id)
    if cmd is None:
        out.write("first drill in this repository: pass --test-cmd '<command>' once\n")
        return 2
    if test_cmd is not None:
        store.set_test_cmd(repo.repo_id, test_cmd, _now())
    return Ready(repo, cmd)


def _list(found: list[spans.Candidate], min_fraction: float, out: TextIO) -> int:
    for c in found:
        mark = "eligible" if c.fraction >= min_fraction else "below threshold"
        out.write(
            f"{c.file}:{c.function.lineno} {c.function.qualname} "
            f"{c.agent_lines}/{c.total_lines} agent lines ({c.fraction:.2f})"
            f" {mark}\n"
        )
    if not found:
        out.write("no function in this repository has an agent-authored line\n")
    return 0


def _refuse(
    ready: Ready, agents: list[str], store: Store, min_fraction: float, out: TextIO
) -> int:
    item = _item(store, ready.repo, None, min_fraction, "refused: agent attached")
    store.result(_result(item, "refused", 0.0, ready.cmd, agents))
    out.write("refused: an agent is attached to this repository\n")
    for a in agents:
        out.write(f"  {a}\n")
    return 3


def run(
    *,
    cwd: Path,
    home: Path,
    store: Store,
    test_cmd: str | None,
    min_fraction: float,
    list_only: bool,
    stdin: TextIO,
    out: TextIO,
) -> int:
    ready = _preflight(cwd, home, store, test_cmd, out)
    if isinstance(ready, int):
        return ready
    found = spans.candidates(store, ready.repo.root, ready.repo.repo_id)
    if list_only:
        return _list(found, min_fraction, out)
    agents = attached_agents(ready.repo.root)
    if agents:
        return _refuse(ready, agents, store, min_fraction, out)
    eligible = sorted(
        (c for c in found if c.fraction >= min_fraction),
        key=lambda c: (-c.fraction, c.file, c.function.lineno),
    )
    for cand in eligible:
        verdict = validate(ready.repo.root, ready.cmd, cand)
        item = _item(store, ready.repo, cand, min_fraction, verdict)
        if verdict == "valid":
            return _drill(item, cand, ready, home, store, stdin, out)
    out.write(
        f"no valid item: {len(found)} candidate functions, {len(eligible)} over the"
        f" {min_fraction:.2f} threshold, none both covered by the tests and passing\n"
    )
    return 1


def _drill(
    item: ItemRow,
    cand: spans.Candidate,
    ready: Ready,
    home: Path,
    store: Store,
    stdin: TextIO,
    out: TextIO,
) -> int:
    path = ready.repo.root / cand.file
    original = path.read_bytes()
    keep = backup_dir(home) / item.item_id
    keep.mkdir(parents=True, exist_ok=True)
    (keep / "original").write_bytes(original)
    (keep / "target").write_text(str(path))
    stripped = spans.strip_body(
        original.decode("utf-8", "surrogateescape"), cand.function
    )
    path.write_bytes(stripped.encode("utf-8", "surrogateescape"))
    out.write(
        f"write-it: {cand.file}:{cand.function.body_start} {cand.function.qualname}\n"
        f"The body is gone; the docstring and the tests remain. Write the body, then\n"
        f"press Enter to run: {ready.cmd}\n"
        f"(q aborts and restores the file)\n"
    )
    out.flush()
    started = time.perf_counter()
    answer = stdin.readline().strip().lower()
    elapsed = time.perf_counter() - started
    outcome = "aborted"
    try:
        if answer != "q":
            passed = tests_pass(ready.repo.root, ready.cmd)
            outcome = "pass" if passed else "fail" if passed is False else "aborted"
    finally:
        path.write_bytes(original)
        shutil.rmtree(keep, ignore_errors=True)
    warnings = [] if answer != "q" else ["owner aborted"]
    store.result(_result(item, outcome, elapsed, ready.cmd, warnings))
    out.write(f"{outcome} in {elapsed:.1f} s; {cand.file} restored\n")
    return 0


def _item(
    store: Store,
    repo: Repo,
    cand: spans.Candidate | None,
    min_fraction: float,
    validation: str,
) -> ItemRow:
    row = ItemRow(
        item_id=uuid.uuid4().hex,
        repo_id=repo.repo_id,
        created_ts=_now(),
        kind="write-it",
        file=cand.file if cand else None,
        qualname=cand.function.qualname if cand else None,
        agent_lines=cand.agent_lines if cand else None,
        total_lines=cand.total_lines if cand else None,
        min_fraction=min_fraction,
        sources=cand.sessions if cand else [f"repo:{repo.repo_id}"],
        validation=validation,
    )
    store.item(row)
    return row


def _result(
    item: ItemRow, outcome: str, elapsed: float, cmd: str, warnings: list[str]
) -> ResultRow:
    return ResultRow(
        item_id=item.item_id,
        ts=_now(),
        outcome=outcome,
        elapsed_s=elapsed,
        test_cmd_hash=hashlib.sha256(cmd.encode()).hexdigest()[:16],
        sources=[f"item:{item.item_id}", "tests:project"],
        warnings=warnings,
    )
