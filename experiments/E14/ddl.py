"""X4-schema for E14: the CHECK clauses a module's DDL declares, read at runtime.

DDL is often built by f-strings and helpers (praxis's ``schema.SCHEMA`` is one f-string
with helper calls), so reading string literals from the AST misses most of it. The probe
below imports the module in a fresh interpreter in the scratch clone, executes every
module-level string that holds ``CREATE TABLE`` in ``sqlite3.connect(":memory:")`` with
ATTACH refused, and prints counts only: CHECK clauses per table, scripts that failed,
and for each CHECK clause the table, the character offset and the length of the text
that removing it would cut from the source file, or -1 when that text is not exactly
once in the source (a clause a helper generates) or not exactly once in the module's
DDL (a fragment several tables share, so one edit would drop several clauses). No DDL
text crosses the pipe.
Standard library only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from edit import Candidate

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TIMEOUT_S = 60.0
_DDL = re.compile(r"CREATE\s+TABLE", re.IGNORECASE)

PROBE = r"""
import importlib, json, re, sqlite3, sys
CHECK = re.compile(r"\bCHECK\s*\(", re.I)
TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)", re.I)
NAMED = re.compile(r"CONSTRAINT\s+\w+\s*$", re.I)
DDL = re.compile(r"CREATE\s+TABLE", re.I)


def close(value, start):
    depth, quoted = 0, False
    for i in range(start, len(value)):
        ch = value[i]
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def cut(value, match):
    end = close(value, match.end() - 1)
    if end is None:
        return None
    lo = match.start()
    named = NAMED.search(value[:lo])
    lo = named.start() if named else lo
    while lo > 0 and value[lo - 1].isspace():
        lo -= 1
    if lo > 0 and value[lo - 1] == ",":
        lo -= 1
    return lo, end + 1


def main():
    module = importlib.import_module(sys.argv[1])
    with open(sys.argv[2], encoding="utf-8", newline="") as handle:
        source = handle.read()
    found = vars(module)
    names = sorted(k for k, v in found.items() if isinstance(v, str) and DDL.search(v))
    db = sqlite3.connect(":memory:")
    db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    failed = 0
    for name in names:
        try:
            db.executescript(found[name])
        except sqlite3.Error:
            failed += 1
    rows = db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite~_%' ESCAPE '~'"
    ).fetchall()
    counts = {n: len(CHECK.findall(sql or "")) for n, sql in rows}
    sites = []
    every = "\n".join(found[name] for name in names)
    for name in names:
        value = found[name]
        for match in CHECK.finditer(value):
            span = cut(value, match)
            tables = TABLE.findall(value[: match.start()])
            if span is None or not tables:
                continue
            removed = value[span[0] : span[1]]
            once = source.count(removed) == 1 and every.count(removed) == 1
            pos = source.find(removed) if once else -1
            sites.append([tables[-1], pos, len(removed)])
    print(json.dumps({"counts": counts, "failed": failed, "sites": sites}))


main()
"""


@dataclass(frozen=True)
class Schema:
    counts: dict[str, int]
    failed: int
    sites: list[tuple[str, int, int]]


def has_ddl(text: str) -> bool:
    return bool(_DDL.search(text))


def probe(clone: Path, env: dict[str, str], module: str, path: str) -> Schema | None:
    """The module's schema as the probe sees it, or None when it does not import."""
    try:
        done = subprocess.run(
            [sys.executable, "-c", PROBE, module, str(clone / path)],
            cwd=clone,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None
    if done.returncode != 0:
        return None
    data = json.loads(done.stdout.strip().splitlines()[-1])
    sites = [(str(t), int(p), int(n)) for t, p, n in data["sites"]]
    return Schema(dict(data["counts"]), int(data["failed"]), sites)


def _cut(pos: int, length: int) -> Callable[[str], str | None]:
    return lambda text: text[:pos] + text[pos + length :]


def drop_checks(path: str, comp: str, schema: Schema) -> list[Candidate]:
    found: list[Candidate] = []
    for i, (table, pos, length) in enumerate(schema.sites):
        if pos < 0 or schema.counts.get(table, 0) < 1:
            continue
        card = {"template": "constraint_dropped", "table": table, "component": comp}
        found.append(
            Candidate(
                "L1c",
                "c_drop_check",
                "fault",
                path,
                comp,
                (table, str(i)),
                _cut(pos, length),
                card,
                extra={"table": table},
            )
        )
    return found
