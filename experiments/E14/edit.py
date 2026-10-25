"""E14 shared shapes: the candidate an operator proposes, byte-offset splicing of source
text, and the function table the L4 and L5 recovery check compares. Standard library
only. Nothing here prints or keeps source text beyond the mutant it returns."""

from __future__ import annotations

import ast
import textwrap
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

Splice = tuple[int, int, int, int, str]
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(frozen=True)
class Candidate:
    """One proposed item. ``mutate`` maps the file's base text to the mutant's, or to
    None when the edit cannot be placed. ``key`` is the item at its level's grain;
    ``card`` holds identifiers only (design 05, section 4)."""

    level: str
    op: str
    intent: str
    path: str
    component: str
    key: tuple[str, ...]
    mutate: Callable[[str], str | None]
    card: dict[str, str] = field(default_factory=dict)
    qualname: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    alt: Callable[[str], str | None] | None = None


def parse(text: str) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _starts(data: bytes) -> list[int]:
    starts = [0]
    for i, byte in enumerate(data):
        if byte == 0x0A:
            starts.append(i + 1)
    return starts


def splice(text: str, edits: list[Splice]) -> str:
    """Apply (lineno, col, end_lineno, end_col, new) edits; columns are UTF-8 byte
    offsets, as ``ast`` gives them. Edits must not overlap."""
    data = text.encode()
    starts = _starts(data)
    spans = sorted(
        ((starts[a - 1] + b, starts[c - 1] + d, new) for a, b, c, d, new in edits),
        reverse=True,
    )
    for i in range(len(spans) - 1):
        if spans[i + 1][1] > spans[i][0]:
            raise ValueError("overlapping edits")
    for lo, hi, new in spans:
        data = data[:lo] + new.encode() + data[hi:]
    return data.decode()


def span(node: ast.expr | ast.stmt) -> tuple[int, int, int, int]:
    end_line = node.end_lineno if node.end_lineno is not None else node.lineno
    end_col = (
        node.end_col_offset if node.end_col_offset is not None else node.col_offset
    )
    return node.lineno, node.col_offset, end_line, end_col


def segment(text: str, node: ast.expr | ast.stmt) -> str | None:
    return ast.get_source_segment(text, node)


def own_nodes(fn: FunctionNode) -> Iterator[ast.AST]:
    """Nodes of ``fn``'s own body, not descending into nested scopes."""
    stack: list[ast.AST] = list(reversed(fn.body))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def functions(tree: ast.Module) -> list[tuple[str, FunctionNode]]:
    """Every function with its qualified name, nested ones included, in source order."""
    found: list[tuple[str, FunctionNode]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                found.append((name, child))
                walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def dumps(tree: ast.Module) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for name, fn in functions(tree):
        table.setdefault(name, []).append(ast.dump(fn))
    return table


def changed_functions(base: ast.Module, mutant: ast.Module) -> set[str]:
    a, b = dumps(base), dumps(mutant)
    return {name for name in a.keys() | b.keys() if a.get(name) != b.get(name)}


def enclosing(qualname: str) -> set[str]:
    """``qualname`` and every dotted prefix of it; class prefixes are harmless, because
    a class is never in the function table."""
    parts = qualname.split(".")
    return {".".join(parts[:k]) for k in range(1, len(parts) + 1)}


def own_line(text: str, node: ast.stmt) -> bool:
    """True when ``node`` starts its first line and ends its last one (a comment may
    follow), so replacing its lines replaces it alone."""
    lines = text.encode().split(b"\n")
    lineno, col, end_line, end_col = span(node)
    before = lines[lineno - 1][:col]
    after = lines[end_line - 1][end_col:].strip()
    return not before.strip() and (not after or after.startswith(b"#"))


def appended_function(text: str, name: str, body: str) -> str:
    """``text`` with a new module-level function ``name`` whose body is ``body``."""
    block = textwrap.indent(body, "    ")
    tail = "" if text.endswith("\n") else "\n"
    return f"{text}{tail}\n\ndef {name}():\n{block}\n"


def statement_source(text: str, node: ast.stmt) -> str | None:
    """The statement's source, dedented to column zero."""
    seg = segment(text, node)
    if seg is None:
        return None
    return textwrap.dedent(" " * node.col_offset + seg)
