"""TypeScript and JavaScript resolution (x1-ts-v1): a specifier and its importer to the
listed file the TypeScript resolver would pick, by set lookups over roots built once per
scan. No I/O happens here.

Order, as tsc's: a relative or absolute specifier joins the importer's directory (then
``rootDirs``); a bare one tries the project's ``paths``, then ``baseUrl``, then a ``#``
subpath through the nearest package.json ``imports``, then a workspace package by name
through its ``exports`` (exact keys, then the pattern with the longest prefix, ``null``
blocking, conditions in key order) or its entry fields. A target in a package's outDir
maps back to its rootDir source. Extension substitution follows tsc's table: ``.js`` to
``.ts``, ``.tsx``, ``.d.ts``, then ``.js``, ``.jsx``; ``.mjs`` to ``.mts``; ``.cjs`` to
``.cts``; ``.jsx`` to ``.tsx`` first. Under node16 and nodenext an ES-module-mode
specifier gets no added extension and no directory index, as tsc refuses both.

Reason codes, resolved: relative, alias, base_url, workspace, workspace_outdir,
package_imports, reference. Unresolved and not blind: external, asset. Blind:
dynamic, relative_missing, alias_unconfigured, unresolved_local, nul, jsdoc.

Design: docs/design/04-languages.md sections 1.2, 1.4, 2.2 and 2.5.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from praxis.lang import ImportSite, Resolved
from praxis.lang.ts_config import (
    CONFIG,
    DEFAULT,
    Config,
    Package,
    dirname,
    inside,
    join,
    names,
    read_configs,
    read_packages,
)

if TYPE_CHECKING:
    from pathlib import Path

_SUBST = {
    ".js": (".ts", ".tsx", ".d.ts", ".js", ".jsx"),
    ".jsx": (".tsx", ".ts", ".d.ts", ".jsx", ".js"),
    ".mjs": (".mts", ".d.mts", ".mjs"),
    ".cjs": (".cts", ".d.cts", ".cjs"),
}
_EXACT = (".d.ts", ".d.mts", ".d.cts", ".ts", ".tsx", ".mts", ".cts")
_ADDED = (".ts", ".tsx", ".d.ts", ".js", ".jsx")
_SCRIPT = frozenset({".js", ".jsx", ".mjs", ".cjs"})
_DECLARATIONS = (".d.ts", ".d.mts", ".d.cts")
_TO_SOURCE = (
    (".d.ts", (".ts", ".tsx")),
    (".d.mts", (".mts",)),
    (".d.cts", (".cts",)),
    (".js", (".ts", ".tsx")),
    (".jsx", (".tsx", ".ts")),
    (".mjs", (".mts",)),
    (".cjs", (".cts",)),
)
# Imports of these suffixes name no code (design 04 section 1.2): an asset, not a miss.
_ASSETS = frozenset(
    [
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".styl",
        ".pcss",
        ".json",
        ".json5",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".ico",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".webm",
        ".ogg",
        ".wav",
        ".wasm",
        ".html",
        ".md",
        ".mdx",
        ".txt",
        ".yaml",
        ".yml",
        ".toml",
        ".graphql",
        ".gql",
        ".vue",
        ".svelte",
        ".astro",
        ".csv",
        ".xml",
    ]
)
# Node's built-in modules: a bare name that is one of these is never an unconfigured
# alias, whatever directory the repository has.
_BUILTINS = frozenset(
    [
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
        "test",
    ]
)


@dataclass
class Roots:
    files: frozenset[str]
    dirs: frozenset[str]
    configs: dict[str, Config]
    nearest: dict[str, str | None]
    packages: dict[str, Package]
    names: dict[str, str]
    scopes: dict[str, str | None]
    issues: tuple[str, ...]
    chosen: dict[str, Config] = field(default_factory=dict)

    def config(self, importer: str) -> Config:
        """The project of a file: the nearest tsconfig.json when its inputs hold the
        file, else the first config it references, transitively, whose inputs do
        (tsserver's rule); a file with no tsconfig.json above it gets the default."""
        found = self.chosen.get(importer)
        if found is None:
            found = self._choose(importer)
            self.chosen[importer] = found
        return found

    def _choose(self, importer: str) -> Config:
        near = self.nearest.get(dirname(importer))
        if near is None:
            return DEFAULT
        config = self.configs[near]
        if config.selects(importer):
            return config
        queue, seen = list(config.references), {near}
        while queue:
            ref = queue.pop(0)
            if ref in seen or ref not in self.configs:
                continue
            seen.add(ref)
            if self.configs[ref].selects(importer):
                return self.configs[ref]
            queue.extend(self.configs[ref].references)
        return config

    def package_of(self, importer: str) -> Package | None:
        scope = self.scopes.get(dirname(importer))
        return None if scope is None else self.packages[scope]


def _ancestry(paths: frozenset[str]) -> frozenset[str]:
    dirs = {""}
    for path in paths:
        parent = dirname(path)
        while parent not in dirs:
            dirs.add(parent)
            parent = dirname(parent)
    return frozenset(dirs)


def _nearest(
    dirs: frozenset[str], present: set[str], name: str
) -> dict[str, str | None]:
    """Directory -> the nearest ``<dir>/<name>`` in ``present`` at or above it."""
    out: dict[str, str | None] = {}
    for d in sorted(dirs, key=lambda p: (p.count("/") + bool(p), p)):
        own = join(d, name)
        out[d] = own if own in present else (out[dirname(d)] if d else None)
    return out


def build(repo_root: Path, listed: list[str]) -> Roots:
    files = frozenset(listed)
    dirs = _ancestry(files)
    packages = read_packages(repo_root, listed)
    by_name = names(packages)
    configs, issues = read_configs(repo_root, files, by_name)
    package_files = {join(d, "package.json") for d in packages}
    scopes = {
        d: (None if p is None else dirname(p))
        for d, p in _nearest(dirs, package_files, "package.json").items()
    }
    return Roots(
        files, dirs, configs, _nearest(dirs, set(configs), CONFIG), packages, by_name,
        scopes, issues,
    )  # fmt: skip


# --- files ------------------------------------------------------------------------


def _script(path: str) -> str | None:
    suffix = posixpath.splitext(path)[1]
    return suffix if suffix in _SUBST else None


def file_of(roots: Roots, path: str, strict: bool, js: bool) -> str | None:
    """The listed file a path names, by tsc's extension rules; ``strict`` is ES-module
    mode under node16, ``js`` whether script files may be picked."""
    suffix = _script(path)
    if suffix is not None:
        return _substituted(roots, path, suffix, strict, js)
    if path.endswith(_EXACT):
        return path if path in roots.files else None
    hit = _added(roots, path, strict, js) or _declared(roots, path)
    return hit if hit is not None or strict else _directory(roots, path, js)


def _substituted(
    roots: Roots, path: str, suffix: str, strict: bool, js: bool
) -> str | None:
    stem = path[: -len(suffix)]
    hit = next((stem + s for s in _SUBST[suffix] if _ok(roots, stem + s, js)), None)
    return hit if hit is not None or strict else _directory(roots, path, js)


def _added(roots: Roots, path: str, strict: bool, js: bool) -> str | None:
    if strict:
        return None
    return next((path + s for s in _ADDED if _ok(roots, path + s, js)), None)


def _declared(roots: Roots, path: str) -> str | None:
    """tsc's arbitrary-extension lookup: `./a.css` names a declaration `a.d.css.ts`."""
    stem, suffix = posixpath.splitext(path)
    found = f"{stem}.d{suffix}.ts"
    return found if suffix and found in roots.files else None


def _ok(roots: Roots, path: str, js: bool) -> bool:
    return (js or posixpath.splitext(path)[1] not in _SCRIPT) and path in roots.files


def _directory(roots: Roots, path: str, js: bool) -> str | None:
    if path not in roots.dirs:
        return None
    package = roots.packages.get(path)
    for entry in package.entries if package is not None else ():
        target = join(path, entry)
        if target != path and inside(target):
            hit = file_of(roots, target, False, js)
            if hit is not None:
                return hit
    index = join(path, "index")
    return next((index + s for s in _ADDED if _ok(roots, index + s, js)), None)


# --- package.json exports and imports ---------------------------------------------

_BLOCKED = object()


def _subpaths(exports: object) -> dict[str, object] | None:
    if not isinstance(exports, dict):
        return {".": exports}
    dotted = [str(k).startswith(".") for k in exports]
    if all(dotted):
        return exports
    return None if any(dotted) else {".": exports}


def match_table(key: str, table: object, conditions: frozenset[str]) -> str | None:
    """PACKAGE_IMPORTS_EXPORTS_RESOLVE: an exact key first, then the pattern key with
    the longest text before its `*`, then the longest pattern."""
    if not isinstance(table, dict):
        return None
    if key in table and "*" not in key and not key.endswith("/"):
        return _target(table[key], "", conditions)
    patterns = sorted(
        (
            p
            for p in table
            if isinstance(p, str) and p.count("*") == 1 and _fits(key, p)
        ),
        key=lambda p: (p.index("*"), len(p)),
        reverse=True,
    )
    if not patterns:
        return None
    before, _, after = patterns[0].partition("*")
    return _target(
        table[patterns[0]], key[len(before) : len(key) - len(after)], conditions
    )


def _fits(key: str, pattern: str) -> bool:
    before, _, after = pattern.partition("*")
    return (
        key.startswith(before)
        and key != before
        and key.endswith(after)
        and len(key) >= len(pattern)
    )


def _target(value: object, matched: str, conditions: frozenset[str]) -> str | None:
    found = _value(value, matched, conditions)
    return found if isinstance(found, str) else None


def _value(value: object, matched: str, conditions: frozenset[str]) -> object:
    if value is None:
        return _BLOCKED
    if isinstance(value, str):
        return value.replace("*", matched)
    if isinstance(value, list):
        found = (_value(v, matched, conditions) for v in value)
        return next((v for v in found if isinstance(v, str)), None)
    if isinstance(value, dict):
        return _condition(value, matched, conditions)
    return None


def _condition(
    table: dict[str, object], matched: str, conditions: frozenset[str]
) -> object:
    """The first condition, in key order, that matches and gives a target or null."""
    for condition, nested in table.items():
        if condition == "default" or condition in conditions:
            hit = _value(nested, matched, conditions)
            if hit is not None:
                return hit
    return None


def _conditions(config: Config, mode: str) -> frozenset[str]:
    node = {"node"} if config.resolution == "node16" else set()
    return frozenset({"types", mode, *node, *config.conditions})


def _package_file(roots: Roots, directory: str, target: str) -> Resolved:
    """A target inside a workspace package: its file, or the source its outDir path
    was built from."""
    path = join(directory, target)
    if not inside(path) or "\0" in path:
        return Resolved(None, "unresolved_local")
    hit = file_of(roots, path, False, True)
    if hit is not None and not hit.endswith(_DECLARATIONS):
        return Resolved(hit, "workspace")
    source = _source(roots, directory, path)
    if source is not None:
        return Resolved(source, "workspace_outdir")
    return Resolved(hit, "workspace" if hit is not None else "unresolved_local")


def _source(roots: Roots, directory: str, path: str) -> str | None:
    near = roots.nearest.get(directory)
    config = roots.configs.get(near) if near is not None else None
    if config is None or config.out_dir is None or config.path is None:
        return None
    if not path.startswith(config.out_dir + "/"):
        return None
    tail = path[len(config.out_dir) + 1 :]
    base = config.root_dir if config.root_dir is not None else dirname(config.path)
    for suffix, sources in _TO_SOURCE:
        if tail.endswith(suffix):
            stem = join(base, tail[: -len(suffix)])
            return next((stem + s for s in sources if stem + s in roots.files), None)
    return file_of(roots, join(base, tail), False, True)


def _package(
    roots: Roots, directory: str, sub: str, mode: str, config: Config
) -> Resolved:
    package = roots.packages[directory]
    if package.exports is not None and config.resolution in ("bundler", "node16"):
        table = _subpaths(package.exports)
        target = match_table(sub, table, _conditions(config, mode))
        if target is None or not target.startswith("./"):
            return Resolved(None, "unresolved_local")
        return _package_file(roots, directory, target)
    if sub != ".":
        hit = file_of(roots, join(directory, sub), False, config.allow_js)
        return Resolved(hit, "workspace" if hit else "unresolved_local")
    for entry in package.entries:
        found = _package_file(roots, directory, entry)
        if found.target is not None:
            return found
    hit = _directory_index(roots, directory, config.allow_js)
    return Resolved(hit, "workspace" if hit else "unresolved_local")


def _directory_index(roots: Roots, directory: str, js: bool) -> str | None:
    index = join(directory, "index")
    return next((index + s for s in _ADDED if _ok(roots, index + s, js)), None)


def _imports(
    roots: Roots, spec: str, importer: str, mode: str, config: Config
) -> Resolved:
    package = roots.package_of(importer)
    if package is None:
        return Resolved(None, "unresolved_local")
    target = match_table(spec, package.imports, _conditions(config, mode))
    if target is None:
        return Resolved(None, "unresolved_local")
    if target.startswith("./"):
        found = _package_file(roots, package.directory, target)
        reason = "package_imports" if found.target is not None else found.reason
        return Resolved(found.target, reason)
    if target.startswith(("../", "/")) or ":" in target:
        return Resolved(None, "unresolved_local")
    return _named(roots, target, importer, mode, config)


# --- specifiers -------------------------------------------------------------------


def _mode(site: ImportSite, importer: str, roots: Roots, config: Config) -> str:
    """The resolution mode: `require` or `import`, as tsc's getModeForUsageLocation."""
    if site.kind in ("require", "import_equals"):
        return "require"
    if site.kind == "dynamic_import" or config.resolution != "node16":
        return "import"
    if importer.endswith((".mts", ".mjs")):
        return "import"
    if importer.endswith((".cts", ".cjs")):
        return "require"
    package = roots.package_of(importer)
    return "import" if package is not None and package.esm else "require"


def _asset(spec: str) -> bool:
    return posixpath.splitext(spec.split("?", 1)[0])[1].lower() in _ASSETS


def _relative(
    roots: Roots, spec: str, importer: str, strict: bool, config: Config
) -> Resolved:
    target = join(dirname(importer), spec)
    if not inside(target):
        return Resolved(None, "external")
    hit = file_of(roots, target, strict, config.allow_js) or _root_dirs(
        roots, target, strict, config
    )
    if hit is not None:
        return Resolved(hit, "relative")
    return Resolved(None, "asset" if _asset(spec) else "relative_missing")


def _root_dirs(roots: Roots, target: str, strict: bool, config: Config) -> str | None:
    for base in config.root_dirs:
        if target.startswith(base + "/"):
            tail = target[len(base) + 1 :]
            for other in config.root_dirs:
                hit = file_of(roots, join(other, tail), strict, config.allow_js)
                if hit is not None:
                    return hit
    return None


def _rank(pattern: str, spec: str) -> tuple[tuple[int, int], str] | None:
    """How well a ``paths`` pattern matches: an exact pattern above any wildcard, then
    the longest text before the `*`; and the text the `*` captured."""
    before, star, after = pattern.partition("*")
    if not star:
        return ((1, 0), "") if spec == pattern else None
    fits = spec.startswith(before) and spec.endswith(after)
    if not fits or len(spec) < len(before) + len(after):
        return None
    return (0, len(before)), spec[len(before) : len(spec) - len(after)]


def _alias(
    roots: Roots, spec: str, strict: bool, config: Config
) -> tuple[Resolved | None, bool]:
    """(resolution through ``paths``, whether a pattern other than `*` matched)."""
    ranked = [
        (found, targets, pattern)
        for pattern, targets in config.paths
        if (found := _rank(pattern, spec)) is not None
    ]
    if not ranked:
        return None, False
    (_, captured), targets, pattern = max(ranked, key=lambda r: r[0][0])
    for target in targets:
        path = join("", target.replace("*", captured))
        hit = file_of(roots, path, strict, config.allow_js) if inside(path) else None
        if hit is not None:
            return Resolved(hit, "alias"), True
    return None, pattern != "*"


def _named(
    roots: Roots, spec: str, importer: str, mode: str, config: Config
) -> Resolved:
    parts = spec.split("/")
    name = "/".join(parts[:2] if spec.startswith("@") else parts[:1])
    if name in roots.names:
        return _package(roots, roots.names[name], "." + spec[len(name) :], mode, config)
    if _looks_local(roots, spec, importer):
        return Resolved(None, "alias_unconfigured")
    return Resolved(None, "external")


def _looks_local(roots: Roots, spec: str, importer: str) -> bool:
    """A bare specifier only a bundler alias could mean: `@/`, `~`, or a first segment
    naming a directory at the repository's or the importer's package root."""
    if spec.startswith(("@/", "~")):
        return True
    first = spec.split("/", 1)[0]
    if first in _BUILTINS or first.startswith(("@", "node:")) or "/" not in spec:
        return False
    package = roots.package_of(importer)
    home = package.directory if package is not None else ""
    return first in roots.dirs or join(home, first) in roots.dirs


def _bare(
    roots: Roots, spec: str, importer: str, mode: str, config: Config
) -> Resolved:
    strict = mode == "import" and config.resolution == "node16"
    found, matched = _alias(roots, spec, strict, config)
    if found is not None:
        return found
    if config.base_url is not None:
        hit = file_of(roots, join(config.base_url, spec), strict, config.allow_js)
        if hit is not None:
            return Resolved(hit, "base_url")
    if spec.startswith("#"):
        return _imports(roots, spec, importer, mode, config)
    named = _named(roots, spec, importer, mode, config)
    if named.target is None and _asset(spec):
        return Resolved(None, "asset")
    if named.reason == "external" and matched:
        return Resolved(None, "unresolved_local")
    return named


def _reference(roots: Roots, spec: str, importer: str) -> Resolved:
    target = join(dirname(importer), spec)
    for choice in (target, target + ".ts", target + ".d.ts"):
        if inside(choice) and choice in roots.files:
            return Resolved(choice, "reference")
    return Resolved(None, "relative_missing")


def resolve(site: ImportSite, importer: str, roots: Roots) -> Resolved:
    spec = site.specifier
    if spec is None:
        return Resolved(None, site.blind or "dynamic")
    if "\0" in spec:
        return Resolved(None, "nul")
    if site.kind == "reference_path":
        return _reference(roots, spec, importer)
    config = roots.config(importer)
    mode = _mode(site, importer, roots, config)
    if spec.startswith(("./", "../", "/")) or spec in (".", ".."):
        strict = mode == "import" and config.resolution == "node16"
        return _relative(roots, spec, importer, strict, config)
    return _bare(roots, spec, importer, mode, config)
