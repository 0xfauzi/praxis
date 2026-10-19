"""Component maps: named sets of repository paths, the nodes every structural item uses.

Design: docs/design/03-measurement.md section 2. A map defines nodes only; every edge
comes from an extractor. Globs are fnmatch patterns over the POSIX repo-relative path,
so `*` crosses `/`. A path no rule matches is `unmapped`, never folded into a neighbour.

The mechanical map cuts the product tree at depth d: a path deeper than d belongs to its
directory at depth d, every other file is its own component, and a package's non-empty
`__init__.py` is `<dir>/.`. An empty `__init__.py` holds no code and stays unmapped, so
it cannot pad the count. The cut is monotone in d, which makes "the smallest d with at
least 5" well defined; it also means a reduction for exceeding 30 always lands below 5.
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MIN_COMPONENTS = 5
MAX_COMPONENTS = 30
UNMAPPED = "unmapped"
INIT = "__init__.py"


@dataclass(frozen=True)
class Rule:
    id: str
    globs: tuple[str, ...]


@dataclass(frozen=True)
class ComponentMap:
    repo_id: str
    source: str
    rules: tuple[Rule, ...]
    version: str
    warnings: tuple[str, ...]


class TooFewComponents(Exception):
    pass


FIXED = (
    Rule("tests", ("tests/*", "test/*", "*/tests/*", "*/test/*", "test_*.py",
                   "*/test_*.py", "*_test.py")),
    Rule("docs", ("docs/*", "*.md", "*.rst")),
    Rule("config", (".*", "*/.*", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini",
                    "*.lock", "*-lock.json", "*-lock.yaml")),
)  # fmt: skip
RESERVED = frozenset({*(rule.id for rule in FIXED), UNMAPPED})
SOURCES = ("mechanical", "owner")


def map_version(rules: tuple[Rule, ...]) -> str:
    rows = [[rule.id, *rule.globs] for rule in rules]
    canon = json.dumps(rows, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _build(
    repo_id: str, source: str, product: list[Rule], warnings: tuple[str, ...] = ()
) -> ComponentMap:
    rules = FIXED + tuple(product)
    return ComponentMap(repo_id, source, rules, map_version(rules), warnings)


def _match(rules: tuple[Rule, ...], path: str) -> str | None:
    for rule in rules:
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in rule.globs):
            return rule.id
    return None


def assign(cmap: ComponentMap, path: str) -> str:
    found = _match(cmap.rules, path)
    return UNMAPPED if found is None else found


def product_components(cmap: ComponentMap) -> list[str]:
    return [rule.id for rule in cmap.rules if rule.id not in RESERVED]


def _product_root(product: list[str]) -> tuple[str, ...]:
    inits = [PurePosixPath(p).parts for p in product if PurePosixPath(p).name == INIT]
    src = sorted({parts[1] for parts in inits if len(parts) == 3 and parts[0] == "src"})
    top = sorted({parts[0] for parts in inits if len(parts) == 2})
    if src:
        return ("src", src[0]) if len(src) == 1 else ("src",)
    return (top[0],) if len(top) == 1 else ()


def _place(
    parts: tuple[str, ...], root: tuple[str, ...], d: int
) -> tuple[str, str, str]:
    """(short id, full id, glob) of one file under the cut at depth d."""
    rel = parts[len(root) :]
    if len(rel) > d:
        cid = "/".join(rel[:d]) or "."
        prefix = "/".join(parts[: len(root) + d])
        return cid, cid, (glob.escape(prefix) + "/*") if prefix else "*"
    exact = glob.escape("/".join(parts))
    if rel[-1] == INIT:
        cid = "/".join((*rel[:-1], "."))
        return cid, cid, exact
    full = "/".join(rel)
    return full.removesuffix(".py"), full, exact


def _cut(files: list[tuple[str, ...]], root: tuple[str, ...], d: int) -> list[Rule]:
    placed = (_place(parts, root, d) for parts in files)
    by_glob = {g: (short, full) for short, full, g in placed}
    taken = Counter(short for short, _ in by_glob.values())
    named = [
        Rule(full if taken[short] > 1 or short in RESERVED else short, (g,))
        for g, (short, full) in by_glob.items()
    ]
    return sorted(named, key=lambda rule: rule.id)


def _ls_files(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [path for path in out.split("\0") if path]


def _empty(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def derive(
    repo_root: Path, repo_id: str, tracked: list[str] | None = None
) -> ComponentMap:
    paths = _ls_files(repo_root) if tracked is None else tracked
    product = [p for p in paths if _match(FIXED, p) is None]
    root = _product_root(product)
    files = [
        PurePosixPath(p).parts
        for p in product
        if PurePosixPath(p).parts[: len(root)] == root
        and len(PurePosixPath(p).parts) > len(root)
        and not (PurePosixPath(p).name == INIT and _empty(repo_root / p))
    ]
    deepest = max((len(parts) - len(root) for parts in files), default=0)
    for d in range(1, deepest + 1):
        rules = _cut(files, root, d)
        if len(rules) > MAX_COMPONENTS:
            warning = f"depth_reduced_from_{d}_to_{d - 1}"
            return _build(repo_id, "mechanical", _cut(files, root, d - 1), (warning,))
        if len(rules) >= MIN_COMPONENTS:
            return _build(repo_id, "mechanical", rules)
    raise TooFewComponents(f"fewer than {MIN_COMPONENTS} components at every depth")


def _is_glob(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _owner_rule(table: Any, where: Path) -> Rule:
    if not isinstance(table, dict) or set(table) != {"id", "globs"}:
        raise ValueError(f"{where}: each [[component]] has exactly `id` and `globs`")
    cid, globs = table["id"], table["globs"]
    if not isinstance(cid, str) or not cid or cid in RESERVED:
        raise ValueError(
            f"{where}: component id must be a name not in {sorted(RESERVED)}"
        )
    if not isinstance(globs, list) or not globs or not all(map(_is_glob, globs)):
        raise ValueError(f"{where}: component {cid!r} needs a non-empty list of globs")
    return Rule(cid, tuple(globs))


def load_owner(praxis_home: Path, repo_id: str) -> ComponentMap | None:
    where = praxis_home / "components" / f"{repo_id}.toml"
    if not where.is_file():
        return None
    with where.open("rb") as handle:
        doc = tomllib.load(handle)
    tables = doc.get("component") if set(doc) == {"component"} else None
    if not isinstance(tables, list) or not tables:
        raise ValueError(f"{where}: expected [[component]] tables and nothing else")
    rules = [_owner_rule(table, where) for table in tables]
    repeated = sorted(cid for cid, n in Counter(r.id for r in rules).items() if n > 1)
    if repeated:
        raise ValueError(f"{where}: component ids repeated: {repeated}")
    return _build(repo_id, "owner", rules)


def to_json(cmap: ComponentMap) -> str:
    doc = {
        "repo_id": cmap.repo_id,
        "source": cmap.source,
        "rules": [[rule.id, *rule.globs] for rule in cmap.rules],
        "version": cmap.version,
        "warnings": list(cmap.warnings),
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def from_json(text: str) -> ComponentMap:
    doc = json.loads(text)
    if set(doc) != {"repo_id", "source", "rules", "version", "warnings"}:
        raise ValueError("component map JSON has unexpected keys")
    rules = tuple(Rule(row[0], tuple(row[1:])) for row in doc["rules"])
    cmap = ComponentMap(
        doc["repo_id"], doc["source"], rules, doc["version"], tuple(doc["warnings"])
    )
    if cmap.source not in SOURCES or cmap.version != map_version(rules):
        raise ValueError("component map source or version does not match its rules")
    return cmap
