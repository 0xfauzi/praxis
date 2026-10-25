"""E14: can praxis plant a fault at a known altitude in a real repository, keep it
invisible to a parse and an import, and get its label back from an extractor?

Design: docs/design/05-altitude.md (proposed). This is the measurement that comes before
any catch-task item is shown to anyone: the generators, the validity checks, the label
recovery and the item supply. It shows the owner nothing and asks nothing.

Levels and operators (design 05, section 3). A fault is an operator applied once, in a
scratch clone, to one file. A decoy is an operator of the same form whose label is
"not wrong" by construction.

  L1 constraint  c_relocate (fault): a statement that a scoped pygrep hook allows
                 only in its site file is copied into a new private function in an
                 in-scope file of another component. c_relocate_in_site (decoy): the
                 same statement into a new private function in the site file.
                 c_drop_check (fault, recognition control, no decoy): one CHECK clause
                 is removed from a DDL string literal.
  L2 flow        f_drop_call: one bare call statement into another component, inside a
                 function, becomes ``pass``. Intended fault when the (caller, callee)
                 component pair is a call edge in a base X2 trace, intended decoy when
                 it is in none. Verifiable only on a repository with a scenario file.
  L3 edge        g_add_edge: a module-level ``import <module of B> as _e14_dep`` in a
                 file of A, for a product pair with no X1 edge A -> B. Fault when B
                 already reaches A (a new component cycle), decoy when it does not and A
                 and B are not already in one cycle.
  L4 mechanism   m_negate_if, m_flip_compare (faults); m_mirror_compare (``a < b`` to
                 ``b > a``), m_swap_branches (``if c: X else: Y`` to
                 ``if not (c): Y else: X``) (decoys, equivalent by construction).
  L5 form        l_shift_int: an int literal k >= 1 used as a subscript, a slice bound
                 or a ``range`` argument becomes k + 1 (fault). l_rename_local: one
                 local name renamed throughout its function (decoy, equivalent by
                 construction).

Acceptance rule, written before the first run (2026-10-25).

For each repository named on the command line, at its committed HEAD in a
``git clone --local`` under a temporary $PRAXIS_HOME (deleted at exit; the owner's tree
and .git are only read by ``git clone``), every candidate site of every operator is
enumerated, and up to N faults and N decoys per level (default N = 20, split round
robin over the level's operators, seeded draw) are applied one at a time, checked and
restored. After each item the clone must show ``git status --porcelain`` empty, or the
run stops.

  (a) Valid: the mutated file parses (``ast.parse``); the mutated module imports in a
      fresh interpreter with the clone's source roots on PYTHONPATH and no bytecode
      written, whenever the base module imports; for c_drop_check the module's DDL still
      executes in ``sqlite3.connect(":memory:")``; for f_drop_call every scenario's exit
      code is unchanged. An item whose base module does not import is import-unknown:
      it is reported, and it is neither valid nor invalid.
  (b) Recovered: re-extraction on the mutant gives the planted label and nothing else.
      c_relocate: the hook's in-scope matches change in the target file only, by one
      or more. c_relocate_in_site: in-scope matches unchanged. c_drop_check: the CHECK
      count of the planted table drops by exactly one and every other table is
      unchanged.
      f_drop_call: a fault changes the component set or the call-edge set of at least
      one scenario trace; a decoy changes neither in any scenario. g_add_edge: X1 on the
      touched file gives the base edges plus exactly (A, B), and the new edge closes a
      cycle iff the item is a fault. L4 and L5: X1 on the touched file and the hook
      matches are unchanged, and the functions whose AST differs are exactly the planted
      function and the functions that enclose it.
  (c) Supply: candidates per operator, and distinct items at the level's grain (L1:
      hook or table, component, resource; L2: call site; L3: ordered component pair;
      L4 and L5: function).

  A level PASSES on a repository iff, separately for faults and for decoys among the
  sampled items with a known import result, at least 90 percent are valid, and every
  valid item is recovered (one recovery failure is a generator defect, named in the
  write-up). A level is SUPPLIED iff it passes and its estimated valid supply (distinct
  items times the valid share) is at least 20 faults and at least 20 decoys; the
  recognition control c_drop_check is reported beside L1 and not counted in it. A level
  is UNDETERMINED where no extractor can check its label (L2 without a scenario file).
  E14 passes iff no level on any repository fails (b): the generators are sound wherever
  they produce items. Supply is the result, not the gate.

  Prediction, written before the run: praxis supplies L1 (from
  derived-writes-only-in-store; judge-isolation has no instance to relocate), L3, L4 and
  L5, and L2 is checkable with the design 3.2 scenarios; telltale supplies L1, L3, L4
  and L5 with L2 undetermined; systemap supplies L3, L4 and L5, has no machine-readable
  constraint (L1 supply 0), and L2 is undetermined. L3 back-edge faults that break the
  module import are expected, and their share is reported.

Amended before the first counted run (2026-10-25), after three trial runs at N = 3
that are not results (docs/experiments/E14.md says what each showed). The rule above
stands except for these five changes, each made because a trial showed the original
could not work as written:

  1. The import check is ``direct`` (the module alone) or, when the base module fails
     that, ``entry`` (the console-script modules of pyproject.toml first, then the
     module): the order the program imports in. In the trial, 3 telltale items (in 2
     modules) and 1 systemap item failed a direct import of their base module with a
     circular-import error.
  2. L3 places the new import inside a new private function, for faults and decoys
     alike, and the card carries no placement. The module-level import is still made
     and import-tested on every L3 item, and its result is reported, not counted. In
     the trial, 4 of 6 module-level back edges on praxis broke the import, which makes
     a back edge visible without any model and makes placement leak the label.
  3. L2 is labelled by X2 after the mutation: fault when a deterministic scenario's
     component set or call-edge set differs from the base, decoy otherwise; recovered
     when a second trace of the mutant gives the same label. The component-pair guess
     is kept as ``prior`` and its agreement is reported. In the trial, 3 of 3 L2 items
     guessed as faults left every trace unchanged, and a positive control (hooks.run
     returning at once) moved two scenarios' edge sets, so X2 sees a real change and
     the guess was wrong. L2 draws 2N items, and its supply per label is the candidate
     items times the label's share of the drawn items.
  4. L2 call resolution also follows a parameter's annotation (``store: Store``), so a
     method call on a typed parameter resolves to the annotated class's component.
  5. c_drop_check removes a clause only when its text occurs exactly once in the
     module's runtime DDL as well as once in the source. In the third trial, one of 3
     praxis items dropped clauses in several tables: 12 of the 64 clauses whose text
     is unique in schema.py sit in a fragment that several tables share.

Nothing here prints or stores code text: the output holds component ids, paths, hook
ids, table names, operator names, qualified function names and counts.

Usage: uv run python experiments/E14/faults.py [--n 20] [--seed 20261025]
       [--out results.json] REPO[=SCENARIOS.toml] [REPO ...]
"""

