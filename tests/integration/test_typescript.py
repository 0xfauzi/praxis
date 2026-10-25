"""X1 for TypeScript and JavaScript (x1-ts-v1), driven through the installed `praxis`
binary on a small real repository built here: `praxis set components` reads it, and the
Stop hook records its file edges, with recorded Claude Code 2.1.303 payloads. Expected
edges, components and lines are written from the planted files before each run.

The planted repository has no Python package root, so the mechanical map cuts the whole
tree at depth 2 (depth 1 gives four components): eleven product components, eight of
them holding a file a registered language parses. Its file edges, by hand:

  src/app.tsx    -> packages/money/src/index.ts  workspace `exports`, `types` condition
  src/app.tsx    -> src/util/fmt.ts              tsconfig `paths` alias
  src/app.tsx    -> src/cart/total.js            require() in an arrow body: deferred
  src/cart/index.ts -> src/types.ts              `import type`: type-only, deferred
  src/cart/total.js -> src/util/fmt.ts           `.js` substituted by `.ts`
  src/cart/total.js -> src/db/store.ts           require() in a function body: deferred
  src/db/store.ts   -> src/util/fmt.ts           import(): deferred

`./styles.css` is an asset, not an edge; `require(name)` in src/db/store.ts and a
relative import of a missing file in src/missing.ts are blind spots; src/util holds a
Kotlin file, a language no reader covers, so it is partial.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from praxis.components import derive, product_components
from praxis.lang import Unparsed
from praxis.lang.typescript import LANGUAGE as TS

pytestmark = pytest.mark.integration

PAYLOADS = Path(__file__).resolve().parents[2] / "fixtures" / "claude" / "2.1.303"
ALIAS = "CANARYALIAS7f3"
CODE = "CANARYCODE9c1"
MISSING = "CANARYMISS2b8"
FILES = {
    "package.json": '{"name": "shop", "private": true, "workspaces": ["packages/*"]}\n',
    "tsconfig.json": (
        '{\n  // JSONC, with a trailing comma\n  "compilerOptions": {'
        '"jsx": "react-jsx", "allowJs": true,'
        f' "paths": {{"@{ALIAS}/*": ["./src/util/*"]}},}},\n}}\n'
    ),
    "packages/money/package.json": (
        '{"name": "@shop/money", "exports": {".": {"types": "./src/index.ts",'
        ' "default": "./dist/index.js"}}}\n'
    ),
    "packages/money/src/index.ts": "export const cents = (n: number) => n * 100;\n",
    "src/app.tsx": (
        'import { cents } from "@shop/money";\n'
        f'import {{ fmt }} from "@{ALIAS}/fmt";\n'
        'import "./styles.css";\n'
        "export const App = () => (\n"
        '  <div title="a > b">{fmt(cents(1))} {require("./cart/total")}</div>\n'
        ");\n"
    ),
    "src/styles.css": "div { color: red; }\n",
    "src/cart/index.ts": (
        'export * from "./total";\nimport type { Item } from "../types";\n'
        "export const first = (items: Item[]) => items[0];\n"
    ),
    "src/cart/total.js": (
        'const { fmt } = require("../util/fmt.js");\n'
        "module.exports = function total() {\n"
        '  return fmt(require("../db/store").get());\n'
        "};\n"
    ),
    "src/db/store.ts": (
        'export function get() { return import("../util/fmt"); }\n'
        'const name = "x";\nexport const dyn = require(name);\n'
    ),
    "src/util/fmt.ts": (
        f'const SECRET = "{CODE}";\n'
        "export const fmt = (n: number) => `${n}`;\nexport const s = SECRET;\n"
    ),
    "src/util/legacy.kt": "fun main() {}\n",
    "src/types.ts": "export interface Item { id: string }\n",
    "src/missing.ts": f'import x from "./{MISSING}";\nexport default x;\n',
    "src/gen.py": "import os\n",
}
PRODUCT = [
    "package.json", "packages/money", "src/app.tsx", "src/cart", "src/db", "src/gen",
    "src/missing.ts", "src/styles.css", "src/types.ts", "src/util", "tsconfig.json",
]  # fmt: skip
# (src, dst): (sites, module_level), at component grain.
EDGES = {
    ("src/app.tsx", "packages/money"): (1, 1),
    ("src/app.tsx", "src/util"): (1, 1),
    ("src/app.tsx", "src/cart"): (1, 0),
    ("src/cart", "src/types.ts"): (1, 0),
    ("src/cart", "src/util"): (1, 1),
    ("src/cart", "src/db"): (1, 0),
    ("src/db", "src/util"): (1, 0),
}
PARSED = [
    "packages/money", "src/app.tsx", "src/cart", "src/db", "src/gen",
    "src/missing.ts", "src/types.ts", "src/util",
]  # fmt: skip


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


class Repo:
    """The planted repository, a HOME, a $PRAXIS_HOME and the binary's two entries."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = (tmp_path / "repo").resolve()
        self.home = tmp_path / "home"
        self.praxis_home = tmp_path / "praxis-home"
        self.session_id = str(uuid.uuid4())
        self.home.mkdir()
        for rel, text in FILES.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "plant")
        found = shutil.which("praxis")
        assert found is not None, "no `praxis` on PATH: run `uv sync` first"
        self.binary = found

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PRAXIS_HOME": str(self.praxis_home),
            "PATH": os.environ.get("PATH", ""),
            **extra,
        }

    def praxis(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=self.env(),
        )

    def hook(self, name: str, **extra: str) -> subprocess.CompletedProcess[str]:
        text = (PAYLOADS / f"{name}.json").read_text()
        text = text.replace("<repo>", str(self.root)).replace("<home>", str(self.home))
        body: dict[str, Any] = json.loads(text)
        body["session_id"] = self.session_id
        done = subprocess.run(
            [self.binary, "hook"],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=self.env(**extra),
        )
        assert done.returncode == 0, done.stderr
        return done

    def edit(self, appended: dict[str, str], **extra: str) -> None:
        """One recorded Bash call that appends to files: the recorder sees pre and
        post only, as for any tool."""
        self.hook("pre_tool_use_bash_commit", **extra)
        for rel, text in appended.items():
            with (self.root / rel).open("a") as handle:
                handle.write(text)
        self.hook("post_tool_use_bash_commit", **extra)

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        db = sqlite3.connect(f"file:{self.praxis_home / 'ledger.db'}?mode=ro", uri=True)
        try:
            return [tuple(r) for r in db.execute(sql)]
        finally:
            db.close()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_set_components_reads_typescript(repo: Repo) -> None:
    assert product_components(derive(repo.root, "planted")) == PRODUCT
    done = repo.praxis("set", "components")
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].endswith("(mechanical): 11 product components"), lines
    assert lines[1:13] == [
        "X1 x1-ts-v1+x1-v2: imports in 7 of 56 ordered pairs of components,"
        " base rate 0.125",
        "  pairs with no direct import: 2 connect through a different component,"
        " 47 have no path",
        "  pairs with no item, because a component has no parsed source file: 54",
        "  components with partial coverage (a blind spot or a parse failure):"
        " src/db src/missing.ts",
        "  components with partial coverage (a file in an unmapped language): src/util",
        "Product files by language: 14",
        "  typescript (x1-ts-v1): 8 (57.1%)",
        "  non code: 4 (28.6%)",
        "  python (x1-v2): 1 (7.1%)",
        "  unmapped language: 1 (7.1%)",
        "D1 items: 56 written at epoch 0, 0 already present",
        lines[12],
    ]
    items = repo.rows(
        "SELECT src_component, dst_component, truth, sites, module_level,"
        " extractor_version FROM probe_items"
    )
    assert {(a, b) for a, b, *_ in items} == {
        (a, b) for a in PARSED for b in PARSED if a != b
    }
    for a, b, truth, sites, module_level, version in items:
        assert version == "x1-ts-v1+x1-v2"
        assert truth == ((a, b) in EDGES), (a, b)
        assert (sites, module_level) == EDGES.get((a, b), (None, None)), (a, b)


