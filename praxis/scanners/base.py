"""Scanner base class.

A Scanner knows how to find chat history files on disk for one AI
provider and turn them into normalized Sessions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from praxis.models import Session


class BaseScanner(ABC):
    """One implementation per provider."""

    provider_name: str

    @abstractmethod
    def discover(self) -> Iterator[Path]:
        """Yield paths to candidate session files on this machine."""

    @abstractmethod
    def parse(self, path: Path) -> Session | None:
        """Read one file and return a Session, or None if unreadable."""

    def scan(self, since: float | None = None) -> Iterator[Session]:
        """Yield all sessions, optionally filtered by mtime."""
        for path in self.discover():
            if since is not None:
                try:
                    if path.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
            session = self.parse(path)
            if session is not None and session.turn_count > 0:
                yield session
