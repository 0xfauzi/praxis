"""E13 Go adapter: ``go list -deps -json`` for packages and the toolchain's own
``go/parser`` for per-file imports."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from shapes import (  # experiments/E13/shapes.py, beside this script on sys.path
    AdapterError,
    Oracle,
    Pair,
    dir_of,
    head_of,
    relative,
    run,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_GO_IMPORTS = """\
package main

import (
	"bufio"
	"encoding/json"
	"go/parser"
	"go/token"
	"os"
	"strconv"
)

func main() {
	fset := token.NewFileSet()
	out := map[string][]string{}
	scan := bufio.NewScanner(os.Stdin)
	for scan.Scan() {
		path := scan.Text()
		f, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			out[path] = nil
			continue
		}
		imps := []string{}
		for _, s := range f.Imports {
			p, _ := strconv.Unquote(s.Path.Value)
			imps = append(imps, p)
		}
		out[path] = imps
	}
	json.NewEncoder(os.Stdout).Encode(out)
}
"""
_GO_FILES = ("GoFiles", "CgoFiles", "TestGoFiles", "XTestGoFiles")
_GO_IMPORT_FIELDS = ("Imports", "TestImports", "XTestImports")


def _go_env(cache: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GOPATH": str(cache / "gopath"),
        "GOMODCACHE": str(cache / "mod"),
        "GOCACHE": str(cache / "build"),
        # local: never fetch a toolchain unless the run asks for it by name.
        "GOTOOLCHAIN": os.environ.get("E13_GOTOOLCHAIN", "local"),
        "GOWORK": "off",
        "GOFLAGS": "-mod=readonly",
    }


def decode_stream(text: str) -> list[dict[str, object]]:
    """``go list -json`` prints concatenated objects, not an array."""
    decoder = json.JSONDecoder()
    found: list[dict[str, object]] = []
    at = 0
    while (at := _skip_space(text, at)) < len(text):
        obj, at = decoder.raw_decode(text, at)
        found.append(obj)
    return found


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at].isspace():
        at += 1
    return at


def _go_packages(repo: Path, env: dict[str, str]) -> list[dict[str, object]]:
    done = run(["go", "list", "-e", "-deps", "-json", "./..."], repo, env)
    packages = decode_stream(done.stdout)
    if not packages:
        raise AdapterError("go list printed no package")
    return packages


def _main_packages(packages: list[dict[str, object]], repo: Path) -> dict[str, str]:
    """Import path -> repository-relative directory, main module only."""
    found: dict[str, str] = {}
    for pkg in packages:
        module = pkg.get("Module")
        if isinstance(module, dict) and module.get("Main"):
            rel = relative(str(pkg["Dir"]), repo)
            found[str(pkg["ImportPath"])] = rel or "."
    return found


def _strings(pkg: dict[str, object], keys: Iterable[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        got = pkg.get(key)
        values += [str(v) for v in got] if isinstance(got, list) else []
    return values


def _go_file_imports(
    paths: list[str], env: dict[str, str], tmp: Path
) -> dict[str, list[str] | None]:
    (tmp / "imports.go").write_text(_GO_IMPORTS)
    done = run(["go", "run", "imports.go"], tmp, env, stdin="\n".join(paths))
    if done.returncode != 0:
        raise AdapterError("the go/parser helper failed")
    parsed: dict[str, list[str] | None] = json.loads(done.stdout)
    return parsed


def _go_dir_edges(packages: list[dict[str, object]], main: dict[str, str]) -> set[Pair]:
    edges: set[Pair] = set()
    for pkg in packages:
        src = main.get(str(pkg.get("ImportPath")))
        if src is None:
            continue
        for imp in _strings(pkg, _GO_IMPORT_FIELDS):
            if imp in main and main[imp] != src:
                edges.add((src, main[imp]))
    return edges


def _go_file_edges(
    repo: Path, files: dict[str, list[str] | None], main: dict[str, str], module: str
) -> tuple[set[Pair], int, int]:
    """(file, package directory) pairs, in-module imports with no package, and files
    the parser refused."""
    edges: set[Pair] = set()
    unresolved = failed = 0
    for path, imports in files.items():
        if imports is None:
            failed += 1
            continue
        src = relative(path, repo) or path
        for imp in imports:
            if imp in main:
                edges.add((src, main[imp]))
            else:
                unresolved += imp == module or imp.startswith(module + "/")
    return {(s, d) for s, d in edges if dir_of(s) != d}, unresolved, failed


def _go_module_path(repo: Path, env: dict[str, str]) -> str:
    done = run(["go", "list", "-m"], repo, env)
    return done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""


def go_oracle(repo: Path, scratch: Path) -> Oracle:
    env = _go_env(scratch / "go")
    start = time.perf_counter()
    packages = _go_packages(repo, env)
    main = _main_packages(packages, repo)
    module = _go_module_path(repo, env)
    paths = sorted(
        str(Path(str(p["Dir"]), f))
        for p in packages
        if str(p.get("ImportPath")) in main
        for f in _strings(p, _GO_FILES)
    )
    with tempfile.TemporaryDirectory(prefix="e13-go-") as tmp:
        files = _go_file_imports(paths, env, Path(tmp))
    edges, unresolved, failed = _go_file_edges(repo, files, main, module)
    seconds = time.perf_counter() - start
    errors = sum(
        1 for p in packages if str(p.get("ImportPath")) in main and "Error" in p
    )
    detail: dict[str, object] = {
        "module": module,
        "main_packages": len(main),
        "all_packages": len(packages),
        "files": len(paths),
        "file_parse_failures": failed,
        "packages_with_error": errors,
        "edge_dst": "package directory",
    }
    return Oracle(
        "go",
        repo.name,
        head_of(repo),
        sorted(edges),
        sorted(_go_dir_edges(packages, main)),
        unresolved,
        seconds,
        detail,
    )
