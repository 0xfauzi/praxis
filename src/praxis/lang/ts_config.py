"""Build files for the TypeScript reader: every listed ``tsconfig.json`` (JSONC, with
its ``extends`` chain and ``references``) and every listed ``package.json`` with its
name, ``exports``, ``imports``, entry fields and module type. Read once per scan, from
the working tree, refusing a path that leaves the repository, a link out of it and
anything that is not a regular file. Paths are repository-relative POSIX strings, ``""``
for the root; a path that leaves the repository starts with ``../`` and matches no
listed file.

A missing or unreadable base config is an issue on the roots, never a failure: the
config is read without it, as tsc reads past an ``extends`` it cannot find with an
error.

Design: docs/design/04-languages.md sections 2.4 and 2.5; the reading rules follow
systemap's typescript_config.py and typescript_projects.py, re-implemented here.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG = "tsconfig.json"
PACKAGE = "package.json"
CONFIG_DIR = "${configDir}"
_JSONC = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
_TRAILING = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')
_GLOB = re.compile(r"[*?]")
# A base config chain longer than this is a loop tsc also refuses.
_MAX_EXTENDS = 16
# The module kinds whose default resolution is classic (measured with tsc 6.0.2;
# every other kind, and no kind, defaults to bundler).
_CLASSIC_MODULES = frozenset({"amd", "umd", "system"})
_RESOLUTION = {
    "node": "node10",
    "node10": "node10",
    "node16": "node16",
    "nodenext": "node16",
    "bundler": "bundler",
    "classic": "classic",
}


def norm(path: str) -> str:
    out = posixpath.normpath(path) if path else ""
    return "" if out == "." else out


def join(base: str, rel: str) -> str:
    return norm(posixpath.join(base, rel) if base else rel)


def dirname(path: str) -> str:
    return posixpath.dirname(path)


def inside(path: str) -> bool:
    return path != ".." and not path.startswith(("../", "/"))


def strip_jsonc(text: str) -> str:
    def blank(m: re.Match[str]) -> str:
        token = m.group(0)
        return token if token.startswith('"') else re.sub(r"[^\r\n]", " ", token)

    def comma(m: re.Match[str]) -> str:
        return m.group(0) if m.group(0).startswith('"') else ""

    return _TRAILING.sub(comma, _JSONC.sub(blank, text.lstrip("\ufeff")))


def read_json(repo_root: Path, rel: str) -> dict[str, Any] | None:
    """A JSONC object from a regular file inside the repository, or None."""
    full = repo_root / rel
    try:
        real = os.path.realpath(full)
        if not (real + os.sep).startswith(os.path.realpath(repo_root) + os.sep):
            return None
        if not stat.S_ISREG(Path(real).stat().st_mode):
            return None
        value = json.loads(strip_jsonc(full.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def vendored(path: str) -> bool:
    return "node_modules" in path.split("/")


@dataclass(frozen=True)
class Package:
    directory: str
    name: str | None
    exports: object
    imports: object
    entries: tuple[str, ...]
    esm: bool


def read_packages(repo_root: Path, listed: list[str]) -> dict[str, Package]:
    """Every listed package.json outside node_modules, by directory."""
    found: dict[str, Package] = {}
    for rel in listed:
        if posixpath.basename(rel) != PACKAGE or vendored(rel):
            continue
        data = read_json(repo_root, rel)
        if data is None:
            continue
        name = data.get("name")
        entries = tuple(
            v for k in ("types", "typings", "main") if isinstance(v := data.get(k), str)
        )
        found[dirname(rel)] = Package(
            dirname(rel),
            name if isinstance(name, str) and name else None,
            data.get("exports"),
            data.get("imports"),
            entries,
            data.get("type") == "module",
        )
    return found


def names(packages: dict[str, Package]) -> dict[str, str]:
    """Package name -> directory; a name two directories give is dropped."""
    seen: dict[str, list[str]] = {}
    for package in packages.values():
        if package.name is not None:
            seen.setdefault(package.name, []).append(package.directory)
    return {name: dirs[0] for name, dirs in seen.items() if len(dirs) == 1}


@dataclass(frozen=True)
class Config:
    """One project's options, with every path repository-relative."""

    path: str | None
    paths: tuple[tuple[str, tuple[str, ...]], ...] = ()
    base_url: str | None = None
    root_dir: str | None = None
    root_dirs: tuple[str, ...] = ()
    out_dir: str | None = None
    conditions: tuple[str, ...] = ()
    resolution: str = "node16"
    allow_js: bool = True
    files: frozenset[str] | None = None
    include: tuple[re.Pattern[str], ...] | None = None
    exclude: tuple[re.Pattern[str], ...] = ()
    references: tuple[str, ...] = ()

    def selects(self, path: str) -> bool:
        """Whether the config's own inputs (files, include, exclude) hold ``path``."""
        if self.files is not None and path in self.files:
            return True
        include = self.include
        if include is None:
            include = () if self.files is not None else (_EVERYTHING,)
        here = dirname(self.path) if self.path else ""
        rel = posixpath.relpath(path, here) if here else path
        hit = any(p.fullmatch(rel) for p in include)
        return hit and not any(p.fullmatch(rel) for p in self.exclude)