def test_stop_records_file_edges_per_language(repo: Repo) -> None:
    assert repo.praxis("set", "components").returncode == 0
    repo.hook("session_start")
    repo.edit(
        {
            "src/db/store.ts": 'import type { Item } from "../types";\n',
            "src/gen.py": "import sys\n",
        }
    )
    repo.hook("stop")
    got = repo.rows(
        "SELECT src_file, dst_component, sites, module_level, extractor_version"
        " FROM file_edges ORDER BY src_file, dst_component"
    )
    assert got == [
        ("src/db/store.ts", "src/types.ts", 1, 0, "x1-ts-v1"),
        ("src/db/store.ts", "src/util", 1, 0, "x1-ts-v1"),
        ("src/gen.py", None, None, None, "x1-v2"),
    ]


def test_no_specifier_or_code_text_reaches_praxis_home(repo: Repo) -> None:
    printed = repo.praxis("set", "components").stdout
    printed += repo.praxis("show", "components").stdout
    repo.hook("session_start")
    repo.edit({"src/app.tsx": f'import {{ s }} from "@{ALIAS}/fmt";\n'})
    printed += repo.hook("stop").stdout
    stored = b"".join(
        p.read_bytes() for p in sorted(repo.praxis_home.rglob("*")) if p.is_file()
    )
    assert stored, "nothing was stored, so nothing was checked"
    for canary in (ALIAS, CODE, MISSING):
        assert canary.encode() not in stored, canary
        assert canary not in printed, canary


