"""X1 per language: the protocol every import reader implements, and the registry that
loads a reader only when a scan holds a file with one of its suffixes.

The hook pays for every import it makes, so a reader module is imported by ``load`` and
never at the top of this package: what a scan may need is known from suffixes alone,
which the registry holds without importing any reader. Python is the only language
registered; another lands only after its own gate (docs/design/04-languages.md
section 8).

Design: docs/design/04-languages.md section 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

R = TypeVar("R")


@dataclass(frozen=True)
class ImportSite:
    """One import a reader saw. A statement that names several modules is one site per
    name, all with the statement's offsets, and the lift counts it once."""

    start: int
    end: int
    specifier: str | None
    kind: str
    deferred: bool
    type_only: bool
    blind: str | None


@dataclass(frozen=True)
class Resolved:
    target: str | None
    reason: str


# The reason codes that make a site a blind spot, for every language (design 2.2).
BLIND = frozenset(
    {
        "dynamic",
        "relative_missing",
        "alias_unconfigured",
        "unresolved_local",
        "ambiguous",
        "nul",
    }
)


class Unparsed(Exception):
    """The reader could not read the source: the file is a parse failure, never a file
    with no imports."""


class Language(Protocol[R]):
    NAME: str
    VERSION: str
    SUFFIXES: tuple[str, ...]
    KINDS: frozenset[str]
    REASONS: frozenset[str]

    def files(self, repo_root: Path, listed: list[str]) -> list[str]: ...

    def roots(self, repo_root: Path, listed: list[str]) -> R: ...

    def imports(self, path: str, source: bytes) -> list[ImportSite]: ...

    def resolve(self, site: ImportSite, importer: str, roots: R) -> Resolved: ...


@dataclass(frozen=True)
class Registered:
    name: str
    version: str
    suffixes: tuple[str, ...]
    load: Callable[[], Language[Any]]


def _python() -> Language[Any]:
    from praxis.lang.python import LANGUAGE

    return LANGUAGE


# x1-v2: a resolved TYPE_CHECKING import stopped counting as a blind spot. The version
# is never renamed: file_edges rows are keyed by it (design 6).
PYTHON = Registered("python", "x1-v2", (".py",), _python)
REGISTERED = (PYTHON,)


def claimant(path: str) -> Registered | None:
    return next((r for r in REGISTERED if path.endswith(r.suffixes)), None)
