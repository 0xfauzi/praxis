"""Python's import reader (x1-v2): import statements and importlib calls read by
``ast``, resolved against a module index of the scan's Python files.

A ``from`` import is one site per imported name, and its specifier is the dotted name
the statement could import (``a.b`` for ``from a import b``, ``..a.b`` for ``from ..a
import b``); an importer may have two module names, so a relative specifier resolves
from each in turn. Reason codes: ``module`` and ``relative`` resolved to one file;
``ambiguous`` matched two files and is a blind spot; ``external`` matched no listed
file, which includes a relative import that names nothing (x1-v2 does not count that as
blind); ``dynamic`` is an ``__import__`` or ``import_module`` call.

Design: docs/design/03-measurement.md section 3.1; docs/design/04-languages.md 2.1.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass
from itertools import accumulate
from typing import TYPE_CHECKING, override

from praxis.lang import PYTHON, ImportSite, Language, Resolved, Unparsed

if TYPE_CHECKING:
    from pathlib import Path

_DYNAMIC = frozenset({"__import__", "import_module"})

Import = ast.Import | ast.ImportFrom


class _Sites(ast.NodeVisitor):
    """Import sites in source order, from statements and importlib calls."""

    def __init__(self, starts: list[int]) -> None:
        self.sites: list[ImportSite] = []
        self._starts = starts
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
        start, end = _span(node, self._starts)
        kind = "import" if isinstance(node, ast.Import) else "from"
        deferred = self._depth > 0 or self._typing
        for spec in _specifiers(node):
            site = ImportSite(start, end, spec, kind, deferred, self._typing, None)
            self.sites.append(site)

    visit_ImportFrom = visit_Import

    def visit_Call(self, node: ast.Call) -> None:
        if _name(node.func) in _DYNAMIC:
            start, end = _span(node, self._starts)
            deferred = self._depth > 0 or self._typing
            site = ImportSite(start, end, None, "dynamic", deferred, False, "dynamic")
            self.sites.append(site)
        self.generic_visit(node)


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _span(node: ast.stmt | ast.expr, starts: list[int]) -> tuple[int, int]:
    # Offsets into the UTF-8 text ast reads: byte offsets when the file is UTF-8, and
    # under another coding cookie still distinct per statement.
    if node.end_lineno is None or node.end_col_offset is None:
        raise Unparsed("ast.parse sets an end on every statement and expression")
    start = starts[node.lineno - 1] + node.col_offset
    return start, starts[node.end_lineno - 1] + node.end_col_offset


def _specifiers(node: Import) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    dots = "." * node.level
    if node.level == 0 or node.module:
        return [f"{dots}{node.module or ''}.{alias.name}" for alias in node.names]
    return [f"{dots}{alias.name}" for alias in node.names]


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


def _relative_base(name: str, is_init: bool, level: int, rest: str) -> str | None:
    package = name.split(".") if is_init else name.split(".")[:-1]
    keep = len(package) - (level - 1)
    if keep <= 0:
        return None
    return ".".join([*package[:keep], rest])


def _candidates(specifier: str, names: list[str], is_init: bool) -> list[str]:
    """The module names to try in order; the first one indexed wins."""
    rest = specifier.lstrip(".")
    level = len(specifier) - len(rest)
    if level == 0:
        return _prefixes(specifier)
    found = (_relative_base(n, is_init, level, rest) for n in names)
    return [c for base in found if base is not None for c in _prefixes(base)]


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


class _Python(Language[_Index]):
    NAME = PYTHON.name
    VERSION = PYTHON.version
    SUFFIXES = PYTHON.suffixes
    KINDS = frozenset({"import", "from", "dynamic"})
    REASONS = frozenset({"module", "relative", "external", "ambiguous", "dynamic"})

    @override
    def files(self, repo_root: Path, listed: list[str]) -> list[str]:
        return [
            p for p in listed if p.endswith(self.SUFFIXES) and (repo_root / p).is_file()
        ]

    @override
    def roots(self, repo_root: Path, listed: list[str]) -> _Index:
        return _Index.build([p for p in listed if p.endswith(self.SUFFIXES)])

    @override
    def imports(self, path: str, source: bytes) -> list[ImportSite]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise Unparsed(type(exc).__name__) from None
        lines = source.splitlines(keepends=True)
        visitor = _Sites([0, *accumulate(len(line) for line in lines)])
        visitor.visit(tree)
        return visitor.sites

    @override
    def resolve(self, site: ImportSite, importer: str, roots: _Index) -> Resolved:
        if site.specifier is None:
            return Resolved(None, "dynamic")
        is_init = importer.rsplit("/", 1)[-1] == "__init__.py"
        tries = _candidates(site.specifier, roots.names[importer], is_init)
        hits = next((roots.modules[c] for c in tries if c in roots.modules), [])
        if len(hits) > 1:
            return Resolved(None, "ambiguous")
        if not hits:
            return Resolved(None, "external")
        return Resolved(hits[0], "relative" if site.specifier[0] == "." else "module")


LANGUAGE = _Python()
