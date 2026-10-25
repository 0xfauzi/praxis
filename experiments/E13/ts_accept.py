"""E13 language acceptance for TypeScript and JavaScript: x1-ts-v1 against the
compiler-API site oracle (``ts_sites.py``), one JSON object per repository with the
fields of docs/design/04-languages.md section 4.4, scored by the rules of 4.3 and 4.5.

Rules, fixed before any held-out data (4.3, 4.5; the choices below the list were made
during development on ky, zod, express, zustand, immer and passport and are frozen with
the code):

- Product files only. A file is dropped on both the source and the target side when the
  mechanical map's fixed classes hold it: praxis's own (``components.FIXED``) plus the
  TypeScript members of 4.3.1 (``*.test.*``, ``*.spec.*``, ``__tests__/``, ``test-d/``,
  ``*.test-d.ts``; ``package.json``, ``tsconfig*.json``, ``*.config.{js,ts,mjs,cjs}``),
  ``examples``, ``vendored`` and ``generated`` (a marker in the first 1 KB, or
  ``linguist-generated`` in the root ``.gitattributes``).
- Grain: file to file, distinct edges, self-pairs dropped. Precision and recall with
  Wilson 95 percent intervals. An oracle edge whose target is not a TypeScript or
  JavaScript file (``resolveJsonModule`` gives ``.json``) is outside the construct
  (1.2), is removed from the oracle side and is counted under ``excluded``.
- Component grain: both edge sets lifted through the mechanical map that
  ``components.derive`` cuts from the tracked files left after the TypeScript fixed
  classes are removed.
- Sites are matched by (file, line, specifier, kind) as multisets. ``deferred`` is
  compared as "does not run when the file loads": the oracle's dynamic-import or
  function-ancestor flag, or its type-only flag (design 1.3), against praxis's
  ``deferred``; ``type_only`` is compared as is.
- A4: per product file, read plus imports plus resolve of every site, roots built once,
  median of ``--reps`` runs; p95 by nearest rank over files; ms per KB is the median over
  files above 4 KB, against X1-Python on praxis's ``src/praxis`` in the same process.
- A5: an exception from the reader or the resolver, and a parse failure on a file the
  oracle parsed with no parse diagnostic.

Disagreement classes are the closed list of 4.4. An edge only the extractor has is
``oracle_env`` when the oracle left its site unresolved and praxis resolved it through a
workspace package's outDir mapping, or through a workspace package with no node_modules
link above the importer.

Usage: ts_accept.py REPO --url URL --install CMD --json OUT [--reps N] [--baseline DIR]
Run from the praxis checkout with ``uv run python experiments/E13/ts_accept.py``.
"""

from __future__ import annotations

import argparse
import bisect
import fnmatch
import json
import math
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path, PurePosixPath

from shapes import head_of, listed_files, run
from ts_sites import SCRIPT

from praxis import components
from praxis.extract import static_imports
from praxis.lang import Unparsed, claimant
from praxis.lang.python import LANGUAGE as PYTHON
from praxis.lang.typescript import LANGUAGE as TS

HERE = Path(__file__).resolve().parent
GATE = 0.98
MIN_EDGES = 200
P95_MS = 10.0
CODE = TS.SUFFIXES
TS_FIXED = (
    components.Rule("tests", ("*.test.*", "*.spec.*", "__tests__/*", "*/__tests__/*",
                              "test-d/*", "*/test-d/*", "*.test-d.ts")),
    components.Rule("config", ("package.json", "*/package.json", "tsconfig*.json",
                               "*/tsconfig*.json", "*.config.js", "*.config.ts",
                               "*.config.mjs", "*.config.cjs")),
    components.Rule("examples", ("examples/*", "*/examples/*", "example/*",
                                 "*/example/*")),
    components.Rule("vendored", ("vendor/*", "*/vendor/*", "third_party/*",
                                 "*/third_party/*", "node_modules/*",
                                 "*/node_modules/*")),
)  # fmt: skip
_MARKER = re.compile(rb"@generated|Code generated .{0,200}DO NOT EDIT", re.S)
Key = tuple[str, int, str, str]
Pair = tuple[str, str]


