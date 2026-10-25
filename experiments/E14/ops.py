"""E14 operators for L2 (flow), L3 (edge), L4 (mechanism) and L5 (form): candidate
enumeration over a scratch clone's product files. Each candidate carries a closure that
maps the base text to the mutant text; nothing is applied here. Standard library only.
"""

from __future__ import annotations

import ast
import zlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from edit import (
    Candidate,
    FunctionNode,
    Splice,
    appended_function,
    functions,
    own_line,
    own_nodes,
    parse,
    segment,
    span,
    splice,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sites import Repo

    from praxis.extract import _Index

DEP = "_e14_dep"
_FLIP = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">"}
_SYMBOL = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
_MIRROR = {ast.Lt: ">", ast.LtE: ">=", ast.Gt: "<", ast.GtE: "<="}
_IMPURE = (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)
_RENAME_BLOCKERS = frozenset({"locals", "vars", "eval", "exec"})


def _edits(edits: list[Splice]) -> Callable[[str], str | None]:
    return lambda text: splice(text, edits)


def _function_nodes(repo: Repo, path: str) -> Iterator[tuple[str, FunctionNode, str]]:
    text = repo.text(path)
    tree = parse(text)
    for qualname, fn in functions(tree) if tree else []:
        yield qualname, fn, text


# L2 ------------------------------------------------------------------------------


def _single_alias(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> ast.stmt:
    if isinstance(node, ast.Import):
        return ast.Import(names=[alias])
    return ast.ImportFrom(module=node.module, names=[alias], level=node.level)


def _local_targets(index: _Index, path: str, tree: ast.Module) -> dict[str, list[str]]:
    """Local name -> the files one import alias resolves to (any depth in the file).
    ``import a.b`` binds ``a``, not a module of ``a.b``, so it is left out."""
    found: dict[str, list[str]] = {}
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]
    pairs = [(node, alias) for node in nodes for alias in node.names]
    for node, alias in pairs:
        dotted = isinstance(node, ast.Import) and not alias.asname and "." in alias.name
        one = _single_alias(node, alias)
        if dotted or not isinstance(one, ast.Import | ast.ImportFrom):
            continue
        files, ambiguous = index.targets(path, one)
        if len(files) == 1 and not ambiguous:
            local = alias.asname or alias.name.split(".")[0]
            found.setdefault(local, []).extend(files)
    return found


def _callee_name(stmt: ast.stmt) -> str | None:
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    func = stmt.value.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return func.id if isinstance(func, ast.Name) else None


def _annotation_names(node: ast.expr | None) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node) if node is not None else []:
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            inner = parse(sub.value)
            names |= (
                {n.id for n in ast.walk(inner) if isinstance(n, ast.Name)}
                if inner
                else set()
            )
    return names


def _typed(fn: FunctionNode, local: dict[str, list[str]]) -> dict[str, list[str]]:
    """Parameter -> the files of the imported names its annotation uses, so that
    ``store.write(...)`` on ``store: Store`` resolves to Store's component."""
    every = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
    return {
        a.arg: [
            f for name in _annotation_names(a.annotation) for f in local.get(name, [])
        ]
        for a in every
    }


def _drop(
    repo: Repo, path: str, qualname: str, node: ast.AST, scope: dict[str, list[str]]
) -> Candidate | None:
    if not isinstance(node, ast.stmt):
        return None
    caller = repo.files[path]
    name = _callee_name(node)
    targets = {repo.files.get(f) for f in scope.get(name or "", [])}
    callee = targets.pop() if len(targets) == 1 else None
    if callee is None or callee == caller or not own_line(repo.text(path), node):
        return None
    card = {
        "template": "no_longer_calls",
        "component": caller,
        "callee": callee,
        "function": qualname,
    }
    return Candidate(
        "L2",
        "f_drop_call",
        "unknown",
        path,
        caller,
        (path, qualname, str(node.lineno)),
        _edits([(*span(node), "pass")]),
        card,
        qualname,
        {"caller": caller, "callee": callee},
    )


def drop_calls(repo: Repo, index: _Index, path: str) -> list[Candidate]:
    tree = parse(repo.text(path))
    local = _local_targets(index, path, tree) if tree else {}
    found: list[Candidate] = []
    for qualname, fn in functions(tree) if tree else []:
        scope = {**local, **_typed(fn, local)}
        for node in own_nodes(fn):
            candidate = _drop(repo, path, qualname, node, scope)
            if candidate is not None:
                found.append(candidate)
    return found


# L3 ------------------------------------------------------------------------------


