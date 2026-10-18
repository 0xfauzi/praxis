"""Working-tree snapshots as multisets of line hashes, from git alone.

A snapshot is, per repo-relative path, a Counter of line hashes, where a hash covers the
diff kind (``+`` or ``-``) and the exact bytes of the line. A line the agent added and a
line it removed are therefore different things, and two identical added lines count
twice. Nothing here keeps a line: the hash is computed and the bytes are dropped.

Design: docs/design/01-recorder.md.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 2.0
# An untracked file above this is skipped and named on the row's warnings. 1,000,000 is
# a cap on hook latency, not a judgement about files; the number needs measuring against
# the owner's repositories and is deliberately generous meanwhile.
UNTRACKED_MAX_BYTES = 1_000_000

Files = dict[str, Counter[str]]


@dataclass(frozen=True)
class Snap:
    """One working-tree snapshot. ``skipped`` holds "path: reason" per file left out."""

    toplevel: str
    files: Files
    skipped: tuple[str, ...]


@dataclass(frozen=True)
class Unobservable:
    """Why no snapshot could be taken. The caller records it and attributes nothing."""

    reason: str


def line_hash(kind: bytes, line: bytes) -> str:
    return hashlib.sha256(kind + b"\x00" + line).hexdigest()[:32]


def repo_id(toplevel_path: str) -> str:
    """How the ledger names a repository: a hash of its toplevel path, not the path."""
    return hashlib.sha256(toplevel_path.encode("utf-8", "surrogateescape")).hexdigest()[
        :16
    ]


def toplevel(cwd: str | Path, timeout_s: float = GIT_TIMEOUT_S) -> str | None:
    """The repository root for ``cwd``; None without a repository or without git."""
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel", timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if top.returncode != 0:
        return None
    return top.stdout.decode("utf-8", "surrogateescape").strip()


def _git(
    cwd: str | Path, *args: str, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    # Never shell=True: ``cwd`` comes from the hook payload. The two -c settings stop a
    # pager and path quoting from changing the bytes we hash.
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "core.pager=cat",
            "-c",
            "core.quotePath=false",
            *args,
        ],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def snapshot(cwd: str | Path, timeout_s: float = GIT_TIMEOUT_S) -> Snap | Unobservable:
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel", timeout=timeout_s)
    except FileNotFoundError:
        return Unobservable("git not found")
    except subprocess.TimeoutExpired:
        return Unobservable("git timed out")
    except OSError as exc:
        return Unobservable(f"cannot run git: {type(exc).__name__}")
    if top.returncode != 0:
        return Unobservable("not a git repository")
    toplevel = top.stdout.decode("utf-8", "surrogateescape").strip()
    try:
        diff = _git(
            toplevel,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-renames",
            "--no-prefix",
            "--unified=0",
            "--diff-algorithm=myers",
            "HEAD",
            timeout=timeout_s,
        )
        others = _git(
            toplevel,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return Unobservable("git timed out")
    if diff.returncode != 0:
        err = diff.stderr.decode("utf-8", "replace")
        if "ambiguous argument 'HEAD'" in err or "bad revision" in err:
            return Unobservable("no HEAD commit")
        return Unobservable(f"git diff failed: {err.strip()[:120]}")
    if others.returncode != 0:
        return Unobservable("git ls-files failed")
    files, skipped = _parse_diff(diff.stdout)
    _add_untracked(Path(toplevel), others.stdout, files, skipped)
    return Snap(toplevel=toplevel, files=files, skipped=tuple(skipped))


@dataclass
class _FileHeader:
    """Where the parser is inside one file's section of the diff."""

    path: str | None = None
    minus_path: str | None = None
    open: bool = False


def _parse_diff(out: bytes) -> tuple[Files, list[str]]:
    """Unified diff with --unified=0 and --no-prefix into per-file line-hash multisets.

    A state machine rather than prefix matching: a content line that begins with ``++``
    prints as ``+++ ...``, so ``+++``/``---`` are headers only while a file's header is
    open, that is between its ``diff --git`` line and its first ``@@``.
    """
    files: Files = {}
    skipped: list[str] = []
    header = _FileHeader()
    for raw in out.split(b"\n"):
        if raw.startswith(b"diff --git "):
            header = _FileHeader(open=True)
        elif header.open:
            _read_header_line(raw, header, skipped)
        elif raw[:1] in (b"+", b"-") and header.path is not None:
            files.setdefault(header.path, Counter())[line_hash(raw[:1], raw[1:])] += 1
    return files, skipped


def _read_header_line(raw: bytes, header: _FileHeader, skipped: list[str]) -> None:
    if raw.startswith(b"--- "):
        header.minus_path = _header_path(raw[4:])
    elif raw.startswith(b"+++ "):
        plus = _header_path(raw[4:])
        header.path = plus if plus is not None else header.minus_path
    elif raw.startswith(b"Binary files "):
        skipped.append(f"{header.path or header.minus_path or '?'}: binary")
    elif raw.startswith(b"@@"):
        header.open = False
        if header.path is None:
            header.path = header.minus_path


def _header_path(raw: bytes) -> str | None:
    text = raw.decode("utf-8", "surrogateescape")
    if text == "/dev/null":
        return None
    # git appends a tab before a trailing-space path; nothing else follows the name.
    return text.split("\t", 1)[0]


def _add_untracked(
    toplevel: Path, listing: bytes, files: Files, skipped: list[str]
) -> None:
    for raw in listing.split(b"\x00"):
        if not raw:
            continue
        path = raw.decode("utf-8", "surrogateescape")
        data = _read_untracked(toplevel / path, path, skipped)
        if data is None:
            continue
        counter = files.setdefault(path, Counter())
        for line in _lines(data):
            counter[line_hash(b"+", line)] += 1


def _read_untracked(full: Path, path: str, skipped: list[str]) -> bytes | None:
    try:
        size = full.stat().st_size
        if size > UNTRACKED_MAX_BYTES:
            skipped.append(f"{path}: {size} bytes, over cap")
            return None
        data = full.read_bytes()
    except OSError:
        skipped.append(f"{path}: unreadable")
        return None
    if b"\x00" in data:
        skipped.append(f"{path}: binary")
        return None
    return data


def _lines(data: bytes) -> list[bytes]:
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return parts
