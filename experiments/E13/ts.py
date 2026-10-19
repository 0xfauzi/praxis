"""E13 TypeScript and JavaScript adapter: the compiler-API route ('api') and the
``tsc --traceResolution`` parse (route 'trace')."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath

from shapes import (  # experiments/E13/shapes.py, beside this script on sys.path
    AdapterError,
    Oracle,
    Pair,
    head_of,
    keep_listed,
    listed_files,
    relative,
    run,
    to_dirs,
)

_TS_API = """\
const ts = require(process.argv[2]);
const host = { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} };
const cfg = ts.getParsedCommandLineOfConfigFile(process.argv[3], {}, host);
const program = ts.createProgram({
  rootNames: cfg.fileNames, options: cfg.options,
  projectReferences: cfg.projectReferences,
});
const out = { files: cfg.fileNames.length, config_errors: cfg.errors.length,
  edges: [], unresolved: [],
  program: program.getSourceFiles().map((sf) => sf.fileName) };
program.forEachResolvedModule((res, name, mode, filePath) => {
  const sf = program.getSourceFileByPath(filePath);
  const src = sf ? sf.fileName : filePath;
  if (res.resolvedModule) out.edges.push([src, res.resolvedModule.resolvedFileName]);
  else out.unresolved.push([src, name]);
});
process.stdout.write(JSON.stringify(out));
"""

_RESOLVING = re.compile(r"^======== Resolving module '(.+)' from '(.+)'\. ========$")
_RESOLVED = re.compile(
    r"^======== Module name '(.+)' was successfully resolved to '(.+?)'"
    r"(?: with Package ID '.+')?\. ========$"
)
_UNRESOLVED = re.compile(r"^======== Module name '(.+)' was not resolved\. ========$")
_JS = ("**/*.js", "**/*.cjs", "**/*.mjs", "**/*.jsx")
_TS_JS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _ts_config(repo: Path, tmp: Path) -> tuple[Path, bool]:
    """The project's tsconfig.json, or a generated one for plain JavaScript."""
    own = repo / "tsconfig.json"
    if own.is_file():
        return own, False
    base = str(repo.resolve())
    config = {
        "compilerOptions": {
            "allowJs": True,
            "checkJs": False,
            "noEmit": True,
            "module": "nodenext",
            "moduleResolution": "nodenext",
            "maxNodeModuleJsDepth": 0,
            "skipLibCheck": True,
            "types": [],
        },
        "include": [f"{base}/{g}" for g in _JS],
        "exclude": [f"{base}/**/node_modules"],
    }
    path = tmp / "tsconfig.json"
    path.write_text(json.dumps(config))
    return path, True


def _typescript_lib() -> Path:
    tsc = shutil.which("tsc")
    if tsc is None:
        raise AdapterError("tsc not on PATH")
    lib = Path(tsc).resolve().parent.parent / "lib" / "typescript.js"
    if not lib.is_file():
        raise AdapterError("typescript.js not found beside tsc")
    return lib


def _ts_api(
    config: Path, tmp: Path
) -> tuple[list[Pair], list[Pair], dict[str, object]]:
    script = tmp / "resolve.js"
    script.write_text(_TS_API)
    done = run(["node", str(script), str(_typescript_lib()), str(config)], tmp)
    if done.returncode != 0 or not done.stdout:
        raise AdapterError("compiler-API script failed")
    data = json.loads(done.stdout)
    edges = [(a, b) for a, b in data["edges"]]
    misses = [(a, b) for a, b in data["unresolved"]]
    counts: dict[str, object] = {"files": data["files"]}
    counts["config_errors"] = data["config_errors"]
    counts["program"] = data["program"]
    return edges, misses, counts


def parse_trace(text: str) -> tuple[list[Pair], list[Pair]]:
    """(importer, resolved file) and (importer, specifier) for unresolved names."""
    edges: list[Pair] = []
    misses: list[Pair] = []
    importer = ""
    for line in text.splitlines():
        if m := _RESOLVING.match(line):
            importer = m.group(2)
        elif m := _RESOLVED.match(line):
            edges.append((importer, m.group(2)))
        elif m := _UNRESOLVED.match(line):
            misses.append((importer, m.group(1)))
    return edges, misses


def _ts_trace(
    config: Path, tmp: Path
) -> tuple[list[Pair], list[Pair], dict[str, object]]:
    argv = ["tsc", "--noEmit", "--traceResolution", "-p", str(config)]
    done = run(argv, tmp)
    edges, misses = parse_trace(done.stdout)
    if not edges and not misses:
        raise AdapterError("tsc trace held no module resolution")
    errors = len(re.findall(r"error TS\d+", done.stdout))
    return edges, misses, {"exit_code": done.returncode, "diagnostics": errors}


def _ts_edges(repo: Path, raw: list[Pair], listed: set[str]) -> tuple[list[Pair], int]:
    pairs: list[Pair] = []
    outside = 0
    for src, dst in raw:
        a, b = relative(src, repo), relative(dst, repo)
        if a is None or b is None or "node_modules" in PurePosixPath(b).parts:
            outside += 1
            continue
        pairs.append((a, b))
    kept, dropped = keep_listed(pairs, listed)
    return kept, outside + dropped


def _is_relative(specifier: str) -> bool:
    return specifier.startswith((".", "/"))


def ts_oracle(repo: Path, route: str = "api") -> Oracle:
    listed = listed_files(repo)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="e13-ts-") as name:
        tmp = Path(name)
        config, generated = _ts_config(repo, tmp)
        resolve = _ts_api if route == "api" else _ts_trace
        raw, misses, counts = resolve(config, tmp)
    seconds = time.perf_counter() - start
    edges, external = _ts_edges(repo, raw, listed)
    in_repo_misses = sum(_is_relative(spec) for _, spec in misses)
    program = counts.pop("program", None)
    if isinstance(program, list):
        inside = {relative(f, repo) for f in program} & listed
        counts["program_files_listed"] = len(inside)
    counts["listed_ts_js"] = sum(p.endswith(_TS_JS_SUFFIXES) for p in listed)
    detail: dict[str, object] = {
        "route": route,
        "generated_config": generated,
        "resolutions": len(raw),
        "external_or_unlisted_dropped": external,
        "unresolved_bare_specifiers": len(misses) - in_repo_misses,
        **counts,
    }
    return Oracle(
        "ts/js",
        repo.name,
        head_of(repo),
        edges,
        to_dirs(edges),
        in_repo_misses,
        seconds,
        detail,
    )
