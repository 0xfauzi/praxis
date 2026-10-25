"""X1 per language: the protocol every import reader implements, and the registry that
loads a reader only when a scan holds a file with one of its suffixes.

The hook pays for every import it makes, so a reader module is imported by ``load`` and
never at the top of this package: what a scan may need is known from suffixes alone,
which the registry holds without importing any reader. A language lands only after its
own gate (docs/design/04-languages.md section 8).

Design: docs/design/04-languages.md section 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
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
        # An import() type in a JSDoc comment (design 1.4); 2.2's vocabulary has no code
        # for it, and none of the others names it.
        "jsdoc",
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

    def roots(self, repo_root: Path, listed: list[str]) -> R:
        """``listed`` holds the reader's own files present in the tree, the files the
        scan names for it, and every listed file no registered language claims (its
        build files): never another language's files."""
        ...

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


def _typescript() -> Language[Any]:
    from praxis.lang.typescript import LANGUAGE

    return LANGUAGE


# x1-v2: a resolved TYPE_CHECKING import stopped counting as a blind spot. The version
# is never renamed: file_edges rows are keyed by it (design 6).
PYTHON = Registered("python", "x1-v2", (".py",), _python)
TYPESCRIPT = Registered(
    "typescript",
    "x1-ts-v1",
    (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    _typescript,
)
REGISTERED = (PYTHON, TYPESCRIPT)


def claimant(path: str) -> Registered | None:
    return next((r for r in REGISTERED if path.endswith(r.suffixes)), None)


# Design 6: every file is in one class by suffix: a registered language, a programming
# language no registered reader covers, non-code, or an unknown suffix, which is never
# folded into another class. Both tables are closed; a suffix in neither is unknown.
UNMAPPED_LANGUAGE = "unmapped_language"
NON_CODE = "non_code"
UNKNOWN = "unknown"
_UNCOVERED = frozenset({
    ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".rb", ".php", ".cs", ".scala",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm", ".ex",
    ".exs", ".clj", ".cljs", ".cljc", ".dart", ".lua", ".jl", ".hs", ".ml", ".mli",
    ".vue", ".svelte", ".astro", ".fs", ".fsx", ".vb", ".pl", ".pm", ".r", ".groovy",
    ".erl", ".hrl", ".elm", ".nim", ".zig", ".sol",
})  # fmt: skip
_NON_CODE = frozenset({
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".css",
    ".scss", ".sass", ".less", ".html", ".htm", ".svg", ".sql", ".md", ".mdx", ".rst",
    ".txt", ".csv", ".tsv", ".xml", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".bmp", ".avif", ".pdf", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
    ".wav", ".ogg", ".webm", ".lock",
})  # fmt: skip


def file_class(path: str) -> str:
    entry = claimant(path)
    if entry is not None:
        return entry.name
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _UNCOVERED:
        return UNMAPPED_LANGUAGE
    return NON_CODE if suffix in _NON_CODE else UNKNOWN
