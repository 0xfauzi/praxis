"""Cross-process advisory lock so two scoring runs never write at once.

A `praxis scan` (or a current-week `praxis review`) and, later, the menu-bar
app can run concurrently; letting two of them score + write the same SQLite
profile at the same time risks lost work and lock contention. This guards the
write-heavy scoring path with a non-blocking advisory lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


class ScanLockError(RuntimeError):
    """Raised when another praxis run already holds the scan lock."""


@contextmanager
def scan_lock(home: Path):
    """Hold an exclusive, non-blocking advisory lock for the duration.

    POSIX: ``fcntl.flock`` on ``<home>/.scan.lock``. The lock is released when
    the file handle closes, which the OS does even on a crash, so a dead
    process never leaves a stale lock behind. On platforms without ``fcntl``
    (Windows) this is a best-effort no-op rather than a hard failure.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return

    home.mkdir(parents=True, exist_ok=True)
    handle = open(home / ".scan.lock", "w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ScanLockError(
                "another praxis scan or review is already running"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
