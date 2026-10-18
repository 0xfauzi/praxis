"""From the ledger's line hashes to agent-authored lines and the functions holding them.

Nothing here reads stored text, because none is stored: the current file is hashed line
by line the way the recorder hashes a ``+`` line, and each hash is looked up. The file
is read at drill time from the owner's working tree and never written anywhere.

Design: docs/design/02-instrument.md.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

from praxis.gitdiff import line_hash

if TYPE_CHECKING:
    from pathlib import Path

    from praxis.store import Store


@dataclass(frozen=True)
class Function:
    qualname: str
    lineno: int
    end_lineno: int
    body_start: int  # first statement after the docstring, or 0 when there is none


@dataclass(frozen=True)
class Candidate:
    file: str
    function: Function
    agent_lines: int
    total_lines: int
    sessions: list[str]

    @property
    def fraction(self) -> float:
        return self.agent_lines / self.total_lines


def agent_line_numbers(content: bytes, hashes: set[str]) -> list[int]:
    lines = content.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return [i + 1 for i, line in enumerate(lines) if line_hash(b"+", line) in hashes]


def functions(source: str) -> list[Function]:
    """Every function and method, by qualified name, with where its real body starts."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[Function] = []

    def visit(nodes: list[ast.stmt], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append(_function(node, prefix))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    return found


def _function(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str) -> Function:
    body = node.body
    has_doc = (
        bool(body)
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    )
    rest = body[1:] if has_doc else body
    start = rest[0].lineno if rest else 0
    end = node.end_lineno if node.end_lineno is not None else node.lineno
    return Function(f"{prefix}{node.name}", node.lineno, end, start)


def candidates(store: Store, repo_root: Path, repo_id: str) -> list[Candidate]:
    """Functions with at least one agent-authored line, across every session on this
    repository. The caller applies the fraction threshold, so that the threshold is a
    recorded parameter rather than something buried here."""
    out: list[Candidate] = []
    for file in store.agent_files(repo_id):
        if file.endswith(".py"):
            out.extend(_file_candidates(store, repo_root, repo_id, file))
    return out


def _file_candidates(
    store: Store, repo_root: Path, repo_id: str, file: str
) -> list[Candidate]:
    try:
        content = (repo_root / file).read_bytes()
    except OSError:
        return []
    hashes, sessions = store.agent_hashes(repo_id, file)
    flagged = set(agent_line_numbers(content, hashes))
    if not flagged:
        return []
    out: list[Candidate] = []
    for fn in functions(content.decode("utf-8", "surrogateescape")):
        if fn.body_start == 0:
            continue
        span = range(fn.lineno, fn.end_lineno + 1)
        hit = sum(1 for n in span if n in flagged)
        if hit:
            out.append(Candidate(file, fn, hit, len(span), sessions))
    return out


def strip_body(source: str, fn: Function, note: str = "write this body") -> str:
    """The source with the function's body, after its docstring, replaced by one raise.

    ``note`` is the comment on that line: the drill's invitation, or the validation
    pass's warning, so that a reader who sees the file mid-validation knows not to edit.
    """
    lines = source.split("\n")
    first = lines[fn.body_start - 1]
    indent = first[: len(first) - len(first.lstrip())]
    replacement = f"{indent}raise NotImplementedError  # praxis write-it: {note}"
    return "\n".join(
        [*lines[: fn.body_start - 1], replacement, *lines[fn.end_lineno :]]
    )