# --- product files ------------------------------------------------------------------


def _linguist(root: Path) -> list[str]:
    path = root / ".gitattributes"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) > 1 and any(a in ("linguist-generated", "linguist-generated=true")
                                  for a in parts[1:]):  # fmt: skip
            out.append(parts[0].lstrip("/"))
    return out


def _generated(root: Path, path: str, linguist: list[str]) -> bool:
    if any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(PurePosixPath(path).name, g)
           for g in linguist):  # fmt: skip
        return True
    if not path.endswith(CODE):
        return False
    with (root / path).open("rb") as handle:
        return bool(_MARKER.search(handle.read(1024)))


def fixed_class(root: Path, path: str, linguist: list[str]) -> str | None:
    found = components._match(components.FIXED + TS_FIXED, path)
    if found is None and _generated(root, path, linguist):
        return "generated"
    return found


def product_files(root: Path, listed: set[str]) -> tuple[set[str], Counter[str]]:
    linguist = _linguist(root)
    classes = {p: fixed_class(root, p, linguist) for p in listed}
    counts = Counter(c for c in classes.values() if c is not None)
    return {p for p, c in classes.items() if c is None}, counts


def mechanical_map(root: Path) -> components.ComponentMap:
    """components.derive over the tracked files the TypeScript fixed classes leave;
    derive applies praxis's own fixed classes itself."""
    linguist = _linguist(root)
    kept = [
        p
        for p in components._ls_files(root)
        if components._match(TS_FIXED, p) is None
        and not ((root / p).is_file() and _generated(root, p, linguist))
    ]
    return components.derive(root, f"e13-{root.name}", kept)


# --- the two sides -------------------------------------------------------------------


def oracle(root: Path, files: list[str]) -> dict[str, object]:
    tsc = run(["sh", "-c", "command -v tsc"], root).stdout.strip()
    lib = Path(tsc).resolve().parent.parent / "lib" / "typescript.js"
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="e13-ts-sites-") as tmp:
        script = Path(tmp) / "ts_sites.js"
        script.write_text(SCRIPT)
        argv = ["node", "--max-old-space-size=8192", str(script), str(lib), str(root)]
        done = run(argv, root, stdin="\n".join(files))
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(f"site oracle failed: {done.stderr[-2000:]}")
    data: dict[str, object] = json.loads(done.stdout)
    data["seconds"] = round(time.perf_counter() - start, 2)
    return data


def _line_of(source: bytes) -> list[int]:
    return [0, *(m.end() for m in re.finditer(rb"\n", source))]


def extractor(root: Path, listed: list[str]) -> dict[str, object]:
    """Every TS/JS file's sites with their resolutions, read through x1-ts-v1's own
    methods; exceptions and parse failures recorded per file."""
    start = time.perf_counter()
    files = TS.files(root, listed)
    roots = TS.roots(root, [*files, *(p for p in listed if claimant(p) is None)])
    sites: dict[str, list[dict[str, object]]] = {}
    failures: dict[str, str] = {}
    exceptions: dict[str, str] = {}
    for path in files:
        try:
            sites[path] = _read_one(root, path, roots)
        except Unparsed as exc:
            failures[path] = str(exc)
        except Exception as exc:  # A5 counts every one.  # noqa: BLE001
            exceptions[path] = f"{type(exc).__name__}: {exc}"
    return {
        "files": files,
        "sites": sites,
        "failures": failures,
        "exceptions": exceptions,
        "issues": list(roots.issues),
        "seconds": round(time.perf_counter() - start, 2),
    }