# ruff: noqa: T201 - a runner's printed output is the record its write-up quotes.
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any

import flow
from ddl import Schema, drop_checks, has_ddl, probe
from edit import Candidate, changed_functions, enclosing, parse
from ops import add_edges, drop_calls, forms, mechanisms
from sites import Hook, Repo, contract_counts, hook_counts, read_hooks, relocations

from praxis import components as comp
from praxis.extract import (
    Scenario,
    _Index,
    _module_names,
    _python_files,
    reach,
    scratch_clone,
    static_imports,
)

LEVELS = ("L1", "L1c", "L2", "L3", "L4", "L5")
INTENTS = ("fault", "decoy")
MIN_SUPPLY = 20
MIN_VALID = 0.9
IMPORT = (
    "import importlib, sys\nfor name in sys.argv[1:]: importlib.import_module(name)"
)
IMPORT_TIMEOUT_S = 60.0
Edges = frozenset[tuple[str, str]]
Row = dict[str, Any]


@dataclass
class World:
    clone: Path
    cmap: comp.ComponentMap
    repo: Repo
    modules: dict[str, str]
    env: dict[str, str]
    hooks: list[Hook]
    graph: set[tuple[str, str]]
    reach: set[tuple[str, str]]
    scenarios: list[Scenario] | None
    entries: list[str]
    base_traces: dict[str, flow.Shape] = field(default_factory=dict)
    stable: set[str] = field(default_factory=set)
    imports: dict[str, str | None] = field(default_factory=dict)
    edges: dict[str, Edges] = field(default_factory=dict)
    schemas: dict[str, Schema] = field(default_factory=dict)

    def assign(self, path: str) -> str:
        return comp.assign(self.cmap, path)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, check=True, text=True
    )
    return done.stdout


