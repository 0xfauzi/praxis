"""Component maps: named sets of repository paths, the nodes every structural item uses.

Design: docs/design/03-measurement.md section 2. A map defines nodes only; every edge
comes from an extractor. Globs are fnmatch patterns over the POSIX repo-relative path,
so `*` crosses `/`. A path no rule matches is `unmapped`, never folded into a neighbour.

The mechanical map cuts the product tree at depth d: a path deeper than d belongs to its
directory at depth d, every other file is its own component, and a package's non-empty
`__init__.py` is `<dir>/.`. An empty `__init__.py` holds no code and stays unmapped, so
it cannot pad the count. The cut is monotone in d, which makes "the smallest d with at
least 5" well defined; it also means a reduction for exceeding 30 always lands below 5.

A flat package (more than 30 children at depth 1, so the cut would fall to depth 0) is
grouped instead: children by the name token before the first underscore, then each
one-file group into the multi-file group it shares the most import edges with (design
2.2, step 5). The grouped map may still exceed 30; freeze refuses it and prints the
`to_toml` draft, the owner's toml format, for the owner to merge a few groups.
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
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

from praxis.extract import static_imports

MIN_COMPONENTS = 5
MAX_COMPONENTS = 30
UNMAPPED = "unmapped"
INIT = "__init__.py"


Parts = tuple[str, ...]


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
GROUPED = "grouped_by_prefix_and_imports"
ABOVE = f"above_{MAX_COMPONENTS}_after_grouping"


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
    # An unmerged path is listed once per stage; a path is an identifier here.
    return list(dict.fromkeys(path for path in out.split("\0") if path))


def _empty(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def _files(repo_root: Path, paths: list[str], root: tuple[str, ...]) -> list[Parts]:
    """The product files under the product root, as path parts."""
    return [
        PurePosixPath(p).parts
        for p in paths
        if _match(FIXED, p) is None
        and PurePosixPath(p).parts[: len(root)] == root
        and len(PurePosixPath(p).parts) > len(root)
        and not (PurePosixPath(p).name == INIT and _empty(repo_root / p))
    ]


def derive(
    repo_root: Path, repo_id: str, tracked: list[str] | None = None
) -> ComponentMap:
    paths = _ls_files(repo_root) if tracked is None else tracked
    root = _product_root([p for p in paths if _match(FIXED, p) is None])
    files = _files(repo_root, paths, root)
    deepest = max((len(parts) - len(root) for parts in files), default=0)
    for d in range(1, deepest + 1):
        rules = _cut(files, root, d)
        if len(rules) > MAX_COMPONENTS and d == 1:
            return _grouped(repo_root, repo_id, rules, files)
        if len(rules) > MAX_COMPONENTS:
            warning = f"depth_reduced_from_{d}_to_{d - 1}"
            return _build(repo_id, "mechanical", _cut(files, root, d - 1), (warning,))
        if len(rules) >= MIN_COMPONENTS:
            return _build(repo_id, "mechanical", rules)
    raise TooFewComponents(f"fewer than {MIN_COMPONENTS} components at every depth")


def _sizes(rules: list[Rule], files: list[Parts]) -> Counter[str]:
    """Files per depth-1 child; each child of the cut holds at least one."""
    found = (_match(tuple(rules), "/".join(parts)) for parts in files)
    return Counter(cid for cid in found if cid is not None)


def _token(cid: str) -> str:
    """The name before the first underscore; a name that starts with one is its own."""
    name = cid.removesuffix(".py")
    return name.split("_", 1)[0] or name


def _by_prefix(rules: list[Rule]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for rule in rules:
        groups.setdefault(_token(rule.id), []).append(rule.id)
    return groups


def _attach(
    groups: dict[str, list[str]], sizes: Counter[str], edges: list[tuple[str, str]]
) -> None:
    """Each one-file group joins the multi-file group it shares the most import edges
    with (in plus out), when that maximum is above 0 and unique; a tie is left alone.
    A round decides every move on the groups it started with, so the result does not
    depend on the order of the groups, and repeats until a round moves nothing. Every
    round but the last empties a one-file group, so the loop ends."""
    near: dict[str, list[str]] = {}
    for a, b in edges:
        near.setdefault(a, []).append(b)
        near.setdefault(b, []).append(a)
    while moves := _moves(groups, sizes, near):
        for token, into in moves.items():
            groups[into] += groups.pop(token)


def _moves(
    groups: dict[str, list[str]], sizes: Counter[str], near: dict[str, list[str]]
) -> dict[str, str]:
    """One round: one-file group -> the multi-file group it joins."""
    home = {cid: token for token, ids in groups.items() for cid in ids}
    multi = {t for t, ids in groups.items() if sum(sizes[c] for c in ids) > 1}
    moves = {}
    for token, ids in groups.items():
        into = None if token in multi else _best(near.get(ids[0], []), home, multi)
        if into is not None:
            moves[token] = into
    return moves


def _best(near: list[str], home: dict[str, str], multi: set[str]) -> str | None:
    """The multi-file group with the most edges to one child, None on a tie or none."""
    best = Counter(home[c] for c in near if home[c] in multi).most_common(2)
    tied = len(best) == 2 and best[0][1] == best[1][1]
    return None if not best or tied else best[0][0]


def _grouped(
    repo_root: Path, repo_id: str, rules: list[Rule], files: list[Parts]
) -> ComponentMap:
    """Design 2.2 step 5 over the depth-1 children: a group of several children is
    named by its token, a reserved token gets a `*`, and a child left alone keeps its
    own id. Import edges are X1's, over the product root's Python files."""
    child = partial(_match, tuple(rules))
    python = list(dict.fromkeys("/".join(p) for p in files if p[-1].endswith(".py")))
    graph = static_imports(repo_root, lambda p: child(p) or UNMAPPED, python)
    edges = [(e.src, e.dst) for e in graph.edges if UNMAPPED not in (e.src, e.dst)]
    groups = _by_prefix(rules)
    _attach(groups, _sizes(rules, files), edges)
    globs = {rule.id: rule.globs for rule in rules}
    product = [
        Rule(
            ids[0] if len(ids) == 1 else token + "*" * (token in RESERVED),
            tuple(sorted(g for cid in ids for g in globs[cid])),
        )
        for token, ids in groups.items()
    ]
    warnings = (GROUPED,) + (ABOVE,) * (len(product) > MAX_COMPONENTS)
    product.sort(key=lambda rule: rule.id)
    return _build(repo_id, "mechanical", product, warnings)


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


def owner_path(praxis_home: Path, repo_id: str) -> Path:
    return praxis_home / "components" / f"{repo_id}.toml"


def load_owner(praxis_home: Path, repo_id: str) -> ComponentMap | None:
    where = owner_path(praxis_home, repo_id)
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


def _toml_string(text: str) -> str:
    # A JSON string is a TOML basic string once DEL, which TOML forbids raw, is escaped.
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def to_toml(cmap: ComponentMap) -> str:
    """The product rules in the owner's toml format, which load_owner reads back as the
    same rules, after comment lines naming the map they were printed from."""
    lines = [f"# praxis component map {cmap.version} ({cmap.source}), {cmap.repo_id}"]
    lines += [f"# warnings: {' '.join(cmap.warnings)}"] * bool(cmap.warnings)
    for rule in cmap.rules:
        if rule.id not in RESERVED:
            globs = ", ".join(map(_toml_string, rule.globs))
            lines += ["", "[[component]]", f"id = {_toml_string(rule.id)}"]
            lines.append(f"globs = [{globs}]")
    return "\n".join(lines) + "\n"


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