def test_a_python_only_stop_never_imports_the_typescript_reader(repo: Repo) -> None:
    assert repo.praxis("set", "components").returncode == 0
    repo.hook("session_start")
    timed = {"PYTHONPROFILEIMPORTTIME": "1"}
    repo.edit({"src/gen.py": "import sys\n"}, **timed)
    python_only = repo.hook("stop", **timed).stderr
    repo.edit({"src/types.ts": "export type Id = string;\n"}, **timed)
    with_ts = repo.hook("stop", **timed).stderr
    assert "praxis.lang.python" in python_only
    assert "praxis.lang.t" not in python_only
    assert "praxis.lang.typescript" in with_ts


SITES = """\
#!/usr/bin/env node
/// <reference path="./types.d.ts" />
import type { A } from "./a";
import b, { type C } from "./b";
import "./side";
export * as ns from "./c";
export type { D } from "./d";
import e = require("./e");
const f = require("./f");
const re = /require("./not")/g, half = 1 / 2;
const tpl = `import x from "./not" ${require("./g")}`;
// require("./not")
/** @type {import("./h").H} */
let t: import("./i").I;
function load() { return require("./j"); }
const lazy = () => import("./k");
const dyn = require(f + "x");
const url = new URL("./l.js", import.meta.url);
class K { require(x: string) { return x; } }
"""
# (kind, specifier, deferred, type_only, blind), in source order.
EXPECTED = [
    ("reference_path", "./types.d.ts", True, True, None),
    ("import_type", "./a", True, True, None),
    ("import", "./b", False, False, None),
    ("side_effect", "./side", False, False, None),
    ("export_from", "./c", False, False, None),
    ("export_type_from", "./d", True, True, None),
    ("import_equals", "./e", False, False, None),
    ("require", "./f", False, False, None),
    ("require", "./g", False, False, None),
    ("jsdoc", None, True, True, "jsdoc"),
    ("import_type_node", "./i", True, True, None),
    ("require", "./j", True, False, None),
    ("dynamic_import", "./k", True, False, None),
    ("require", None, False, False, "dynamic"),
    ("resolve", None, False, False, "dynamic"),
]


def test_the_reader_sees_each_import_form_once() -> None:
    sites = TS.imports("src/x.ts", SITES.encode())
    got = [(s.kind, s.specifier, s.deferred, s.type_only, s.blind) for s in sites]
    assert got == EXPECTED
    assert all(s.start < s.end for s in sites)
    assert len({(s.start, s.end) for s in sites}) == len(sites)


def test_the_reader_refuses_a_bundle_as_a_parse_failure() -> None:
    with pytest.raises(Unparsed, match="too_large"):
        TS.imports("dist/bundle.js", b"x" * (512 * 1024 + 1))