def _read_one(root: Path, path: str, roots: object) -> list[dict[str, object]]:
    source = (root / path).read_bytes()
    lines = _line_of(source)
    out = []
    for site in TS.imports(path, source):
        found = TS.resolve(site, path, roots)
        out.append({
            "line": bisect.bisect_right(lines, site.start),
            "specifier": site.specifier,
            "kind": site.kind,
            "deferred": site.deferred,
            "type_only": site.type_only,
            "blind": site.blind,
            "target": found.target,
            "reason": found.reason,
        })  # fmt: skip
    return out


def shipped_edges(root: Path, files: set[str]) -> set[Pair]:
    """X1 file edges through extract.static_imports, the path the recorder uses."""
    graph = static_imports(root, lambda p: p)
    return {(e.src, e.dst) for e in graph.edges if e.src in files}


# --- scoring -------------------------------------------------------------------------


def wilson(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    z, p = 1.959964, k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(centre - half, 4), round(centre + half, 4)]


def score(oracle_edges: set[Pair], extractor_edges: set[Pair]) -> dict[str, object]:
    both = len(oracle_edges & extractor_edges)
    no, ne = len(oracle_edges), len(extractor_edges)
    return {
        "oracle": no,
        "extractor": ne,
        "both": both,
        "precision": round(both / ne, 4) if ne else None,
        "recall": round(both / no, 4) if no else None,
        "precision_ci95": wilson(both, ne),
        "recall_ci95": wilson(both, no),
    }


def keyed(per_file: dict[str, list[dict[str, object]]], keep: set[str]) -> dict[Key, list[dict[str, object]]]:
    out: dict[Key, list[dict[str, object]]] = {}
    for path, sites in per_file.items():
        if path not in keep:
            continue
        for s in sites:
            if s["specifier"] is not None:
                key = (path, int(s["line"]), str(s["specifier"]), str(s["kind"]))
                out.setdefault(key, []).append(s)
    return out


def edge_set(per_file: dict[str, list[dict[str, object]]], product: set[str]) -> set[Pair]:
    return {
        (path, str(s["target"]))
        for path, sites in per_file.items()
        if path in product
        for s in sites
        if s["target"] in product and s["target"] != path
    }


def site_agreement(o: dict[Key, list[dict[str, object]]], x: dict[Key, list[dict[str, object]]]) -> dict[str, object]:
    matched = deferred = typed = 0
    for key in o.keys() & x.keys():
        for a, b in zip(o[key], x[key], strict=False):
            matched += 1
            deferred += bool(a["deferred"] or a["type_only"]) == bool(b["deferred"])
            typed += bool(a["type_only"]) == bool(b["type_only"])
    return {
        "oracle": sum(map(len, o.values())),
        "extractor": sum(map(len, x.values())),
        "matched": matched,
        "deferred_agree": deferred,
        "type_only_agree": typed,
        "deferred_rate": round(deferred / matched, 4) if matched else None,
        "type_only_rate": round(typed / matched, 4) if matched else None,
    }


def _target_of(site: dict[str, object] | None) -> str | None:
    return None if site is None else site["target"]  # type: ignore[return-value]


def _linked(root: Path, importer: str, spec: str) -> bool:
    """Whether a node_modules link for the package a bare specifier names exists in a
    directory at or above the importer: the oracle can only resolve through one."""
    parts = spec.split("/")
    name = "/".join(parts[:2] if spec.startswith("@") else parts[:1])
    here = PurePosixPath(importer).parent
    for folder in (here, *here.parents):
        if (root / folder / "node_modules" / name).exists():
            return True
    return False


def _extra_class(root: Path, o: dict[str, object] | None, x: dict[str, object]) -> str:
    if o is None:
        return "extract_extra"
    if o["target"] is not None or o.get("outside"):
        return "resolve_wrong"
    reason, spec = x["reason"], str(x["specifier"])
    if reason == "workspace_outdir":
        return "oracle_env"
    if reason == "workspace" and not _linked(root, str(x["path"]), spec):
        return "oracle_env"
    return "resolve_extra"