# The configuration of a file with no tsconfig.json above it: what a generated config
# gives (allowJs, nodenext), the same as E13's oracle (docs/design/04-languages.md 4.2).
DEFAULT = Config(None)


# Each option with the directory of the file that declared it.
_Options = dict[str, tuple[Any, str]]


@dataclass
class _Chain:
    repo_root: Path
    packages: dict[str, str]
    issues: list[str] = field(default_factory=list)

    def options(self, rel: str, depth: int = 0) -> tuple[_Options, _Options]:
        """(compilerOptions, input keys) after the extends chain, each value with the
        directory that declared it."""
        data = read_json(self.repo_root, rel) if depth < _MAX_EXTENDS else None
        if data is None:
            self.issues.append(rel)
            return {}, {}
        compiler: _Options = {}
        inputs: _Options = {}
        for base in _extends(data):
            target = self._base(dirname(rel), base)
            if target is None:
                self.issues.append(f"{rel}:{base}")
                continue
            more, more_inputs = self.options(target, depth + 1)
            compiler.update(more)
            inputs.update(more_inputs)
        own = data.get("compilerOptions")
        if isinstance(own, dict):
            compiler.update({k: (v, dirname(rel)) for k, v in own.items()})
        keys = ("files", "include", "exclude")
        inputs.update({k: (data[k], dirname(rel)) for k in keys if k in data})
        return compiler, inputs

    def _base(self, here: str, ref: str) -> str | None:
        """The file an ``extends`` names: relative, or a package found in a
        node_modules folder above, or a workspace package of that name."""
        if ref.startswith("."):
            candidates = [join(here, ref)]
        else:
            candidates = [join(d, "node_modules/" + ref) for d in _ancestors(here)]
            name = _package_name(ref)
            if name in self.packages:
                candidates.append(
                    join(self.packages[name], ref[len(name) :].lstrip("/"))
                )
        for base in candidates:
            for choice in (base, base + ".json", join(base, CONFIG)):
                if inside(choice) and (self.repo_root / choice).is_file():
                    return choice
        return None


def _ancestors(here: str) -> list[str]:
    out = [here]
    while here:
        here = dirname(here)
        out.append(here)
    return out


def _package_name(spec: str) -> str:
    parts = spec.split("/")
    return "/".join(parts[:2] if spec.startswith("@") else parts[:1])


def _extends(data: dict[str, Any]) -> list[str]:
    value = data.get("extends")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _expand(value: str, origin: str, config_dir: str) -> str:
    if value.startswith(CONFIG_DIR):
        return join(config_dir, value[len(CONFIG_DIR) :].lstrip("/"))
    return join(origin, value)


def _path_option(options: _Options, key: str, config_dir: str) -> str | None:
    value = options.get(key)
    if value is None or not isinstance(value[0], str):
        return None
    return _expand(value[0], value[1], config_dir)


def _resolution(options: _Options) -> str:
    raw = options.get("moduleResolution", (None, ""))[0]
    if isinstance(raw, str) and raw.lower() in _RESOLUTION:
        return _RESOLUTION[raw.lower()]
    module = options.get("module", (None, ""))[0]
    module = module.lower() if isinstance(module, str) else ""
    if module in _CLASSIC_MODULES:
        return "classic"
    return (
        "node16" if module in ("node16", "node18", "node20", "nodenext") else "bundler"
    )