def _insert_line(tree: ast.Module) -> int:
    """The line before which a new module-level import goes: after the last top-level
    import, else after the docstring, else at the top."""
    after = 0
    for i, node in enumerate(tree.body):
        is_doc = (
            i == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        if isinstance(node, ast.Import | ast.ImportFrom) or is_doc:
            after = node.end_lineno or node.lineno
    return after + 1


def _add_local_import(module: str) -> Callable[[str], str | None]:
    """The counted placement: inside a new private function, for faults and decoys
    alike, so the import never runs at import time and the card has no placement."""
    return lambda text: appended_function(
        text, DEP, f"import {module} as dependency\nreturn dependency"
    )


def _add_import(module: str) -> Callable[[str], str | None]:
    """The module-level placement, tested for import only (reported, not counted)."""

    def mutate(text: str) -> str | None:
        tree = parse(text)
        if tree is None:
            return None
        line = _insert_line(tree)
        lines = text.split("\n")
        if line > len(lines):
            return f"{text.rstrip(chr(10))}\nimport {module} as {DEP}\n"
        return splice(text, [(line, 0, line, 0, f"import {module} as {DEP}\n")])

    return mutate


def add_edges(
    repo: Repo,
    modules: dict[str, str],
    edges: set[tuple[str, str]],
    reach: set[tuple[str, str]],
) -> list[Candidate]:
    by_comp: dict[str, list[str]] = {}
    for path in sorted(repo.files):
        if repo.text(path).strip():
            by_comp.setdefault(repo.files[path], []).append(path)
    found: list[Candidate] = []
    for a in sorted(by_comp):
        for b in sorted(by_comp):
            back, forward = (b, a) in reach, (a, b) in reach
            if a == b or (a, b) in edges or (back and forward):
                continue
            src = by_comp[a][zlib.crc32(f"{a}>{b}".encode()) % len(by_comp[a])]
            dst = by_comp[b][zlib.crc32(b.encode()) % len(by_comp[b])]
            card = {"template": "new_dependency", "component": a, "depends_on": b}
            found.append(
                Candidate(
                    "L3",
                    "g_add_edge",
                    "fault" if back else "decoy",
                    src,
                    a,
                    (a, b),
                    _add_local_import(modules[dst]),
                    card,
                    extra={"src": a, "dst": b},
                    alt=_add_import(modules[dst]),
                )
            )
    return found


# L4 ------------------------------------------------------------------------------


def _single_line(node: ast.expr) -> bool:
    return node.lineno == node.end_lineno


def _negate(text: str, node: ast.If) -> Callable[[str], str | None] | None:
    seg = segment(text, node.test)
    is_not = isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
    if seg is None or is_not or not _single_line(node.test):
        return None
    return _edits([(*span(node.test), f"not ({seg})")])


def _gap(text: str, node: ast.Compare) -> tuple[Splice, bytes] | None:
    """The bytes between a single comparison's operands, with their splice span."""
    if len(node.ops) != 1 or type(node.ops[0]) not in _SYMBOL or not _single_line(node):
        return None
    left, right = node.left, node.comparators[0]
    lo, hi = left.end_col_offset, right.col_offset
    if lo is None or left.end_lineno != right.lineno:
        return None
    line = text.encode().split(b"\n")[right.lineno - 1]
    return (right.lineno, lo, right.lineno, hi, ""), line[lo:hi]


def _flip(text: str, node: ast.Compare) -> Callable[[str], str | None] | None:
    found = _gap(text, node)
    op = type(node.ops[0])
    if found is None or found[1].strip(b" ()").decode() != _SYMBOL[op]:
        return None
    (a, b, c, d, _), gap = found
    new = gap.decode().replace(_SYMBOL[op], _FLIP[op], 1)
    return _edits([(a, b, c, d, new)])


def _mirror(text: str, node: ast.Compare) -> Callable[[str], str | None] | None:
    found = _gap(text, node)
    if found is None or found[1].strip().decode() != _SYMBOL[type(node.ops[0])]:
        return None
    left, right = node.left, node.comparators[0]
    if any(isinstance(n, _IMPURE) for side in (left, right) for n in ast.walk(side)):
        return None
    ls, rs = segment(text, left), segment(text, right)
    if ls is None or rs is None:
        return None
    new = f"{rs} {_MIRROR[type(node.ops[0])]} {ls}"
    return _edits([(*span(node), new)])


def _is_elif(text: str, node: ast.If) -> bool:
    first = node.orelse[0]
    line = text.encode().split(b"\n")[first.lineno - 1]
    return isinstance(first, ast.If) and line.strip().startswith(b"elif")


def _block(lines: list[bytes], body: list[ast.stmt]) -> tuple[Splice, str]:
    first, last = body[0].lineno, body[-1].end_lineno or body[-1].lineno
    text = b"\n".join(lines[first - 1 : last]).decode()
    return (first, 0, last, len(lines[last - 1]), ""), text


def _swap(text: str, node: ast.If) -> Callable[[str], str | None] | None:
    seg = segment(text, node.test)
    if seg is None or not node.orelse or not _single_line(node.test):
        return None
    if _is_elif(text, node) or node.body[0].lineno == node.lineno:
        return None
    if not (own_line(text, node.body[0]) and own_line(text, node.orelse[0])):
        return None
    if node.body[0].col_offset != node.orelse[0].col_offset:
        return None
    lines = text.encode().split(b"\n")
    (b_span, b_text), (o_span, o_text) = (
        _block(lines, node.body),
        _block(lines, node.orelse),
    )
    return _edits(
        [
            (*span(node.test), f"not ({seg})"),
            (*b_span[:4], o_text),
            (*o_span[:4], b_text),
        ]
    )


Option = tuple[str, str, Callable[[str], str | None] | None]


def _options(text: str, node: ast.AST) -> list[Option]:
    if isinstance(node, ast.If):
        return [
            ("m_negate_if", "fault", _negate(text, node)),
            ("m_swap_branches", "decoy", _swap(text, node)),
        ]
    if isinstance(node, ast.Compare):
        return [
            ("m_flip_compare", "fault", _flip(text, node)),
            ("m_mirror_compare", "decoy", _mirror(text, node)),
        ]
    return []


def mechanisms(repo: Repo, path: str) -> list[Candidate]:
    found: list[Candidate] = []
    comp = repo.files[path]
    for qualname, fn, text in _function_nodes(repo, path):
        card = {"template": "function_changed", "component": comp, "function": qualname}
        found += [
            Candidate(
                "L4", op, intent, path, comp, (path, qualname), mutate, card, qualname
            )
            for node in own_nodes(fn)
            for op, intent, mutate in _options(text, node)
            if mutate is not None
        ]
    return found


# L5 ------------------------------------------------------------------------------


def _slots(node: ast.AST) -> list[ast.expr | None]:
    """Where an int literal is an index, a slice bound or a ``range`` argument."""
    if isinstance(node, ast.Subscript):
        return [node.slice]
    if isinstance(node, ast.Slice):
        return [node.lower, node.upper, node.step]
    is_range = (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "range"
    )
    return list(node.args) if is_range and isinstance(node, ast.Call) else []


def _positive_int(slot: ast.expr | None) -> ast.Constant | None:
    if isinstance(slot, ast.UnaryOp) and isinstance(slot.op, ast.USub):
        slot = slot.operand
    if not isinstance(slot, ast.Constant):
        return None
    value = slot.value
    return slot if type(value) is int and value >= 1 else None


def _int_sites(fn: FunctionNode) -> Iterator[ast.Constant]:
    for node in own_nodes(fn):
        for slot in _slots(node):
            found = _positive_int(slot)
            if found is not None:
                yield found


def _shift(text: str, node: ast.Constant) -> Callable[[str], str | None] | None:
    value = node.value
    if type(value) is not int or segment(text, node) != str(value):
        return None
    return _edits([(*span(node), str(value + 1))])


def _blocked(fn: FunctionNode) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.JoinedStr | ast.Match | ast.Global | ast.Nonlocal):
            return True
        if isinstance(node, ast.Name) and node.id in _RENAME_BLOCKERS:
            return True
    return False


