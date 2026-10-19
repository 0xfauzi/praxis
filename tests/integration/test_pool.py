"""`praxis components freeze`, `praxis components` and the pool counts, driven through
pool.py against a real git repository and a real ledger under a temporary $PRAXIS_HOME.
The CLI verbs land in stage C, so these call the module with the stdout the verbs will
pass. Every expected figure is written here, from the files planted, before the run.

The planted package `app` (depth 1 under src/) has seven product components: `.` (a
non-empty __init__.py), a, b, c, d, data.json and the directory sub. data.json holds no
Python, so its 12 ordered pairs are not generated; d's only import is under
TYPE_CHECKING (a blind spot) and sub holds a file that does not parse, so the 18 items
touching d or sub are partial and the other 12 are full.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from praxis import pool
from praxis.components import assign, derive, product_components
from praxis.store import Store
from praxis.unassisted import locate

pytestmark = pytest.mark.integration

NOW = "2026-01-05T09:00:00.000Z"
LATER = "2026-01-12T09:00:00.000Z"
CANARY = "CANARYPOOLc0de5"
CANARY_REASON = "CANARYREASONf00d"
FILES = {
    "src/app/__init__.py": "from app import a\n",
    "src/app/a.py": "from app import b\nfrom app.c import f\n",
    "src/app/b.py": "def g():\n    from app import c\n    import app.c\n    return c\n",
    "src/app/c.py": f"SECRET = {CANARY!r}\n\n\ndef f():\n    return SECRET\n",
    "src/app/d.py": (
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from app import a\n"
    ),
    "src/app/data.json": "{}\n",
    "src/app/sub/__init__.py": "",
    "src/app/sub/ok.py": "from app import c\n",
    "src/app/sub/bad.py": "def (:\n",
    "tests/test_a.py": "from app import a\n",
    "README.md": "app\n",
    "pyproject.toml": "[project]\nname = 'app'\n",
    "scripts/run.sh": "true\n",
}
PRODUCT = [".", "a", "b", "c", "d", "data.json", "sub"]
PARSED = [".", "a", "b", "c", "d", "sub"]
# (src, dst): (sites, module_level). b imports c twice inside a function.
EDGES = {
    (".", "a"): (1, True),
    ("a", "b"): (1, True),
    ("a", "c"): (1, True),
    ("b", "c"): (2, False),
    ("d", "a"): (1, False),
    ("sub", "c"): (1, True),
}
PARTIAL = {"d": "blind_spots:d:1", "sub": "parse_failures:sub:1"}


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def plant(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "plant")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    plant(root, FILES)
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "praxis-home"


def run_freeze(repo: Path, home: Path, reason: str | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = pool.freeze(repo, home, out, NOW, reason=reason)
    return code, out.getvalue()


def rows(home: Path, sql: str, *params: object) -> list[dict[str, object]]:
    ledger = Store(home / "ledger.db").open()
    try:
        return [dict(r) for r in ledger.db.execute(sql, params)]
    finally:
        ledger.close()


def git_state(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in (root / ".git").rglob("*")
        if p.is_file()
    }


def home_bytes(home: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file())


def test_freeze_writes_the_d1_pool(repo: Path, home: Path) -> None:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    version = derive(repo, repo_id).version
    assert product_components(derive(repo, repo_id)) == PRODUCT
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    before = git_state(repo)

    code, out = run_freeze(repo, home)

    assert code == 0, out
    assert out.splitlines() == [
        f"map {version} frozen (mechanical), 7 product components",
        "X1 x1-v1: 6 edges among 30 ordered pairs, base rate 0.200",
        "  non-edge pairs: 4 reach through another component, 20 no path",
        "  pairs not generated (a component with no parsed Python file): 12",
        "  partial (blind spot or parse failure): d sub",
        "D1 items: 30 written at epoch 0, 0 already present",
        "pool: 30 items (6 true), 30 unexposed, 7 tasks at 4 items per task",
    ]
    assert git_state(repo) == before, "freeze wrote under the owner's .git"
    maps = rows(home, "SELECT version, source, frozen_ts FROM component_maps")
    assert maps == [{"version": version, "source": "mechanical", "frozen_ts": NOW}]
    items = rows(
        home, "SELECT * FROM probe_items ORDER BY src_component, dst_component"
    )
    assert len(items) == 30
    pairs = {(str(r["src_component"]), str(r["dst_component"])) for r in items}
    assert pairs == {(a, b) for a in PARSED for b in PARSED if a != b}
    for r in items:
        key = (str(r["src_component"]), str(r["dst_component"]))
        assert (r["relation"], r["epoch"], r["truth_ts"]) == ("D1", 0, NOW)
        assert (r["claim_class"], r["map_version"]) == ("derived", version)
        assert r["truth"] == (key in EDGES)
        sites = EDGES.get(key, (None, None))
        assert (r["sites"], r["module_level"]) == sites
        want = sorted(PARTIAL[c] for c in key if c in PARTIAL)
        assert json.loads(str(r["warnings"])) == want
        assert r["coverage"] == ("partial" if want else "full")
        assert json.loads(str(r["sources"])) == [
            "extractor:x1-v1",
            f"map:{version}",
            f"tree:{tree}",
        ]
    assert CANARY.encode() not in home_bytes(home)


def test_refreeze_adds_nothing_and_a_change_needs_a_reason_code(
    repo: Path, home: Path
) -> None:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    old = derive(repo, repo_id).version
    assert run_freeze(repo, home)[0] == 0

    code, out = run_freeze(repo, home)
    assert code == 0
    assert "D1 items: 0 written at epoch 0, 30 already present" in out.splitlines()
    assert len(rows(home, "SELECT item_id FROM probe_items")) == 30

    plant(repo, {"src/app/e.py": "from app import d\n"})
    new = derive(repo, repo_id).version
    assert new != old
    code, out = run_freeze(repo, home)
    assert code == 2
    assert out == (
        f"praxis components freeze: map {old} is frozen; changing to {new} needs"
        " --reason, one of new_module regrouped renamed correction\n"
    )
    code, out = run_freeze(repo, home, reason=CANARY_REASON)
    assert code == 2
    assert CANARY_REASON not in out

    code, out = run_freeze(repo, home, reason="new_module")
    assert code == 0, out
    lines = out.splitlines()
    assert f"superseded map {old} (new_module)" in lines
    assert "X1 x1-v1: 7 edges among 42 ordered pairs, base rate 0.167" in lines
    assert "D1 items: 42 written at epoch 0, 0 already present" in lines
    maps = rows(
        home,
        "SELECT version, frozen_ts, superseded_ts, reason FROM component_maps"
        " ORDER BY created_ts, version",
    )
    assert {m["version"]: (m["superseded_ts"], m["reason"]) for m in maps} == {
        old: (NOW, None),
        new: (None, "new_module"),
    }
    counts = rows(
        home, "SELECT map_version, COUNT(*) AS n FROM probe_items GROUP BY map_version"
    )
    assert {c["map_version"]: c["n"] for c in counts} == {old: 30, new: 42}
    stored = home_bytes(home)
    assert CANARY_REASON.encode() not in stored
    assert CANARY.encode() not in stored


def test_too_few_components_refuses_and_flags(tmp_path: Path, home: Path) -> None:
    root = tmp_path / "small"
    root.mkdir()
    git(root, "init", "-q")
    plant(
        root,
        {"src/app/__init__.py": "X = 1\n", **{f"src/app/{m}.py": "" for m in "abc"}},
    )
    code, out = run_freeze(root, home)
    assert (code, out) == (
        2,
        "praxis components freeze: fewer than 5 product components in the"
        " mechanical map\n",
    )
    flags = rows(home, "SELECT repo_id, code, detail_json FROM flags")
    assert flags == [
        {
            "repo_id": locate(root).repo_id,  # type: ignore[union-attr]
            "code": "too_few_components",
            "detail_json": '{"source": "mechanical"}',
        }
    ]
    assert rows(home, "SELECT * FROM component_maps") == []


def test_owner_map_is_frozen_with_its_source(repo: Path, home: Path) -> None:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    owner = home / "components" / f"{repo_id}.toml"
    owner.parent.mkdir(parents=True)
    groups = {
        "pkg": ["src/app/__init__.py"],
        "core": ["src/app/a.py", "src/app/b.py"],
        "leaf": ["src/app/c.py"],
        "types": ["src/app/d.py"],
    }

    def write(table: dict[str, list[str]]) -> None:
        owner.write_text(
            "".join(
                f"[[component]]\nid = {cid!r}\nglobs = {json.dumps(globs)}\n"
                for cid, globs in table.items()
            )
        )

    write(groups)
    assert run_freeze(repo, home)[0] == 2
    flags = rows(home, "SELECT code, detail_json FROM flags")
    assert flags == [
        {
            "code": "too_few_components",
            "detail_json": '{"source": "owner", "components": 4}',
        }
    ]

    write({**groups, "sub": ["src/app/sub/*"]})
    code, out = run_freeze(repo, home)
    assert code == 0, out
    assert out.splitlines()[0].endswith("frozen (owner), 5 product components")
    items = rows(
        home,
        "SELECT src_component, dst_component, sites, module_level FROM probe_items"
        " WHERE truth = 1",
    )
    edges = {
        (r["src_component"], r["dst_component"]): (r["sites"], r["module_level"])
        for r in items
    }
    # core -> leaf: a's one import-time site plus b's two function-level sites.
    assert edges == {
        ("pkg", "core"): (1, 1),
        ("core", "leaf"): (3, 1),
        ("types", "core"): (1, 0),
        ("sub", "leaf"): (1, 1),
    }
    assert len(rows(home, "SELECT item_id FROM probe_items")) == 20


def test_show_and_pool_counts(repo: Path, home: Path) -> None:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    unknown = dict.fromkeys(pool.COUNT_KEYS)
    assert pool.pool_counts(home, repo_id) == unknown
    out = io.StringIO()
    assert pool.show(repo, home, out) == 0
    assert "no frozen map" in out.getvalue()
    assert not (home / "ledger.db").exists(), "a read created the ledger"

    assert run_freeze(repo, home)[0] == 0
    version = derive(repo, repo_id).version
    assert pool.pool_counts(home, repo_id) == {
        "components": 7,
        "pairs": 42,
        "items": 30,
        "positive": 6,
        "unexposed": 30,
        "tasks_left": 7,
    }
    out = io.StringIO()
    assert pool.show(repo, home, out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0] == f"map {version} (mechanical), 7 product components"
    assert (
        "  tests (fixed class): tests/* test/* */tests/* */test/* test_*.py"
        " */test_*.py *_test.py" in lines
    )
    assert "  sub: src/app/sub/*" in lines
    assert f"unmapped tracked paths: 1 of {len(FILES)}" in lines
    assert (
        lines[-1] == "pool: 30 items (6 true), 30 unexposed, 7 tasks at 4 items"
        " per task"
    )

    ids = [
        str(r["item_id"])
        for r in rows(home, "SELECT item_id FROM probe_items ORDER BY item_id")
    ]
    ledger = Store(home / "ledger.db").open()
    try:
        ledger.task_register(
            region=[],
            bound=ids[:2],
            related=ids[2:3],
            drawn=ids[3:4],
            task_id="t1",
            repo_id=repo_id,
            map_version=version,
            ordinal=1,
            ts=LATER,
            arm="control",
            seed_hash="ab" * 32,
            stratum="uniform",
        )
    finally:
        ledger.close()
    counts = pool.pool_counts(home, repo_id)
    assert (counts["items"], counts["unexposed"], counts["tasks_left"]) == (30, 26, 6)


def test_praxis_itself(home: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    repo_id = locate(root).repo_id  # type: ignore[union-attr]
    cmap = derive(root, repo_id)
    product = product_components(cmap)
    on_disk = [
        p
        for p in git(root, "ls-files", "-z").split("\0")
        if p.endswith(".py") and (root / p).is_file()
    ]
    python = {assign(cmap, p) for p in on_disk} & set(product)
    k = len(python)

    code, out = run_freeze(root, home)

    assert code == 0, out
    lines = out.splitlines()
    assert lines[0].endswith(f"frozen (mechanical), {len(product)} product components")
    assert f"D1 items: {k * (k - 1)} written at epoch 0, 0 already present" in lines
    unknown = len(product) * (len(product) - 1) - k * (k - 1)
    assert (
        f"  pairs not generated (a component with no parsed Python file): {unknown}"
        in lines
    )
    assert pool.pool_counts(home, repo_id)["items"] == k * (k - 1)


def owner_toml(table: dict[str, list[str]]) -> str:
    return "".join(
        f"[[component]]\nid = {cid!r}\nglobs = {json.dumps(globs)}\n"
        for cid, globs in table.items()
    )


def test_show_names_a_module_the_owner_map_does_not_map(repo: Path, home: Path) -> None:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    owner = home / "components" / f"{repo_id}.toml"
    owner.parent.mkdir(parents=True)
    owner.write_text(
        owner_toml(
            {
                "pkg": ["src/app/__init__.py"],
                "core": ["src/app/a.py", "src/app/b.py"],
                "leaf": ["src/app/c.py"],
                "types": ["src/app/d.py"],
                "sub": ["src/app/sub/ok.py", "src/app/sub/bad.py"],
            }
        )
    )
    assert run_freeze(repo, home)[0] == 0
    # Unmapped of the 13 planted: data.json, the empty sub/__init__.py, scripts/run.sh.
    out = io.StringIO()
    assert pool.show(repo, home, out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0].endswith("(owner), 5 product components")
    assert "unmapped tracked paths: 3 of 13" in lines
    assert "unmapped Python files beside mapped ones: 0" in lines

    plant(repo, {"src/app/e.py": "X = 1\n", "src/app/sub/new.py": "Y = 1\n"})
    out = io.StringIO()
    assert pool.show(repo, home, out) == 0
    lines = out.getvalue().splitlines()
    assert "unmapped tracked paths: 5 of 15" in lines
    at = lines.index("unmapped Python files beside mapped ones: 2")
    assert lines[at + 1 : at + 4] == [
        "  src/app/e.py",
        "  src/app/sub/new.py",
        "  each stays unmapped until the owner's toml assigns it and"
        " `praxis components freeze --reason new_module` refreezes",
    ]
    code, again = run_freeze(repo, home)
    assert code == 0, again
    assert "D1 items: 0 written at epoch 0, 20 already present" in again.splitlines()


# `praxis components derive` and the refusal outside 5 to 30, through the installed
# binary. The flat package has 32 one-file children with no prefix in common and no
# import edge, so the grouped map keeps 32 components and freeze refuses it.

FLAT = {f"src/flat/m{i:02}.py": "" for i in range(32)}
FLAT["src/flat/m00.py"] = f"SECRET = {CANARY!r}\n"
FLAT["src/flat/__init__.py"] = ""


def praxis(*args: str, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    found = shutil.which("praxis")
    assert found is not None, "no `praxis` on PATH: run `uv sync` first"
    env = {
        "HOME": str(home.parent),
        "PRAXIS_HOME": str(home),
        "PATH": os.environ["PATH"],
    }
    return subprocess.run(
        [found, *args], capture_output=True, text=True, cwd=cwd, env=env, check=False
    )


def test_derive_drafts_the_owner_map_and_freeze_prints_it(
    tmp_path: Path, home: Path
) -> None:
    root = tmp_path / "flat"
    root.mkdir()
    git(root, "init", "-q")
    plant(root, FLAT)
    repo_id = locate(root).repo_id  # type: ignore[union-attr]
    cmap = derive(root, repo_id)
    path = home / "components" / f"{repo_id}.toml"
    draft = (
        f"# praxis component map {cmap.version} (mechanical), {repo_id}\n"
        "# warnings: grouped_by_prefix_and_imports above_30_after_grouping\n"
        + "".join(
            f'\n[[component]]\nid = "m{i:02}"\nglobs = ["src/flat/m{i:02}.py"]\n'
            for i in range(32)
        )
    )

    done = praxis("components", "freeze", cwd=root, home=home)
    assert (done.returncode, done.stderr) == (2, "")
    assert done.stdout == (
        "praxis components freeze: more than 30 product components in the"
        f" mechanical map\n{draft}praxis components freeze: `praxis components derive"
        f" --write` saves this draft as {path}; merge or split components there,"
        " then freeze again\n"
    )
    assert rows(home, "SELECT * FROM component_maps") == []
    assert rows(home, "SELECT * FROM flags") == []

    done = praxis("components", "derive", cwd=root, home=home)
    assert (done.returncode, done.stdout, done.stderr) == (0, draft, "")
    assert not path.exists()
    done = praxis("components", "derive", "--write", cwd=root, home=home)
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == f"wrote {path}; edit it, then `praxis components freeze`\n"
    assert path.read_text() == draft
    done = praxis("components", "derive", "--write", cwd=root, home=home)
    assert (done.returncode, done.stderr) == (2, "")
    assert done.stdout == (
        f"praxis components derive: {path} exists; edit it, or move it away\n"
    )
    assert path.read_text() == draft

    # The owner merges m00 and m01: 31 components, still one too many, and no draft.
    groups = {f"m{i:02}": [f"src/flat/m{i:02}.py"] for i in range(2, 32)}
    path.write_text(
        owner_toml({"m00": ["src/flat/m00.py", "src/flat/m01.py"], **groups})
    )
    done = praxis("components", "freeze", cwd=root, home=home)
    assert (done.returncode, done.stdout) == (
        2,
        "praxis components freeze: more than 30 product components in the owner map\n",
    )
    # And m02 too: 30, which freezes as the owner's map.
    del groups["m02"]
    merged = ["src/flat/m00.py", "src/flat/m01.py", "src/flat/m02.py"]
    path.write_text(owner_toml({"m00": merged, **groups}))
    done = praxis("components", "freeze", cwd=root, home=home)
    assert done.returncode == 0, done.stdout
    assert done.stdout.splitlines()[0].endswith("frozen (owner), 30 product components")
    assert CANARY.encode() not in home_bytes(home)


def test_an_unedited_draft_of_the_frozen_mechanical_map_is_refused(
    repo: Path, home: Path
) -> None:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    version = derive(repo, repo_id).version
    assert run_freeze(repo, home)[0] == 0
    out = io.StringIO()
    assert pool.draft(repo, home, out, write=True) == 0
    code, out_text = run_freeze(repo, home)
    assert (code, out_text) == (
        2,
        f"praxis components freeze: map {version} is stored as another source's map"
        " with the same rules\n",
    )
    assert rows(home, "SELECT source FROM component_maps") == [{"source": "mechanical"}]


# epoch re-admission

FLIP = "2026-01-19T09:00:00.000Z"
BACK = "2026-01-26T09:00:00.000Z"


def refreeze(repo: Path, home: Path, now: str) -> list[str]:
    out = io.StringIO()
    assert pool.freeze(repo, home, out, now) == 0, out.getvalue()
    return out.getvalue().splitlines()


def test_a_truth_flip_readmits_the_pair_at_the_next_epoch(
    repo: Path, home: Path
) -> None:
    repo_id = locate(repo).repo_id  # type: ignore[union-attr]
    version = derive(repo, repo_id).version
    assert run_freeze(repo, home)[0] == 0
    pair = "SELECT * FROM probe_items WHERE src_component = 'c' AND dst_component = 'b'"
    (old,) = rows(home, pair)
    assert (old["truth"], old["epoch"]) == (0, 0)
    ledger = Store(home / "ledger.db").open()
    try:
        ledger.task_register(
            region=[], bound=[str(old["item_id"])], related=[], drawn=[],
            task_id="t1", repo_id=repo_id, map_version=version, ordinal=1, ts=LATER,
            arm="control", seed_hash="ab" * 32, stratum="uniform",
        )  # fmt: skip
    finally:
        ledger.close()
    exposures = rows(home, "SELECT * FROM exposures")
    before = rows(home, "SELECT * FROM probe_items ORDER BY item_id")

    plant(repo, {"src/app/c.py": FILES["src/app/c.py"] + "from app import b\n"})
    lines = refreeze(repo, home, FLIP)

    assert "D1 items: 0 written at epoch 0, 30 already present" in lines
    assert "D1 truth flips re-admitted at the next epoch: 1" in lines
    after = rows(home, "SELECT * FROM probe_items ORDER BY item_id")
    assert [r for r in after if r in before] == before, "an item was changed"
    (new,) = [r for r in after if r not in before]
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert {k: new[k] for k in ("relation", "src_component", "dst_component")} == {
        "relation": "D1",
        "src_component": "c",
        "dst_component": "b",
    }
    assert (new["truth"], new["epoch"], new["sites"], new["module_level"]) == (
        1,
        1,
        1,
        1,
    )
    assert (new["truth_ts"], new["map_version"], new["coverage"]) == (
        FLIP,
        version,
        "full",
    )
    assert json.loads(str(new["sources"])) == [
        "extractor:x1-v1",
        f"map:{version}",
        f"tree:{tree}",
    ]
    assert rows(home, "SELECT * FROM exposures") == exposures
    counts = pool.pool_counts(home, repo_id)
    assert (counts["items"], counts["positive"], counts["unexposed"]) == (30, 7, 30)

    lines = refreeze(repo, home, BACK)
    assert "D1 truth flips re-admitted at the next epoch: 0" in lines
    assert len(rows(home, "SELECT item_id FROM probe_items")) == 31

    plant(repo, {"src/app/c.py": FILES["src/app/c.py"]})
    lines = refreeze(repo, home, "2026-02-02T09:00:00.000Z")
    assert "D1 truth flips re-admitted at the next epoch: 1" in lines
    epochs = rows(home, pair + " ORDER BY epoch")
    assert [(r["epoch"], r["truth"], r["sites"]) for r in epochs] == [
        (0, 0, None),
        (1, 1, 1),
        (2, 0, None),
    ]
    assert CANARY.encode() not in home_bytes(home)


def test_a_pair_x1_no_longer_parses_is_not_readmitted(repo: Path, home: Path) -> None:
    assert run_freeze(repo, home)[0] == 0
    # c stops parsing: a -> c, b -> c and sub -> c still resolve to c.py, but c has no
    # parsed file, so its pairs are unknown, not flipped to 0.
    plant(repo, {"src/app/c.py": "def (:\n"})
    lines = refreeze(repo, home, FLIP)
    assert "D1 truth flips re-admitted at the next epoch: 0" in lines
    assert "D1 items: 0 written at epoch 0, 20 already present" in lines
    assert len(rows(home, "SELECT item_id FROM probe_items WHERE epoch > 0")) == 0