def _aliases(
    options: _Options, base_url: str | None, config_dir: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    raw, origin = options.get("paths", ({}, config_dir))
    if not isinstance(raw, dict):
        return ()
    base = origin if base_url is None else base_url
    return tuple(
        (
            pattern,
            tuple(_expand(t, base, config_dir) for t in targets if isinstance(t, str)),
        )
        for pattern, targets in raw.items()
        if isinstance(pattern, str) and isinstance(targets, list)
    )


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _glob(pattern: str) -> re.Pattern[str]:
    """A tsconfig include or exclude pattern, relative to its config's directory, as a
    regular expression over paths relative to that directory. A wildcard never matches
    a `..` segment; a pattern with no wildcard names a file or a directory."""
    body = pattern.strip("/").removeprefix("./")
    regex = (
        re.escape(body)
        .replace(r"\*\*/", r"(?:(?!\.\.(?:/|$))[^/]+/)*")
        .replace(r"\*\*", r"(?!\.\.(?:/|$)).*")
        .replace(r"\*", "[^/]*")
        .replace(r"\?", "[^/]")
    )
    if not _GLOB.search(body):
        regex += "(?:/.*)?"
    return re.compile(regex)


_EVERYTHING = _glob("**/*")


def _patterns(
    inputs: _Options, key: str, config_dir: str
) -> tuple[re.Pattern[str], ...] | None:
    value = inputs.get(key)
    if value is None:
        return None
    raw, origin = value
    out = []
    for entry in _strings(raw):
        full = _expand(entry, origin, config_dir)
        rel = posixpath.relpath(full or ".", config_dir or ".")
        out.append(_glob(rel))
    return tuple(out)


def _files(inputs: _Options, config_dir: str) -> frozenset[str] | None:
    value = inputs.get("files")
    if value is None:
        return None
    return frozenset(_expand(v, value[1], config_dir) for v in _strings(value[0]))


def _references(repo_root: Path, rel: str, listed: frozenset[str]) -> tuple[str, ...]:
    data = read_json(repo_root, rel) or {}
    out = []
    for ref in data.get("references") or []:
        raw = ref.get("path") if isinstance(ref, dict) else None
        if isinstance(raw, str):
            target = join(dirname(rel), raw)
            target = target if target.endswith(".json") else join(target, CONFIG)
            if target in listed:
                out.append(target)
    return tuple(out)


def read_config(
    repo_root: Path,
    rel: str,
    listed: frozenset[str],
    chain: _Chain,
) -> Config:
    config_dir = dirname(rel)
    options, inputs = chain.options(rel)
    base_url = _path_option(options, "baseUrl", config_dir)
    exclude = _patterns(inputs, "exclude", config_dir) or ()
    out_dir = _path_option(options, "outDir", config_dir)
    if out_dir is not None:
        exclude += (_glob(posixpath.relpath(out_dir or ".", config_dir or ".")),)
    allow = options.get("allowJs", (True, ""))[0]
    root_dirs = options.get("rootDirs", ([], ""))
    return Config(
        path=rel,
        paths=_aliases(options, base_url, config_dir),
        base_url=base_url,
        root_dir=_path_option(options, "rootDir", config_dir),
        root_dirs=tuple(
            _expand(v, root_dirs[1], config_dir) for v in _strings(root_dirs[0])
        ),
        out_dir=out_dir,
        conditions=tuple(_strings(options.get("customConditions", ([], ""))[0])),
        resolution=_resolution(options),
        allow_js=allow is not False,
        files=_files(inputs, config_dir),
        include=_patterns(inputs, "include", config_dir),
        exclude=exclude,
        references=_references(repo_root, rel, listed),
    )


def read_configs(
    repo_root: Path,
    listed: frozenset[str],
    packages: dict[str, str],
) -> tuple[dict[str, Config], tuple[str, ...]]:
    """Every listed tsconfig.json outside node_modules and every config a reference
    names, transitively; and the issues met while reading them."""
    chain = _Chain(repo_root, packages)
    queue = sorted(
        p for p in listed if posixpath.basename(p) == CONFIG and not vendored(p)
    )
    configs: dict[str, Config] = {}
    while queue:
        rel = queue.pop(0)
        if rel in configs:
            continue
        configs[rel] = read_config(repo_root, rel, listed, chain)
        queue.extend(r for r in configs[rel].references if r not in configs)
    return configs, tuple(dict.fromkeys(chain.issues))