def _root(path: str, module: str) -> str:
    parts = path[:-3].split("/")
    parts = parts[:-1] if parts[-1] == "__init__" else parts
    return "/".join(parts[: len(parts) - len(module.split("."))]) or "."


def _decodes(path: Path) -> bool:
    try:
        path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return True


def entries(clone: Path, modules: dict[str, str]) -> list[str]:
    """The console-script modules, the repository's own first import; else its top
    packages."""
    where = clone / "pyproject.toml"
    doc = tomllib.loads(where.read_text()) if where.is_file() else {}
    scripts = doc.get("project", {}).get("scripts", {})
    found = sorted({str(v).split(":")[0] for v in scripts.values()})
    return found or sorted({m.split(".")[0] for m in modules.values()})


def build(clone: Path, scenarios: list[Scenario] | None, home: Path) -> World:
    cmap = comp.derive(clone, "e14")
    product = set(comp.product_components(cmap))
    every = _python_files(clone)
    known = set(every)
    files = {
        p: comp.assign(cmap, p)
        for p in every
        if comp.assign(cmap, p) in product and _decodes(clone / p)
    }
    modules = {p: _module_names(p, known)[0] for p in files}
    roots = sorted({str(clone / _root(p, m)) for p, m in modules.items()})
    env = {
        **os.environ,
        "HOME": str(home / "home"),
        "PRAXIS_HOME": str(home / "praxis-home"),
        "TELLTALE_HOME": str(home / "telltale-home"),
        "PYTHONPATH": os.pathsep.join(roots),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    graph = static_imports(clone, partial(comp.assign, cmap))
    edges = {(e.src, e.dst) for e in graph.edges if {e.src, e.dst} <= product}
    hooks, _ = read_hooks(clone)
    return World(
        clone,
        cmap,
        Repo(clone, files, {}),
        modules,
        env,
        hooks,
        edges,
        reach(graph, sorted(product)),
        scenarios,
        entries(clone, modules),
    )


# Base facts, read while the file on disk is the base ---------------------------------


def imports_ok(w: World, path: str, mode: str) -> bool:
    """``direct`` imports the module alone; ``entry`` imports the console-script modules
    first, the order the program itself uses, for a module that is only importable
    after its package's import cycle has been entered from the top."""
    names = [w.modules[path]] if mode == "direct" else [*w.entries, w.modules[path]]
    try:
        done = subprocess.run(
            [sys.executable, "-c", IMPORT, *names],
            cwd=w.clone,
            env=w.env,
            capture_output=True,
            timeout=IMPORT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def x1(w: World, path: str) -> Edges:
    graph = static_imports(w.clone, w.assign, files=[path])
    return frozenset((e.src, e.dst) for e in graph.edges)


def base_facts(w: World, path: str) -> tuple[str | None, Edges]:
    """The import mode the base module passes (None: neither), and its X1 edges."""
    if path not in w.imports:
        modes = [m for m in ("direct", "entry") if imports_ok(w, path, m)]
        w.imports[path] = modes[0] if modes else None
        w.edges[path] = x1(w, path)
    return w.imports[path], w.edges[path]


# Recovery per level: (extra validity, recovered, detail) -----------------------------

Check = tuple[bool, bool, str]


def check_l1(w: World, c: Candidate, base: str, mutant: str) -> Check:
    before = hook_counts(w.hooks, c.path, base)
    after = hook_counts(w.hooks, c.path, mutant)
    if c.intent == "decoy":
        return True, after == before, "in_scope_matches_changed"
    hook = c.extra["hook"]
    grew = after.get(hook, 0) > before.get(hook, 0)
    rest = {k: v for k, v in after.items() if k != hook}
    same = rest == {k: v for k, v in before.items() if k != hook}
    return True, grew and same, "violation_not_seen"


def check_l1c(w: World, c: Candidate, _base: str, _mutant: str) -> Check:
    before = w.schemas[c.path]
    after = probe(w.clone, w.env, w.modules[c.path], c.path)
    if after is None or after.failed != before.failed:
        return False, False, "ddl_fails"
    if set(after.counts) != set(before.counts):
        return False, False, "tables_changed"
    table = c.extra["table"]
    expect = {**before.counts, table: before.counts[table] - 1}
    return True, after.counts == expect, "check_count"


def _moved(w: World, after: dict[str, flow.Shape]) -> bool:
    return any(
        (after[s].components, after[s].edges)
        != (w.base_traces[s].components, w.base_traces[s].edges)
        for s in w.stable
    )


def check_l2(w: World, c: Candidate, _base: str, _mutant: str) -> Check:
    """The label is X2's: fault when the mutant's traces differ from the base in a
    deterministic scenario. Recovered when a second trace of the mutant agrees."""
    assert w.scenarios is not None
    first = flow.traces(w.clone, w.cmap, w.scenarios, c.path)
    second = flow.traces(w.clone, w.cmap, w.scenarios, c.path)
    exits = all(first[s].exit_code == w.base_traces[s].exit_code for s in w.stable)
    label = "fault" if _moved(w, first) else "decoy"
    return exits, _moved(w, second) == (label == "fault"), label


def check_l3(w: World, c: Candidate, _base: str, _mutant: str) -> Check:
    pair = (c.extra["src"], c.extra["dst"])
    _, before = base_facts(w, c.path)
    after = x1(w, c.path)
    exact = pair not in before and after == before | {pair}
    cycle = (pair[1], pair[0]) in w.reach
    return True, exact and cycle == (c.intent == "fault"), "x1_or_cycle"


def module_level(w: World, c: Candidate, base: str, mode: str) -> bool | None:
    """Reported beside L3: does the same import at module level still import?"""
    alt = c.alt(base) if c.alt else None
    if alt is None:
        return None
    (w.clone / c.path).write_bytes(alt.encode())
    return imports_ok(w, c.path, mode)


def check_local(w: World, c: Candidate, base: str, mutant: str) -> Check:
    tb, tm = parse(base), parse(mutant)
    assert tb is not None
    assert tm is not None
    assert c.qualname is not None
    _, before = base_facts(w, c.path)
    same_edges = x1(w, c.path) == before
    same_hooks = hook_counts(w.hooks, c.path, base) == hook_counts(
        w.hooks, c.path, mutant
    )
    changed = changed_functions(tb, tm)
    local = c.qualname in changed and changed <= enclosing(c.qualname)
    return True, same_edges and same_hooks and local, "not_local"


CHECKS = {
    "L1": check_l1,
    "L1c": check_l1c,
    "L2": check_l2,
    "L3": check_l3,
    "L4": check_local,
    "L5": check_local,
}


# One item: apply, check, restore -----------------------------------------------------


def _row(c: Candidate) -> Row:
    return {
        "level": c.level,
        "op": c.op,
        "intent": c.intent,
        "component": c.component,
        "path": c.path,
        "qualname": c.qualname,
        "key": list(c.key),
        "card": c.card,
    }


def _restore(w: World, target: Path, data: bytes) -> None:
    target.write_bytes(data)
    flow.restore(w.clone, None)
    if _git(w.clone, "status", "--porcelain"):
        raise RuntimeError("the scratch clone was not restored; stopping")


def evaluate(w: World, c: Candidate) -> Row:
    row = _row(c)
    base = w.repo.text(c.path)
    mutant = c.mutate(base)
    if mutant is None or mutant == base:
        return {**row, "valid": False, "recovered": False, "reason": "unplaceable"}
    if parse(mutant) is None:
        return {**row, "valid": False, "recovered": False, "reason": "parse"}
    mode, _ = base_facts(w, c.path)
    target = w.clone / c.path
    data = target.read_bytes()
    target.write_bytes(mutant.encode())
    try:
        mutant_imports = imports_ok(w, c.path, mode) if mode else None
        extra, recovered, detail = CHECKS[c.level](w, c, base, mutant)
        if c.level == "L3" and mode:
            row["module_level_imports"] = module_level(w, c, base, mode)
    finally:
        _restore(w, target, data)
    row["import_mode"] = mode
    if c.level == "L2":
        row.update(prior=c.extra.get("prior"), intent=detail)
        detail = "trace_unstable"
    return _verdict_row(row, mutant_imports, extra, recovered, detail)


def _verdict_row(
    row: Row, imports: bool | None, extra: bool, recovered: bool, detail: str
) -> Row:
    if imports is None:
        return {**row, "valid": None, "recovered": None, "reason": "import_unknown"}
    if not imports or not extra:
        reason = "import" if not imports else detail
        return {**row, "valid": False, "recovered": False, "reason": reason}
    return {
        **row,
        "valid": True,
        "recovered": recovered,
        "reason": None if recovered else detail,
    }


# Enumeration and the seeded draw -----------------------------------------------------


def enumerate_all(w: World, tally: Counter[str]) -> list[Candidate]:
    every = _python_files(w.clone)
    index = _Index.build(every)
    found: list[Candidate] = []
    for hook in w.hooks:
        found += relocations(w.repo, hook)
    for path in sorted(w.repo.files):
        text = w.repo.text(path)
        tree = parse(text)
        if tree is None:
            tally["parse_failures"] += 1
            continue
        found += _schema(w, path, text, tally)
        found += drop_calls(w.repo, index, path)
        found += mechanisms(w.repo, path)
        found += forms(w.repo, path)
    found += add_edges(w.repo, w.modules, w.graph, w.reach)
    return found


def _schema(w: World, path: str, text: str, tally: Counter[str]) -> list[Candidate]:
    if not has_ddl(text):
        return []
    tally["ddl_modules"] += 1
    schema = probe(w.clone, w.env, w.modules[path], path)
    if schema is None:
        tally["ddl_modules_unimportable"] += 1
        return []
    w.schemas[path] = schema
    tally["ddl_tables"] += len(schema.counts)
    tally["ddl_checks"] += sum(schema.counts.values())
    tally["ddl_scripts_failed"] += schema.failed
    tally["check_sites"] += len(schema.sites)
    tally["check_sites_in_source"] += sum(1 for _, pos, _ in schema.sites if pos >= 0)
    return drop_checks(path, w.repo.files[path], schema)


def _with_prior(c: Candidate, on_path: set[tuple[str, str]]) -> Candidate:
    if c.level != "L2":
        return c
    pair = (c.extra["caller"], c.extra["callee"])
    prior = "fault" if pair in on_path else "decoy"
    return replace(c, intent="unknown", extra={**c.extra, "prior": prior})


def flow_intents(w: World, found: list[Candidate]) -> list[Candidate]:
    """L2 candidates get the a-priori guess (the component pair is a base call edge)
    as ``prior`` only; their label comes from X2 after the mutation."""
    if w.scenarios is None:
        return [
            replace(c, intent="unverifiable") if c.level == "L2" else c for c in found
        ]
    first = flow.traces(w.clone, w.cmap, w.scenarios, None)
    second = flow.traces(w.clone, w.cmap, w.scenarios, None)
    w.base_traces = first
    w.stable = {s for s in first if first[s] == second[s]}
    on_path = {e for s in w.stable for e in first[s].edges}
    return [_with_prior(c, on_path) for c in found]


def draw(found: list[Candidate], n: int, rng: random.Random) -> list[Candidate]:
    """Up to n candidates, round robin over operators, each operator's order seeded."""
    by_op: dict[str, list[Candidate]] = {}
    for c in found:
        by_op.setdefault(c.op, []).append(c)
    pools = [rng.sample(v, len(v)) for _, v in sorted(by_op.items())]
    out: list[Candidate] = []
    while len(out) < n and any(pools):
        for pool in pools:
            if pool and len(out) < n:
                out.append(pool.pop())
    return out


# Verdicts ----------------------------------------------------------------------------


def _side(cands: list[Candidate], rows: list[Row]) -> dict[str, Any]:
    known = [r for r in rows if r["valid"] is not None]
    valid = [r for r in known if r["valid"]]
    share = len(valid) / len(known) if known else None
    items = len({c.key for c in cands})
    return {
        "candidates": len(cands),
        "items": items,
        "components": len({c.component for c in cands}),
        "by_op": dict(Counter(c.op for c in cands)),
        "sampled": len(rows),
        "import_unknown": len(rows) - len(known),
        "valid": len(valid),
        "recovered": sum(1 for r in valid if r["recovered"]),
        "valid_share": share,
        "est_supply": None if share is None else round(items * share, 1),
        "invalid_reasons": dict(
            Counter(str(r["reason"]) for r in known if not r["valid"])
        ),
        "templates": sorted({c.card.get("template", "") for c in cands}),
    }


def verdict(level: str, sides: dict[str, dict[str, Any]], checkable: bool) -> str:
    needs = ("fault",) if level == "L1c" else INTENTS
    if not checkable:
        return "UNDETERMINED"
    if any(sides[i]["candidates"] == 0 for i in needs):
        return "NOT_SUPPLIED"
    if any(sides[i]["valid"] > sides[i]["recovered"] for i in needs):
        return "FAIL_B"
    shares = [sides[i]["valid_share"] for i in needs]
    if any(s is None for s in shares):
        return "UNDETERMINED"
    if any(s < MIN_VALID for s in shares if s is not None):
        return "FAIL_A"
    supplies = [sides[i]["est_supply"] or 0 for i in needs]
    return "SUPPLIED" if min(supplies) >= MIN_SUPPLY else "PASS_NOT_SUPPLIED"


def _level(
    w: World, level: str, found: list[Candidate], rows: list[Row]
) -> dict[str, Any]:
    mine = [c for c in found if c.level == level]
    drawn = [r for r in rows if r["level"] == level]
    sides = {
        i: _side(
            [c for c in mine if c.intent in (i, "unknown")],
            [r for r in drawn if r["intent"] == i],
        )
        for i in INTENTS
    }
    if level == "L2":
        _l2_supply(sides, drawn)
    checkable = level != "L2" or w.scenarios is not None
    return {**sides, "verdict": verdict(level, sides, checkable)}


def summarize(w: World, found: list[Candidate], rows: list[Row]) -> dict[str, Any]:
    out = {level: _level(w, level, found, rows) for level in LEVELS}
    l2 = [c for c in found if c.level == "L2"]
    drawn = [r for r in rows if r["level"] == "L2" and r["valid"]]
    out["L2"].update(
        candidates=len(l2),
        items=len({c.key for c in l2}),
        prior_fault_drawn=sum(1 for r in drawn if r.get("prior") == "fault"),
        prior_agrees=sum(1 for r in drawn if r.get("prior") == r["intent"]),
    )
    l3 = [r for r in rows if r["level"] == "L3"]
    out["L3"]["module_level_imports"] = {
        i: dict(
            Counter(str(r.get("module_level_imports")) for r in l3 if r["intent"] == i)
        )
        for i in INTENTS
    }
    return out


def _l2_supply(sides: dict[str, dict[str, Any]], drawn: list[Row]) -> None:
    """L2 labels are known only after the mutation, so a label's supply is the
    candidate items times that label's share of the items drawn with a known import,
    and the valid share is over all of them."""
    known = [r for r in drawn if r["valid"] is not None]
    if not known:
        return
    valid = [r for r in known if r["valid"]]
    for i, side in sides.items():
        share = sum(1 for r in valid if r["intent"] == i) / len(known)
        side["est_supply"] = round(side["items"] * share, 1)
        side["valid_share"] = len(valid) / len(known)


# The run -----------------------------------------------------------------------------


def draw_all(w: World, found: list[Candidate], n: int, rng: random.Random) -> list[Row]:
    """N faults and N decoys per level; for L2, 2N items whose label X2 gives."""
    rows: list[Row] = []
    for level in LEVELS:
        intents = ("unknown",) if level == "L2" else INTENTS
        size = 2 * n if level == "L2" else n
        for intent in intents:
            pool = [c for c in found if c.level == level and c.intent == intent]
            rows += [evaluate(w, c) for c in draw(pool, size, rng)]
    return rows


def run_repo(spec: str, n: int, seed: int, home: Path) -> dict[str, Any]:
    path, _, scen = spec.partition("=")
    scenarios = flow.load(Path(scen)) if scen else None
    clone = scratch_clone(Path(path).resolve(), home)
    started = time.perf_counter()
    try:
        _git(clone, "config", "core.hooksPath", "/dev/null")
        w = build(clone, scenarios, home)
        tally: Counter[str] = Counter()
        _, hook_tally = read_hooks(clone)
        found = flow_intents(w, enumerate_all(w, tally))
        rows = draw_all(w, found, n, random.Random(seed))
        result = {
            "repo": Path(path).name,
            "head": _git(clone, "rev-parse", "HEAD").strip(),
            "map_version": w.cmap.version,
            "product_components": len(comp.product_components(w.cmap)),
            "product_files": len(w.repo.files),
            "x1_edges": len(w.graph),
            "hooks": dict(hook_tally),
            "contract": dict(contract_counts(clone)),
            "tally": dict(tally),
            "scenarios": _scenario_summary(w),
            "levels": summarize(w, found, rows),
            "rows": rows,
        }
    finally:
        shutil.rmtree(clone, ignore_errors=True)
    result["seconds"] = round(time.perf_counter() - started, 1)
    return result


def _scenario_summary(w: World) -> dict[str, Any] | None:
    if w.scenarios is None:
        return None
    return {
        s: {
            "components": len(shape.components),
            "edges": len(shape.edges),
            "exit_code": shape.exit_code,
            "outcome": shape.outcome,
            "deterministic": s in w.stable,
        }
        for s, shape in w.base_traces.items()
    }


def _print(result: dict[str, Any]) -> None:
    print(
        f"\n{result['repo']} {result['head'][:7]}: {result['product_components']} "
        f"components, {result['product_files']} files, {result['seconds']} s"
    )
    for level, data in result["levels"].items():
        for intent in INTENTS:
            s = data[intent]
            share = "-" if s["valid_share"] is None else f"{s['valid_share']:.2f}"
            print(
                f"  {level:<4}{intent:<6} cand {s['candidates']:>6}"
                f" items {s['items']:>6}"
                f" drawn {s['sampled']:>3} unk {s['import_unknown']:>2} valid"
                f" {s['valid']:>3} rec {s['recovered']:>3} share {share:>4}"
                f" est {s['est_supply']}  {s['invalid_reasons']}"
            )
        print(f"  {level:<4}-> {data['verdict']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0] if __doc__ else ""
    )
    parser.add_argument("repos", nargs="+", help="REPO or REPO=SCENARIOS.toml")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261025)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    home = Path(tempfile.mkdtemp(prefix="e14-home-"))
    bindir = home / "bin"
    flow.wrapper(bindir)
    flow.environment(bindir)
    os.environ["PRAXIS_HOME"] = str(home / "praxis-home")
    try:
        results = [run_repo(spec, args.n, args.seed, home) for spec in args.repos]
    finally:
        shutil.rmtree(home, ignore_errors=True)
    for result in results:
        _print(result)
    if args.out is not None:
        args.out.write_text(
            json.dumps({"n": args.n, "seed": args.seed, "results": results}, indent=1)
        )
    failed = any(
        d["verdict"] == "FAIL_B" for r in results for d in r["levels"].values()
    )
    print("\nE14:", "FAIL (a generator defect, see FAIL_B)" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
