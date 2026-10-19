"""Component maps derived from real git repositories, as `praxis make components`
sees them: `git ls-files` in a temporary repository, never a hand-built path list,
except where a test names the list on purpose. Expected values are written before each
run.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from praxis import components as c

pytestmark = pytest.mark.integration

AROUND = {
    "tests/test_a.py": "",
    "docs/guide.md": "",
    "README.md": "",
    "pyproject.toml": "",
    ".gitignore": "",
    "uv.lock": "",
    ".github/workflows/ci.yml": "",
    "scripts/run.sh": "",
}


def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def test_src_layout_depth_one_with_fixed_classes(tmp_path: Path) -> None:
    files = {f"src/app/{m}.py": "" for m in "abcde"}
    root = repo(tmp_path, {**files, "src/app/__init__.py": "X = 1\n", **AROUND})
    cmap = c.derive(root, "r1")
    assert cmap.source == "mechanical"
    assert cmap.warnings == ()
    assert [r.id for r in cmap.rules[:3]] == ["tests", "docs", "config"]
    assert c.product_components(cmap) == [".", "a", "b", "c", "d", "e"]
    expected = {
        "src/app/a.py": "a",
        "src/app/__init__.py": ".",
        "tests/test_a.py": "tests",
        "docs/guide.md": "docs",
        "README.md": "docs",
        "pyproject.toml": "config",
        ".gitignore": "config",
        "uv.lock": "config",
        ".github/workflows/ci.yml": "config",
        "scripts/run.sh": "unmapped",
        "src/app/new.py": "unmapped",
    }
    assert {p: c.assign(cmap, p) for p in expected} == expected


def test_flat_package_empty_init_is_not_a_component(tmp_path: Path) -> None:
    files = {f"pkg/m{i}.py": "" for i in range(1, 6)}
    root = repo(tmp_path, {**files, "pkg/__init__.py": "", "pkg/tests/test_m.py": ""})
    cmap = c.derive(root, "r2")
    assert c.product_components(cmap) == ["m1", "m2", "m3", "m4", "m5"]
    assert c.assign(cmap, "pkg/__init__.py") == "unmapped"
    assert c.assign(cmap, "pkg/tests/test_m.py") == "tests"


def test_depth_two_package(tmp_path: Path) -> None:
    root = repo(
        tmp_path,
        {
            "src/big/__init__.py": "X = 1\n",
            "src/big/cli.py": "",
            "src/big/core/__init__.py": "",
            "src/big/core/a.py": "",
            "src/big/core/b.py": "",
            "src/big/io/__init__.py": "Y = 1\n",
            "src/big/io/r.py": "",
            "src/big/io/fmt/x.py": "",
            "src/big/io/fmt/y.py": "",
        },
    )
    cmap = c.derive(root, "r3")
    assert c.product_components(cmap) == [
        ".",
        "cli",
        "core/a",
        "core/b",
        "io/.",
        "io/fmt",
        "io/r",
    ]
    expected = {
        "src/big/io/__init__.py": "io/.",
        "src/big/core/__init__.py": "unmapped",
        "src/big/core/new.py": "unmapped",
        "src/big/io/fmt/z.py": "io/fmt",
        "src/big/cli.py": "cli",
    }
    assert {p: c.assign(cmap, p) for p in expected} == expected


def test_polyglot_repo_root_and_id_collisions(tmp_path: Path) -> None:
    root = repo(
        tmp_path,
        {
            "web/index.ts": "",
            "web/app.ts": "",
            "server/main.go": "",
            "lib/util.py": "",
            "Makefile": "",
            "LICENSE": "",
            "config.py": "",
            "tools.py": "",
            "tools/gen.py": "",
            "README.md": "",
        },
    )
    cmap = c.derive(root, "r4")
    assert c.product_components(cmap) == [
        "LICENSE",
        "Makefile",
        "config.py",
        "lib",
        "server",
        "tools",
        "tools.py",
        "web",
    ]
    expected = {
        "config.py": "config.py",
        "tools.py": "tools.py",
        "tools/gen.py": "tools",
        "server/main.go": "server",
        "README.md": "docs",
    }
    assert {p: c.assign(cmap, p) for p in expected} == expected


def test_owner_toml_overrides_and_refusals(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert c.load_owner(home, "r5") is None
    (home / "components").mkdir(parents=True)
    path = home / "components" / "r5.toml"
    path.write_text(
        '[[component]]\nid = "core"\nglobs = ["src/app/a.py", "src/app/b.py"]\n'
        '[[component]]\nid = "edge"\nglobs = ["src/app/*"]\n'
    )
    cmap = c.load_owner(home, "r5")
    assert cmap is not None
    assert cmap.source == "owner"
    assert c.product_components(cmap) == ["core", "edge"]
    expected = {
        "src/app/a.py": "core",
        "src/app/c.py": "edge",
        "tests/test_a.py": "tests",
        "lib/x.py": "unmapped",
    }
    assert {p: c.assign(cmap, p) for p in expected} == expected
    one = '[[component]]\nid = "a"\nglobs = ["x"]\n'
    refused = {
        one + one: "ids repeated",
        '[[component]]\nid = "tests"\nglobs = ["x"]\n': "id must be",
        '[[component]]\nid = "a"\nglob = ["x"]\n': "exactly `id` and `globs`",
        '[[component]]\nid = "a"\nglobs = []\n': "non-empty list",
        'name = "x"\n': "nothing else",
    }
    for text, reason in refused.items():
        path.write_text(text)
        with pytest.raises(ValueError, match=reason):
            c.load_owner(home, "r5")


def test_too_few_components_refuses(tmp_path: Path) -> None:
    root = repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "", "pkg/b.py": ""})
    with pytest.raises(c.TooFewComponents):
        c.derive(root, "r6")
    with pytest.raises(c.TooFewComponents):
        c.derive(root, "r6", tracked=["README.md", "tests/test_x.py"])


def test_more_than_thirty_reduces_depth_with_warning(tmp_path: Path) -> None:
    files = {f"src/w/{d}/m{i}.py": "" for d in "pq" for i in range(16)}
    root = repo(tmp_path, {**files, "src/w/__init__.py": ""})
    cmap = c.derive(root, "r7")
    assert cmap.warnings == ("depth_reduced_from_2_to_1",)
    assert c.product_components(cmap) == ["p", "q"]


def test_version_and_json_round_trip(tmp_path: Path) -> None:
    files = {f"src/app/{m}.py": "" for m in "abcde"}
    cmap = c.derive(tmp_path, "r8", tracked=list(files))
    canon = json.dumps([[r.id, *r.globs] for r in cmap.rules], separators=(",", ":"))
    assert cmap.version == hashlib.sha256(canon.encode()).hexdigest()[:16]
    assert c.from_json(c.to_json(cmap)) == cmap
    doc = json.loads(c.to_json(cmap))
    doc["rules"][-1][1] = "src/app/z.py"
    with pytest.raises(ValueError, match="does not match"):
        c.from_json(json.dumps(doc))


# The flat-package rule (design 2.2 step 5): 31 children at depth 1, so the cut would
# fall to depth 0. Expected groups, from the files below: page gains render (two edges
# to page against one to store) and then leaf (whose only edge is to render, so it
# moves in the second round); cli, a directory, gains __main__; config.py, config_a and
# config_b share the reserved token config; hub has one edge to page and one to store,
# a tie, so it stays alone; ".", data.json and the 16 fillers have no edge to a group.
FILLERS = [f"f{i:02}" for i in range(15)] + ['q"t']
FLAT = {
    "src/flat/__init__.py": "X = 1\n",
    "src/flat/__main__.py": "from flat.cli import main\n",
    "src/flat/cli/__init__.py": "",
    "src/flat/cli/main.py": "",
    "src/flat/cli/run.py": "from flat.cli import main\n",
    "src/flat/page.py": "",
    "src/flat/page_a.py": "",
    "src/flat/page_b.py": "from flat import page\n",
    "src/flat/store_x.py": "",
    "src/flat/store_y.py": "from flat import store_x\n",
    "src/flat/config.py": "",
    "src/flat/config_a.py": "",
    "src/flat/config_b.py": "",
    "src/flat/render.py": "from flat import page_a, page_b, store_x, leaf\n",
    "src/flat/leaf.py": "",
    "src/flat/hub.py": "from flat import store_x, page\n",
    "src/flat/data.json": "{}\n",
    **{f"src/flat/{name}.py": "" for name in FILLERS},
}
PAGE = tuple(f"src/flat/{m}.py" for m in ("leaf", "page", "page_a", "page_b", "render"))
CONFIG = tuple(f"src/flat/{m}.py" for m in ("config", "config_a", "config_b"))
FLAT_RULES = [
    c.Rule(".", ("src/flat/__init__.py",)),
    c.Rule("cli", ("src/flat/__main__.py", "src/flat/cli/*")),
    c.Rule("config*", CONFIG),
    c.Rule("data.json", ("src/flat/data.json",)),
    *(c.Rule(name, (f"src/flat/{name}.py",)) for name in FILLERS[:-1]),
    c.Rule("hub", ("src/flat/hub.py",)),
    c.Rule("page", PAGE),
    c.Rule('q"t', ('src/flat/q"t.py',)),
    c.Rule("store", ("src/flat/store_x.py", "src/flat/store_y.py")),
]


def test_flat_package_groups_by_prefix_then_imports(tmp_path: Path) -> None:
    root = repo(tmp_path, {**FLAT, **AROUND})
    cmap = c.derive(root, "r9")
    assert (cmap.source, cmap.warnings) == ("mechanical", (c.GROUPED,))
    assert cmap.rules == c.FIXED + tuple(FLAT_RULES)
    expected = {
        "src/flat/render.py": "page",
        "src/flat/leaf.py": "page",
        "src/flat/hub.py": "hub",
        "src/flat/__main__.py": "cli",
        "src/flat/cli/new.py": "cli",
        "src/flat/config.py": "config*",
        "src/flat/page_c.py": "unmapped",
        "src/flat/cli/__init__.py": "cli",
        "tests/test_a.py": "tests",
    }
    assert {p: c.assign(cmap, p) for p in expected} == expected

    # Thirty-one children with no prefix in common and no import edge stay 31.
    tmp = tmp_path / "wide"
    tmp.mkdir()
    files = {f"src/w/m{i:02}.py": "" for i in range(31)}
    wide = repo(tmp, {**files, "src/w/__init__.py": ""})
    above = c.derive(wide, "r10")
    assert above.warnings == (c.GROUPED, c.ABOVE)
    assert len(c.product_components(above)) == 31


def test_to_toml_round_trips_through_load_owner(tmp_path: Path) -> None:
    root = repo(tmp_path, {**FLAT, **AROUND})
    cmap = c.derive(root, "r11")
    text = c.to_toml(cmap)
    assert text.splitlines()[:2] == [
        f"# praxis component map {cmap.version} (mechanical), r11",
        f"# warnings: {c.GROUPED}",
    ]
    assert 'id = "q\\"t"' in text.splitlines()
    home = tmp_path / "home"
    c.owner_path(home, "r11").parent.mkdir(parents=True)
    c.owner_path(home, "r11").write_text(text)
    owner = c.load_owner(home, "r11")
    assert owner is not None
    assert (owner.source, owner.rules) == ("owner", cmap.rules)
    assert owner.version == cmap.version


def test_praxis_itself_still_cuts_at_depth_one() -> None:
    root = Path(__file__).resolve().parents[2]
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "src/praxis"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    modules = [p for p in listed if p.count("/") == 2 and (root / p).stat().st_size]
    cmap = c.derive(root, "r12")
    assert cmap.warnings == ()
    assert len(c.product_components(cmap)) == len(modules)
    assert {c.assign(cmap, p) for p in modules} == set(c.product_components(cmap))
