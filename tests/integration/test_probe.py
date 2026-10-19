"""`praxis probe <kind>`, driven through probe.administer against a real git repository
with real imports, a map derived and frozen from it, items exposed by tasks registered
through the store, and a real ledger under a temporary $PRAXIS_HOME. The verb lands in
stage C, so these tests pass the stdin and stdout it will pass. Expected values are
written before each run; PFSIM's are worked by hand in the test's comments.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from praxis import probe
from praxis.components import derive, product_components, to_json
from praxis.store import Evidence, Store
from praxis.transcript import options_hash
from praxis.unassisted import locate

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

NOW = "2026-01-05T09:00:00.000Z"
CANARY = "CANARYPROBE51d3"
LABEL_CANARY = "CANARYLABEL0e2a"
MODULES = ("a", "b", "c", "d", "e", "f")
IMPORTS = {"a": ["b"], "b": ["c"], "e": ["a"]}  # X1: a->b, b->c, e->a
EV = Evidence("full", ["test:planted"], "test-v1", [], [])
SEED_HASH = "0" * 64


def ago(days: float) -> str:
    when = dt.datetime.fromisoformat(NOW) - dt.timedelta(days=days)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_module(root: Path, name: str, imports: list[str]) -> None:
    body = "".join(f"import pkg.{m}\n" for m in imports) + f"NAME = {name!r}\n"
    (root / "src" / "pkg" / f"{name}.py").write_text(body)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("")
    for m in MODULES:
        write_module(root, m, IMPORTS.get(m, []))
    git = ["git", "-C", str(root)]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                    "-q", "-m", "init"], check=True)  # fmt: skip
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "praxis-home"


@contextmanager
def ledger(home: Path) -> Iterator[Store]:
    s = Store(home / "ledger.db").open()
    try:
        yield s
    finally:
        s.close()


def freeze(repo: Path, home: Path) -> tuple[str, str]:
    found = locate(repo)
    assert found is not None
    cmap = derive(repo, found.repo_id)
    assert product_components(cmap) == list(MODULES)
    with ledger(home) as s:
        s.component_map_put(to_json(cmap), ago(40))
        s.component_map_freeze(found.repo_id, cmap.version, ago(40))
    return found.repo_id, cmap.version


ITEMS = [  # (relation, src, dst, truth at the pool's extraction)
    ("D1", "f", "a", False),  # exposed 30 days ago: past L_max
    ("D1", "a", "b", True),
    ("D1", "b", "a", False),
    ("D1", "c", "d", False),  # c imports d by probe time: truth changed
    ("D2", "e", "b", True),  # e -> a -> b
    ("D1", "d", "e", False),  # exposed 2 days ago: not yet due
]


def expose(home: Path, repo_id: str, version: str) -> dict[tuple[str, str], str]:
    """Three tasks, each superseding the last: 30, 8 and 2 days before NOW."""
    with ledger(home) as s:
        ids = s.probe_items_put(
            rows=[(r, a, b, t, 0, None, None) for r, a, b, t in ITEMS], ev=EV,
            repo_id=repo_id, map_version=version, truth_ts=ago(40),
            extractor_version="x1-v1",
        )  # fmt: skip
        key = dict(zip([(a, b) for _, a, b, _ in ITEMS], ids, strict=True))
        plan = [(30, ["fa"], [], []), (8, ["ab", "ba"], ["cd"], ["eb"]),
                (2, ["de"], [], [])]  # fmt: skip
        for ordinal, (days, bound, related, drawn) in enumerate(plan, 1):
            ids_of = [[key[(p[0], p[1])] for p in g] for g in (bound, related, drawn)]
            s.task_register(
                region=[], bound=ids_of[0], related=ids_of[1], drawn=ids_of[2],
                task_id=f"task{ordinal}", repo_id=repo_id, map_version=version,
                ordinal=ordinal, ts=ago(days), arm="control", seed_hash=SEED_HASH,
                stratum="uniform",
            )  # fmt: skip
    return key


def administer(repo: Path, home: Path, kind: str, typed: str, now: str = NOW) -> str:
    out = io.StringIO()
    code = probe.administer(repo, home, io.StringIO(typed), out, now, kind)
    assert code == 0, out.getvalue()
    return out.getvalue()


def rows(home: Path, sql: str) -> list[sqlite3.Row]:
    with ledger(home) as s:
        return list(s.db.execute(sql))


def flags(home: Path) -> list[tuple[str, dict[str, object]]]:
    found = rows(home, "SELECT code, detail_json FROM flags ORDER BY id")
    return [(r["code"], json.loads(r["detail_json"])) for r in found]


def home_bytes(home: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file())


def git_out(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True)  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def exposed(repo: Path, home: Path) -> tuple[str, str, dict[tuple[str, str], str]]:
    repo_id, version = freeze(repo, home)
    return repo_id, version, expose(home, repo_id, version)


def test_structural_records_brier_inputs_rereads_truth_and_drops_typed_text(
    repo: Path, home: Path, exposed: tuple[str, str, dict[tuple[str, str], str]]
) -> None:
    repo_id, _, key = exposed
    due_ids = [key[p] for p in [("a", "b"), ("b", "a"), ("c", "d"), ("e", "b")]]
    assert [r["item_id"] for r in probe.due(home, repo_id, NOW, 7.0)] == due_ids
    write_module(repo, "c", ["d"])  # c now imports d, uncommitted
    head = git_out(repo, "rev-parse", "HEAD")
    tree = git_out(repo, "rev-parse", "HEAD^{tree}")
    typed = f"{CANARY}\n9\ns\n{CANARY}\n7\n1\n"
    shown = administer(repo, home, "structural", typed)
    expected = [  # item, p, skipped, truth now, truth changed, task, coverage
        (key["a", "b"], 0.9, 0, 1, 0, "task2", "full"),
        (key["b", "a"], None, 1, 0, 0, "task2", "full"),
        (key["c", "d"], 0.7, 0, 1, 1, "task2", "full"),
        (key["e", "b"], 0.1, 0, 1, 0, "task2", "full"),
    ]
    got = rows(home, "SELECT * FROM probe_responses ORDER BY id")
    cols = ("item_id", "p", "skipped", "truth_at_probe", "truth_changed", "task_id",
            "coverage")  # fmt: skip
    assert [tuple(r[c] for c in cols) for r in got] == expected
    for r in got:
        assert 8.0 <= r["lag_days"] < 8.01
        assert (r["deadline_met"], r["agent_attached"], r["dirty"]) == (1, 0, 1)
        assert (r["head_sha"], r["tree_hash"]) == (head, tree)
        assert r["claim_class"] == "observed"
        assert json.loads(r["sources"]) == [f"probe_item:{r['item_id']}",
                                            "x1:x1-v1", f"tree:{tree}"]  # fmt: skip
    assert "answered 4; 0 could not be scored now" in shown
    assert "yes" not in shown  # no answer key at probe time
    assert probe.due(home, repo_id, NOW, 7.0) == []
    assert "no structural item is due" in administer(repo, home, "structural", "")
    assert CANARY.encode() not in home_bytes(home)


def test_an_unparsed_source_component_is_not_asked_and_stays_due(
    repo: Path, home: Path, exposed: tuple[str, str, dict[tuple[str, str], str]]
) -> None:
    repo_id, _, key = exposed
    (repo / "src" / "pkg" / "a.py").write_text("def broken(:\n")
    shown = administer(repo, home, "structural", "5\n5\n5\n")
    got = rows(home, "SELECT item_id, coverage, warnings FROM probe_responses")
    # a->b unreadable; b->a and c->d are D1 and unaffected; e->b is D2 and partial.
    assert [(r["item_id"], r["coverage"], json.loads(r["warnings"])) for r in got] == [
        (key["b", "a"], "full", []),
        (key["c", "d"], "full", []),
        (key["e", "b"], "partial", ["parse_failure"]),
    ]
    assert "answered 3; 1 could not be scored now and stay due" in shown
    assert [r["item_id"] for r in probe.due(home, repo_id, NOW, 7.0)] == [key["a", "b"]]


@contextmanager
def fake_agent(bin_dir: Path, cwd: Path) -> Iterator[int]:
    # A Python script named claude, so the name survives into the argv.
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("import time\ntime.sleep(60)\n")
    agent = subprocess.Popen([sys.executable, str(script)], cwd=cwd)
    try:
        yield agent.pid
    finally:
        agent.kill()
        agent.wait()


@pytest.mark.parametrize("kind", probe.KINDS)
def test_an_attached_agent_refuses_every_kind_and_the_refusal_is_recorded(
    repo: Path,
    home: Path,
    tmp_path: Path,
    exposed: tuple[str, str, dict[tuple[str, str], str]],
    kind: str,
) -> None:
    repo_id = exposed[0]
    out = io.StringIO()
    with fake_agent(tmp_path / "bin", repo / "src") as pid:
        code = probe.administer(repo, home, io.StringIO("9\n" * 20), out, NOW, kind)
    assert code == 2
    assert f"claude pid {pid} in {repo / 'src'}" in out.getvalue()
    assert flags(home) == [("agent_attached", {"kind": kind, "agents": 1})]
    for table in ("probe_responses", "drift_responses", "decision_responses",
                  "knockouts", "pathfinder_runs"):  # fmt: skip
        assert rows(home, f"SELECT * FROM {table}") == []  # noqa: S608
    assert len(probe.due(home, repo_id, NOW, 7.0)) == 4


def test_end_of_input_abandons_and_a_late_answer_misses_the_deadline(
    repo: Path,
    home: Path,
    exposed: tuple[str, str, dict[tuple[str, str], str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_id, _, key = exposed
    # The deadline is a constant (60 s, provisional); a real minute is too long to
    # wait, so the test shortens it and answers through a real pipe after 0.4 s.
    monkeypatch.setattr(probe, "DEADLINE_S", 0.1)
    read_fd, write_fd = os.pipe()

    def late() -> None:
        time.sleep(0.4)
        os.write(write_fd, b"5\n")
        os.close(write_fd)

    writer = threading.Thread(target=late)
    writer.start()
    with os.fdopen(read_fd) as stdin:
        out = io.StringIO()
        code = probe.administer(repo, home, stdin, out, NOW, "structural")
    writer.join()
    assert code == 0
    (got,) = rows(home, "SELECT item_id, p, seconds, deadline_met FROM probe_responses")
    assert (got["item_id"], got["p"], got["deadline_met"]) == (key["a", "b"], 0.5, 0)
    assert got["seconds"] > 0.1  # the sleep starts before the question is shown
    assert flags(home) == [("abandoned", {"kind": "structural", "answered": 1,
                                          "due": 4})]  # fmt: skip
    assert len(probe.due(home, repo_id, NOW, 7.0)) == 3


def scan(
    home: Path, repo_id: str, version: str, ts: str, edges: dict[str, list[str]]
) -> None:
    """One recorder scan at Stop: each file with the components it imports."""
    files = {f"src/pkg/{m}.py": m for m in edges}
    found = [(f"src/pkg/{m}.py", d, 1, True) for m, ds in edges.items() for d in ds]
    with ledger(home) as s:
        s.file_edges(scanned=files, edges=found, ev=EV, session_id=f"sess-{ts[:10]}",
                     repo_id=repo_id, map_version=version, ts=ts,
                     extractor_version="x1-v1")  # fmt: skip


def test_drift_scores_against_the_edge_history_once_per_week(
    repo: Path, home: Path
) -> None:
    repo_id, version = freeze(repo, home)
    scan(home, repo_id, version, ago(20), {"a": ["b"], "c": []})
    scan(home, repo_id, version, ago(3), {"a": ["b"], "c": ["d"], "f": []})
    # a: same imports as its last scan; c: gained d; f: first scan, nothing to compare;
    # b, d, e: not scanned in a window where the recorder scanned something.
    typed = f"y\nn\n{CANARY}\ny\nn\ns\ny\n"
    shown = administer(repo, home, "drift", typed)
    expected = [("a", 1, 0, "full"), ("b", 0, 0, "full"), ("c", 1, 1, "full"),
                ("d", 0, 0, "full"), ("e", None, 0, "full"),
                ("f", 1, None, "unobservable")]  # fmt: skip
    got = rows(home, "SELECT * FROM drift_responses ORDER BY id")
    assert [(r["component"], r["said_changed"], r["did_change"], r["coverage"])
            for r in got] == expected  # fmt: skip
    assert {r["week"] for r in got} == {"2026-W02"}
    assert json.loads(got[0]["sources"]) == [
        f"edge_history:{ago(7)}/{NOW}", f"sess-{ago(3)[:10]}"]  # fmt: skip
    assert "  c: you said yes, changed: yes\n" in shown
    assert "  f: you said yes, changed: unknown\n" in shown
    again = administer(repo, home, "drift", "y\n" * 6)
    assert "the drift probe for 2026-W02 is already answered" in again
    assert len(rows(home, "SELECT * FROM drift_responses")) == 6
    assert CANARY.encode() not in home_bytes(home)


def ask_entry(tid: str, labels: list[str]) -> str:
    options = [{"label": x, "description": "d"} for x in labels]
    question = {"question": "q", "header": "h", "multiSelect": False,
                "options": options}  # fmt: skip
    block = {"type": "tool_use", "id": tid, "name": "AskUserQuestion",
             "input": {"questions": [question]}}  # fmt: skip
    entry = {"type": "assistant", "isSidechain": False, "timestamp": ago(10),
             "message": {"role": "assistant", "content": [block],
                         "stop_reason": "tool_use"}}  # fmt: skip
    return json.dumps(entry)


def test_decision_recall_voids_swept_and_changed_transcripts_and_feeds_back(
    repo: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_id, _ = freeze(repo, home)
    user_home = tmp_path / "user-home"
    project = user_home / ".claude" / "projects" / "-tmp-repo"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user_home))
    shown_labels = ["keep", "split", LABEL_CANARY]
    (project / "sess-1.jsonl").write_text(
        ask_entry("t1", shown_labels) + "\n" + ask_entry("t3", ["x", "y"]) + "\n"
    )
    digest = options_hash(shown_labels)
    planted = [  # (session, tool_use_id, days ago, chosen)
        ("sess-1", "t1", 10, 1),  # due, transcript holds it: asked
        ("sess-2", "t2", 12, 0),  # due, transcript gone: transcript_swept
        ("sess-1", "t3", 11, 0),  # due, labels differ: transcript_changed
        ("sess-1", "t4", 2, 0),  # too recent
        ("sess-1", "t5", 9, None),  # chosen option unknown: cannot be scored
    ]
    with ledger(home) as s:
        for session in ("sess-1", "sess-2"):
            s.touch_session_repo(session, repo_id, ago(20))
        ids = {
            tid: s.decision(ev=EV, session_id=sid, tool_use_id=tid,
                            tool_name="AskUserQuestion", prompt_id=None, ts=ago(days),
                            option_count=3, chosen_index=chosen, options_hash=digest)
            for sid, tid, days, chosen in planted
        }  # fmt: skip
    shown = administer(repo, home, "decision", f"{CANARY} why\n{CANARY}\n4\n2\n")
    got = rows(home, "SELECT * FROM decision_responses")
    assert [(r["decision_id"], r["recalled_index"], r["skipped"]) for r in got] == [
        (ids["t1"], 1, 0)
    ]
    assert 10.0 <= got[0]["lag_days"] < 10.01
    assert "offered 3 options" in shown
    assert "It was option 2." in shown
    assert LABEL_CANARY not in shown
    voids = [("transcript_swept", {"decision_id": ids["t2"]}),
             ("transcript_changed", {"decision_id": ids["t3"]})]  # fmt: skip
    assert flags(home) == voids
    assert "no decision is due" in administer(repo, home, "decision", "")
    assert flags(home) == voids
    data = home_bytes(home)
    assert CANARY.encode() not in data
    assert LABEL_CANARY.encode() not in data


def test_knockout_shows_failing_scenarios_and_scores_the_named_components(
    repo: Path, home: Path
) -> None:
    repo_id, version = freeze(repo, home)
    plant = {f"run-{m}": [m] for m in MODULES}  # each scenario enters one component
    with ledger(home) as s:
        for n, (sid, comps) in enumerate(plant.items()):
            s.scenario_run(components=comps, call_edges={}, ev=EV, repo_id=repo_id,
                           map_version=version, ts=ago(5), scenario_id=sid,
                           command_hash=f"{n:016x}", outcome="ok", exit_code=0,
                           duration_ms=1.0, extractor_version="x2-v1")  # fmt: skip
        # Latest run failed: not a baseline a knockout can be read against.
        s.scenario_run(components=list(MODULES), call_edges={}, ev=EV,
                       repo_id=repo_id, map_version=version, ts=ago(1),
                       scenario_id="run-a", command_hash="f" * 16,
                       outcome="failed", exit_code=1, duration_ms=1.0,
                       extractor_version="x2-v1")  # fmt: skip

    def knocked(now: str) -> str:
        """The draw for ``now``, read from what is shown: one failing scenario, or
        none when the draw is a, which no ok run enters."""
        found = re.search(r"Scenarios that fail: (\S+) \(of 5\)",
                          administer(repo, home, "knockout", "", now))  # fmt: skip
        assert found is not None
        shown = found.group(1)
        return "a" if shown == "none" else shown.removeprefix("run-")

    truth = knocked(NOW)
    number = MODULES.index(truth) + 1
    administer(repo, home, "knockout", f"{CANARY}\n{number} {number}\n{number}\n")
    other = ago(-1)
    wrong = MODULES.index("a" if knocked(other) != "a" else "b") + 1
    administer(repo, home, "knockout", f"{wrong}\n", other)
    got = rows(home, "SELECT * FROM knockouts ORDER BY id")
    assert [(json.loads(r["components_named_json"]), r["hit"], r["scenario_id"])
            for r in got] == [([truth], 1, None),
                              ([MODULES[wrong - 1]], 0, None)]  # fmt: skip
    assert got[0]["component_knocked"] == truth
    assert len(json.loads(got[0]["sources"])) == 5
    assert [c for c, _ in flags(home)] == ["abandoned", "abandoned"]
    assert CANARY.encode() not in home_bytes(home)


def test_pathfinder_stores_pfsim_against_x1_and_discards_the_ratings(
    repo: Path, home: Path
) -> None:
    freeze(repo, home)
    # X1 links: a-b, b-c, a-e. Rating those 9 (distance 1) and every other pair 1
    # (distance 9): PFnet keeps the three, drops a-c, b-e, c-e (a path of steps 1
    # beats 9), and keeps all 9 pairs touching d or f (every path to them has a step
    # of 9). L1 = 12, L2 = 3, C = 3: S = 3 / (12 + 3 - 3) = 0.25; E[C] = 12 * 3 / 15
    # = 2.4, chance S = 2.4 / 12.6, S minus chance = 0.25 - 0.19047619... = 0.0595238.
    close = {("a", "b"), ("b", "c"), ("a", "e")}
    pairs = [(x, y) for i, x in enumerate(MODULES) for y in MODULES[i + 1 :]]
    typed = f"{CANARY}\n10\n" + "".join(
        "9\n" if pair in close else "1\n" for pair in pairs
    )
    assert "ratings are discarded" in administer(repo, home, "pathfinder", typed)
    administer(repo, home, "pathfinder", "9\n1\ns\n")
    (got,) = rows(home, "SELECT * FROM pathfinder_runs")
    assert (got["n_components"], got["coverage"]) == (6, "full")
    assert got["pfsim"] == pytest.approx(0.25)
    assert got["pfsim_minus_chance"] == pytest.approx(0.25 - 2.4 / 12.6)
    assert flags(home) == [("abandoned", {"kind": "pathfinder", "rated": 2,
                                          "pairs": 15})]  # fmt: skip
    assert CANARY.encode() not in home_bytes(home)


def test_refusals_outside_a_repository_and_without_a_frozen_map(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    out = io.StringIO()
    assert probe.administer(tmp_path, home, io.StringIO(), out, NOW, "drift") == 2
    assert "not inside a git repository" in out.getvalue()
    out = io.StringIO()
    assert probe.administer(repo, home, io.StringIO(), out, NOW, "drift") == 2
    assert "no frozen component map" in out.getvalue()
    with pytest.raises(ValueError, match="probe kinds"):
        probe.administer(repo, home, io.StringIO(), out, NOW, "teach-back")
    found = locate(repo)
    assert found is not None
    assert probe.due(home, found.repo_id, NOW, 7.0) == []