def _miss_class(o: dict[str, object], x: dict[str, object] | None) -> str:
    if x is None:
        return "extract_miss"
    return "resolve_miss" if x["target"] is None else "resolve_wrong"


def _sites_for(keyed_sites: dict[Key, list[dict[str, object]]], pair: Pair) -> list[tuple[Key, dict[str, object]]]:
    return [
        (key, s)
        for key, group in keyed_sites.items()
        if key[0] == pair[0]
        for s in group
        if s["target"] == pair[1]
    ]


def disagreements(
    root: Path,
    o: dict[Key, list[dict[str, object]]],
    x: dict[Key, list[dict[str, object]]],
    oracle_edges: set[Pair],
    extractor_edges: set[Pair],
) -> list[dict[str, object]]:
    out = []
    for pair in sorted(oracle_edges - extractor_edges):
        classes = Counter(
            _miss_class(s, (x.get(key) or [None])[0]) for key, s in _sites_for(o, pair)
        )
        out.append(_row(pair, "oracle", classes))
    for pair in sorted(extractor_edges - oracle_edges):
        classes = Counter(
            _extra_class(root, (o.get(key) or [None])[0], {**s, "path": key[0]})
            for key, s in _sites_for(x, pair)
        )
        out.append(_row(pair, "extractor", classes))
    return out


def _row(pair: Pair, side: str, classes: Counter[str]) -> dict[str, object]:
    top = classes.most_common(1)[0][0] if classes else "unknown"
    return {"src": pair[0], "dst": pair[1], "side": side, "class": top,
            "site_classes": dict(classes)}  # fmt: skip


def _local_looking(x: dict[str, object]) -> bool:
    spec = str(x["specifier"])
    local = ("relative_missing", "alias_unconfigured", "unresolved_local", "nul")
    return spec.startswith((".", "/", "#")) or x["reason"] in local