def _nested_names(fn: FunctionNode) -> set[str]:
    """Names bound or read anywhere other than ``fn``'s own body, plus names that
    ``fn`` binds in a way a Name rename would miss."""
    own = {id(n) for n in own_nodes(fn)}
    names = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and id(node) not in own:
            names.add(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        if isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
    return names


def _rename(fn: FunctionNode, used: set[str]) -> list[tuple[str, Splice]]:
    """Per renameable local, its splices; returned as (name, splice) pairs."""
    if _blocked(fn):
        return []
    stored = {
        n.id
        for n in own_nodes(fn)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }
    names = sorted(stored - _nested_names(fn))
    out: list[tuple[str, Splice]] = []
    for name in names:
        if f"{name}_" in used:
            continue
        out += [
            (name, (*span(n), f"{name}_"))
            for n in own_nodes(fn)
            if isinstance(n, ast.Name) and n.id == name
        ]
    return out


def forms(repo: Repo, path: str) -> list[Candidate]:
    found: list[Candidate] = []
    comp = repo.files[path]
    text = repo.text(path)
    tree = parse(text)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} if tree else set()
    for qualname, fn, _ in _function_nodes(repo, path):
        card = {"template": "line_changed", "component": comp, "function": qualname}
        for node in _int_sites(fn):
            mutate = _shift(text, node)
            if mutate is not None:
                found.append(
                    Candidate(
                        "L5",
                        "l_shift_int",
                        "fault",
                        path,
                        comp,
                        (path, qualname),
                        mutate,
                        card,
                        qualname,
                    )
                )
        groups: dict[str, list[Splice]] = {}
        for name, edit in _rename(fn, used):
            groups.setdefault(name, []).append(edit)
        for edits in groups.values():
            found.append(
                Candidate(
                    "L5",
                    "l_rename_local",
                    "decoy",
                    path,
                    comp,
                    (path, qualname),
                    _edits(edits),
                    card,
                    qualname,
                )
            )
    return found
