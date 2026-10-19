"""Component maps derived from real git repositories, as `praxis components derive` sees
them: `git ls-files` in a temporary repository, never a hand-built path list, except
where a test names the list on purpose. Expected values are written before each run.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from praxis import components as c

if TYPE_CHECKING:
    from pathlib import Path

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
