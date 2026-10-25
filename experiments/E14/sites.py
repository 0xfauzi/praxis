"""X4, the enforcement-site extractor (design 05, section 2.1), and the L1 operators.

Two machine-readable kinds of constraint are read. A scoped pygrep hook in
``.pre-commit-config.yaml`` (``language: pygrep`` with a ``files`` pattern) says that
lines matching its ``entry`` may appear only outside its scope: its site is the product
file outside the scope that holds matches. A CHECK clause in DDL says what a table
accepts (read by ``ddl.py``). Everything else (prose invariants, hooks in another
language, double-quoted or aliased YAML values) is counted and never guessed at.
Matching follows pre-commit's pygrep: a bytes pattern searched line by line,
``re.search`` on the path.
Standard library only; only identifiers and counts leave this module.
"""

from __future__ import annotations

import ast
import re
import zlib
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from edit import Candidate, appended_function, parse, statement_source

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

L1 = "L1"
RELOCATED = "_e14_relocated"
_ID = re.compile(r"^(\s*)- id:\s*(\S+)")
_KEY = re.compile(r"^(\s*)(\w+):\s*(.*)$")


@dataclass(frozen=True)
class Hook:
    id: str
    pattern: re.Pattern[bytes]
    files: str
    exclude: str

    def covers(self, path: str) -> bool:
        return bool(re.search(self.files, path)) and not re.search(self.exclude, path)

    def count(self, text: str) -> int:
        return sum(
            1 for line in text.encode().split(b"\n") if self.pattern.search(line)
        )


def _scalar(raw: str) -> str | None:
    """A YAML plain or single-quoted scalar; anything else is None (counted, unread)."""
    raw = raw.strip()
    if raw.startswith("'"):
        out, i = [], 1
        while i < len(raw):
            if raw[i] == "'" and raw[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
            elif raw[i] == "'":
                return "".join(out)
            else:
                out.append(raw[i])
                i += 1
        return None
    if not raw or raw[0] in '"*&|>[{':
        return None
    return raw.split(" #")[0].strip() or None


def _blocks(text: str) -> list[dict[str, str | None]]:
    blocks: list[dict[str, str | None]] = []
    indent = -1
    for line in text.splitlines():
        head = _ID.match(line)
        if head:
            indent = len(head.group(1))
            blocks.append({"id": head.group(2)})
            continue
        key = _KEY.match(line)
        if blocks and key and len(key.group(1)) > indent:
            blocks[-1].setdefault(key.group(2), _scalar(key.group(3)))
    return blocks


def read_hooks(root: Path) -> tuple[list[Hook], Counter[str]]:
    """Scoped pygrep hooks, and counts of every hook kind seen."""
    tally: Counter[str] = Counter()
    where = root / ".pre-commit-config.yaml"
    if not where.is_file():
        tally["no_config"] += 1
        return [], tally
    found: list[Hook] = []
    for block in _blocks(where.read_text()):
        tally["hooks"] += 1
        if block.get("language") != "pygrep":
            continue
        tally["pygrep"] += 1
        entry, files = block.get("entry"), block.get("files")
        if entry is None or "args" in block:
            tally["pygrep_unread"] += 1
        elif "files" not in block:
            tally["pygrep_global"] += 1
        elif files is None:
            tally["pygrep_unread"] += 1
        else:
            exclude = block.get("exclude", "^$")
            if exclude is None:
                tally["pygrep_unread"] += 1
                continue
            found.append(
                Hook(str(block["id"]), re.compile(entry.encode()), files, exclude)
            )
    tally["pygrep_scoped"] = len(found)
    return found, tally


def hook_counts(hooks: list[Hook], path: str, text: str) -> dict[str, int]:
    return {h.id: h.count(text) for h in hooks if h.covers(path)}


def _smallest_statement(tree: ast.Module, line: int) -> ast.stmt | None:
    best: ast.stmt | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or hasattr(node, "body"):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end and (
            best is None or end - node.lineno < (best.end_lineno or 0) - best.lineno
        ):
            best = node
    return best


def _resource(match: re.Match[bytes]) -> tuple[str, str]:
    """(operation, resource) named by the hook's match: identifiers, never a line."""
    text = match.group(0).decode(errors="replace").split()
    operation = text[0] if text else "match"
    if match.lastindex:
        return operation, match.group(match.lastindex).decode(errors="replace")
    return operation, " ".join(text[1:]) or operation


@dataclass(frozen=True)
class Repo:
    """The scratch clone as the operators see it: product files and their components."""

    root: Path
    files: dict[str, str]
    texts: dict[str, str]

    def text(self, path: str) -> str:
        if path not in self.texts:
            self.texts[path] = (self.root / path).read_bytes().decode("utf-8")
        return self.texts[path]


def _sites(repo: Repo, hook: Hook) -> list[str]:
    return [
        p for p in sorted(repo.files) if not hook.covers(p) and hook.count(repo.text(p))
    ]


def _targets(repo: Repo, hook: Hook, site_comp: str) -> dict[str, list[str]]:
    by_comp: dict[str, list[str]] = {}
    for path in sorted(repo.files):
        comp = repo.files[path]
        if comp != site_comp and hook.covers(path):
            by_comp.setdefault(comp, []).append(path)
    return by_comp


def _plant(body: str) -> Callable[[str], str | None]:
    return lambda text: appended_function(text, RELOCATED, body)


def relocations(repo: Repo, hook: Hook) -> list[Candidate]:
    found: list[Candidate] = []
    for site in _sites(repo, hook):
        text, comp = repo.text(site), repo.files[site]
        tree = parse(text)
        if tree is None:
            continue
        seen: set[int] = set()
        for lineno, line in enumerate(text.encode().split(b"\n"), 1):
            match = hook.pattern.search(line)
            stmt = _smallest_statement(tree, lineno) if match else None
            body = statement_source(text, stmt) if stmt else None
            if match is None or stmt is None or body is None or stmt.lineno in seen:
                continue
            seen.add(stmt.lineno)
            found += _pair(repo, hook, (site, comp), _resource(match), body)
    return found


def _pair(
    repo: Repo, hook: Hook, site: tuple[str, str], op_res: tuple[str, str], body: str
) -> list[Candidate]:
    (path, comp), (operation, resource) = site, op_res
    card = {"template": "now_runs", "operation": operation, "resource": resource}
    extra = {"hook": hook.id}
    out = [
        Candidate(
            L1,
            "c_relocate_in_site",
            "decoy",
            path,
            comp,
            (hook.id, comp, resource),
            _plant(body),
            {**card, "component": comp},
            extra=extra,
        )
    ]
    for target_comp, paths in _targets(repo, hook, comp).items():
        pick = paths[zlib.crc32(f"{hook.id}:{resource}".encode()) % len(paths)]
        out.append(
            Candidate(
                L1,
                "c_relocate",
                "fault",
                pick,
                target_comp,
                (hook.id, target_comp, resource),
                _plant(body),
                {**card, "component": target_comp},
                extra=extra,
            )
        )
    return out


def contract_counts(root: Path) -> Counter[str]:
    """Constraints a systemap contract declares: prose invariants name the components
    that govern them but give no violation test; import rules are machine-readable."""
    tally: Counter[str] = Counter()
    where = root / "map" / "contract.py"
    tree = parse(where.read_text()) if where.is_file() else None
    for node in ast.walk(tree) if tree else []:
        func = node.func if isinstance(node, ast.Call) else None
        if isinstance(func, ast.Name) and func.id in ("Invariant", "ImportRule"):
            tally[func.id] += 1
    return tally