def both_unresolved(o: dict[Key, list[dict[str, object]]], x: dict[Key, list[dict[str, object]]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for key in o.keys() & x.keys():
        for a, b in zip(o[key], x[key], strict=False):
            if a["target"] is None and not a.get("outside") and b["target"] is None:
                if _local_looking(b):
                    counts[str(b["reason"])] += 1
    return dict(sorted(counts.items()))


def lift(cmap: components.ComponentMap, edges: set[Pair]) -> set[Pair]:
    lifted = {(components.assign(cmap, a), components.assign(cmap, b)) for a, b in edges}
    return {(a, b) for a, b in lifted if a != b}


# --- A4: per-file cost ---------------------------------------------------------------


def time_files(language: object, root: Path, files: list[str], roots: object, reps: int) -> list[list[object]]:
    """[path, bytes, median ms] per file: read, imports and resolve of every site."""
    rows: list[list[object]] = []
    for path in files:
        runs = []
        size = 0
        for _ in range(reps):
            start = time.perf_counter()
            source = (root / path).read_bytes()
            try:
                for site in language.imports(path, source):  # type: ignore[attr-defined]
                    if site.blind is None:
                        language.resolve(site, path, roots)  # type: ignore[attr-defined]
            except Unparsed:
                pass
            runs.append((time.perf_counter() - start) * 1000)
            size = len(source)
        rows.append([path, size, round(statistics.median(runs), 4)])
    return rows


def cost(rows: list[list[object]]) -> dict[str, object]:
    ms = sorted(float(r[2]) for r in rows)  # type: ignore[arg-type]
    big = [float(r[2]) / (int(r[1]) / 1024) for r in rows if int(r[1]) > 4096]  # type: ignore[call-overload]
    p95 = ms[max(0, math.ceil(0.95 * len(ms)) - 1)] if ms else None
    return {
        "files": len(rows),
        "p50_ms": round(statistics.median(ms), 4) if ms else None,
        "p95_ms": round(p95, 4) if p95 is not None else None,
        "max_ms": round(ms[-1], 4) if ms else None,
        "ms_per_kb_median_over_4kb": round(statistics.median(big), 4) if big else None,
        "files_over_10ms": sum(m > P95_MS for m in ms),
    }


def python_baseline(repo: Path, reps: int) -> dict[str, object]:
    """X1-Python on ``repo``'s ``src/praxis``, the same harness: the A4 reference."""
    listed = sorted(listed_files(repo))
    files = [p for p in listed if p.startswith("src/praxis/") and p.endswith(".py")]
    roots = PYTHON.roots(repo, PYTHON.files(repo, listed))
    rows = time_files(PYTHON, repo, files, roots, reps)
    return {"root": f"{repo}/src/praxis", **cost(rows), "per_file": rows}


# --- one repository ------------------------------------------------------------------


def _product_sites(sites: dict[str, list[dict[str, object]]], product: set[str]) -> dict[str, list[dict[str, object]]]:
    return {p: v for p, v in sites.items() if p in product}


def _construct(o_sites: dict[str, list[dict[str, object]]]) -> int:
    """Blank the target of every oracle site resolved to a non-code file (outside the
    construct, 1.2) and count them."""
    n = 0
    for sites in o_sites.values():
        for s in sites:
            if s["target"] is not None and not str(s["target"]).endswith(CODE):
                s["target"], s["outside"] = None, True
                n += 1
    return n


def measure(repo: Path, url: str, install: str, reps: int, baseline: dict[str, object]) -> dict[str, object]:
    listed = sorted(listed_files(repo))
    product, fixed = product_files(repo, set(listed))
    cmap = mechanical_map(repo)
    ts_files = TS.files(repo, listed)
    o = oracle(repo, ts_files)
    o_sites = {p: v["sites"] for p, v in o["files"].items()}  # type: ignore[union-attr]
    excluded = _construct(o_sites)
    x = extractor(repo, listed)
    x_sites: dict[str, list[dict[str, object]]] = x["sites"]  # type: ignore[assignment]
    oracle_edges = edge_set(o_sites, product)
    extractor_edges = edge_set(x_sites, product)
    shipped = {(a, b) for a, b in shipped_edges(repo, product) if b in product and a != b}
    ok, xk = keyed(o_sites, product), keyed(x_sites, product)
    rows = disagreements(repo, ok, xk, oracle_edges, extractor_edges)
    roots = TS.roots(repo, [*ts_files, *(p for p in listed if claimant(p) is None)])
    timed = time_files(TS, repo, [p for p in ts_files if p in product], roots, reps)
    parsed = {p for p, v in o["files"].items() if v["parse_errors"] == 0}  # type: ignore[union-attr]
    result = {
        "language": "ts/js",
        "repo": repo.name,
        "url": url,
        "commit": run(["git", "rev-parse", "HEAD"], repo).stdout.strip(),
        "head": head_of(repo),
        "map_version": cmap.version,
        "components": len(components.product_components(cmap)),
        "oracle": {
            "route": "api-sites",
            "tool_versions": {"typescript": o["typescript"], "node": o["node"]},
            "install_command": install,
            "seconds": o["seconds"],
            "diagnostics": {"config_errors": o["config_errors"],
                            "parse_errors": sum(v["parse_errors"] for v in o["files"].values())},  # type: ignore[union-attr]
        },
        "extractor": {
            "version": TS.VERSION,
            "seconds": x["seconds"],
            "per_file": timed,
            "issues": x["issues"],
            "shipped_edges_equal": shipped == extractor_edges,
        },
        "grain": "file",
        "files": {"listed": len(listed), "ts_js": len(ts_files),
                  "product_ts_js": sum(p in product for p in ts_files),
                  "fixed_classes": dict(sorted(fixed.items()))},
        "edges": score(oracle_edges, extractor_edges),
        "component_edges": score(lift(cmap, oracle_edges), lift(cmap, extractor_edges)),
        "component_sets_equal": lift(cmap, oracle_edges) == lift(cmap, extractor_edges),
        "sites": site_agreement(ok, xk),
        "disagreements": rows,
        "disagreement_classes": dict(Counter(r["class"] for r in rows)),
        "both_unresolved": both_unresolved(ok, xk),
        "excluded": {"out_of_construct": excluded},
        "a4": {**cost(timed), "python_ms_per_kb": baseline["ms_per_kb_median_over_4kb"]},
        "a5": {
            "exceptions": x["exceptions"],
            "parse_failures": {p: why for p, why in x["failures"].items()},  # type: ignore[union-attr]
            "parse_failures_on_oracle_parsed": sorted(set(x["failures"]) & parsed),  # type: ignore[arg-type]
        },
        "reasons": dict(Counter(str(s["reason"]) for v in _product_sites(x_sites, product).values() for s in v)),
    }
    result["verdict"], result["rules"] = verdict(result)
    result["rule"] = "04 section 4.5"
    return result


def _at_least(value: object, gate: float) -> bool:
    return isinstance(value, float | int) and value >= gate


def verdict(r: dict[str, object]) -> tuple[str, dict[str, object]]:
    e, c, s, a4, a5 = (r[k] for k in ("edges", "component_edges", "sites", "a4", "a5"))
    rules: dict[str, object] = {
        "A1_min_edges": e["oracle"] >= MIN_EDGES,  # type: ignore[index]
        "A1": _at_least(e["precision"], GATE) and _at_least(e["recall"], GATE),  # type: ignore[index]
        "A2": _at_least(s["deferred_rate"], GATE) and _at_least(s["type_only_rate"], GATE),  # type: ignore[index]
        "A3": r["component_sets_equal"] or (_at_least(c["precision"], GATE)  # type: ignore[index]
                                            and _at_least(c["recall"], GATE)),  # type: ignore[index]
        "A4": (a4["p95_ms"] is not None and a4["p95_ms"] <= P95_MS  # type: ignore[index]
               and a4["ms_per_kb_median_over_4kb"] is not None  # type: ignore[index]
               and a4["ms_per_kb_median_over_4kb"] <= a4["python_ms_per_kb"]),  # type: ignore[index]
        "A5": not a5["exceptions"] and not a5["parse_failures_on_oracle_parsed"],  # type: ignore[index]
    }
    if not rules["A1_min_edges"]:
        return "replace", rules
    passed = all(rules[k] for k in ("A1", "A2", "A3", "A4", "A5"))
    return ("pass" if passed else "fail"), rules


def say(text: str) -> None:
    sys.stdout.write(text + "\n")


def summary(r: dict[str, object]) -> None:
    e, c, s, a4 = r["edges"], r["component_edges"], r["sites"], r["a4"]
    say(f"{r['repo']} {r['head']} verdict {r['verdict']} {r['rules']}")
    say(f"  edges {e}")
    say(f"  component edges {c}")
    say(f"  sites {s}")
    say(f"  a4 {a4}  a5 {r['a5']}")
    say(f"  classes {r['disagreement_classes']} both_unresolved {r['both_unresolved']}")
    say(f"  excluded {r['excluded']} shipped_equal {r['extractor']['shipped_edges_equal']}")  # type: ignore[index]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="E13 TypeScript acceptance")
    parser.add_argument("repo", type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--install", default="none")
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--baseline", type=Path, default=HERE.parent.parent)
    args = parser.parse_args(argv)
    baseline = python_baseline(args.baseline.resolve(), args.reps)
    say(f"python baseline {({k: v for k, v in baseline.items() if k != 'per_file'})}")
    result = measure(args.repo.resolve(), args.url, args.install, args.reps, baseline)
    result["python_baseline"] = baseline
    summary(result)
    args.json.write_text(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
