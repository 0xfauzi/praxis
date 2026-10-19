"""`praxis start task` and `praxis show task`, driven through the module against a real
git repository, a component map derived and set from it, planted probe items and a real
ledger under a temporary $PRAXIS_HOME. These tests call task.register and task.last
with the stdin and stdout that the commands pass.

The map is set at FROZEN, so the start week (design 6.4, at most 7 weeks later) is past
at NOW and the arm is drawn; the forced-control test sets its map at NOW instead.

The arm is checked against an oracle written here from the rule in task.py's docstring
(sha256 of json.dumps([seed, repo_id, ordinal]), treatment below 0.5). repo_id hashes
the temporary repository's path, so a seed cannot be fixed in advance: each test finds
the first seed that the oracle says gives the arm it needs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import random
import subprocess
from itertools import permutations
from typing import TYPE_CHECKING

import pytest

from praxis import task
from praxis.components import derive, product_components, to_json
from praxis.store import Evidence, Store
from praxis.unassisted import locate

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

pytestmark = pytest.mark.integration

NOW = "2026-01-05T09:00:00.000Z"
FROZEN = "2025-10-06T09:00:00.000Z"
CANARY = "CANARYTASK7c41e"
MODULES = ("a", "b", "c", "d", "e", "f")
TRUE_EDGES = {("a", "b"), ("b", "c"), ("c", "d"), ("e", "a")}
EV = Evidence("full", ["session:s1:turn:0"], "test-v1", [], [])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    pkg = root / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for m in MODULES:
        (pkg / f"{m}.py").write_text(f"NAME = {m!r}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "praxis-home"


def ledger(home: Path) -> Store:
    return Store(home / "ledger.db").open()


Item = tuple[str, str, str, bool, int, None, None]
ALL_PAIRS: list[Item] = [
    ("D1", a, b, (a, b) in TRUE_EDGES, 0, None, None)
    for a, b in permutations(MODULES, 2)
]


def freeze(
    repo: Path, home: Path, items: list[Item] = ALL_PAIRS, at: str | None = FROZEN
) -> tuple[str, str]:
    """Derive, store and set the map and draw the start week as `praxis set
    components` does (no draw when ``at`` is None); plant the items (every ordered D1
    pair at epoch 0 unless told otherwise)."""
    found = locate(repo)
    assert found is not None
    cmap = derive(repo, found.repo_id)
    assert sorted(product_components(cmap)) == list(MODULES)
    s = ledger(home)
    try:
        s.component_map_put(to_json(cmap), at or FROZEN)
        s.component_map_freeze(found.repo_id, cmap.version, at or FROZEN)
        if at is not None:
            task.draw_start(s, found.repo_id, cmap.version, at)
        if items:
            s.probe_items_put(
                rows=items,
                ev=EV,
                repo_id=found.repo_id,
                map_version=cmap.version,
                truth_ts=NOW,
                extractor_version="x1-test",
            )
    finally:
        s.close()
    return found.repo_id, cmap.version


def oracle(seed: int, repo_id: str, ordinal: int) -> tuple[str, str]:
    key = hashlib.sha256(json.dumps([seed, repo_id, ordinal]).encode()).hexdigest()
    arm = "treatment" if random.Random(int(key, 16)).random() < 0.5 else "control"
    return key, arm


def seed_for(arm: str, repo_id: str, ordinal: int) -> int:
    return next(s for s in range(1000) if oracle(s, repo_id, ordinal)[1] == arm)


def run(
    repo: Path, home: Path, seed: int, typed: str = "", now: str = NOW
) -> tuple[int, str, int]:
    stdin, stdout = io.StringIO(typed), io.StringIO()
    code = task.register(repo, home, stdin, stdout, now, seed)
    return code, stdout.getvalue(), stdin.tell()


START = "praxis start task: "
CONTROL_OUT = "No task is closed yet.\nPraxis recorded task 1 as a control task.\n"


def tasks(home: Path, repo_id: str) -> list[sqlite3.Row]:
    s = ledger(home)
    try:
        return s.task_rows(repo_id)
    finally:
        s.close()


def exposures(home: Path, task_id: str) -> dict[str, list[sqlite3.Row]]:
    s = ledger(home)
    try:
        rows = s.exposures_of(task_id)
    finally:
        s.close()
    roles: dict[str, list[sqlite3.Row]] = {"bound": [], "related": [],
                                           "drawn_not_shown": []}  # fmt: skip
    for r in rows:
        roles[r["role"]].append(r)
    return roles


def pair(row: sqlite3.Row) -> set[str]:
    return {row["src_component"], row["dst_component"]}


def question(row: sqlite3.Row) -> str:
    return f"Does code in {row['src_component']} import code in {row['dst_component']}?"


def home_bytes(home: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file())


def close_with(home: Path, task_id: str, touched: set[str], ts: str) -> None:
    """What the recorder does at the Stop that closes a segment."""
    s = ledger(home)
    try:
        s.task_bind(task_id, "s1", "p1", ts)
        s.task_close(task_id, ts)
        finals = {"a": True, "b": None}
        edges = {"a": (1, 0)}
        rows = [
            (c, c in touched, finals.get(c, False), *edges.get(c, (None, None)))
            for c in MODULES
        ]
        s.outcome(task_id, rows, ts, ev=EV)
    finally:
        s.close()


def test_refuses_without_a_set_map_a_start_or_a_repository(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    code, out, _ = run(repo, home, 0)
    assert (code, out) == (2, START + "this repository has no set component map. To"
                              " set one, run `praxis set components`.\n")  # fmt: skip
    found = locate(repo)
    assert found is not None
    assert tasks(home, found.repo_id) == []
    freeze(repo, home, at=None)  # a map set without the start draw
    code, out, _ = run(repo, home, 0)
    assert (code, out) == (2, START + "this repository has no study start record. To"
                              " make one, run `praxis set components`.\n")  # fmt: skip
    assert tasks(home, found.repo_id) == []
    outside = tmp_path / "plain"
    outside.mkdir()
    refused = "praxis start task: run this command in a git repository.\n"
    assert run(outside, home, 0)[:2] == (2, refused)
    buf = io.StringIO()
    assert task.last(outside, home, buf) == 2
    assert buf.getvalue() == "praxis show task: run this command in a git repository.\n"
    buf = io.StringIO()
    assert (task.last(repo, home, buf), buf.getvalue()) == (
        0,
        "No task is closed yet.\n",
    )


def test_control_binds_items_and_reads_nothing(repo: Path, home: Path) -> None:
    repo_id, version = freeze(repo, home)
    seed = seed_for("control", repo_id, 1)
    code, out, consumed = run(repo, home, seed, f"{CANARY}\n9\n")
    assert (code, consumed) == (0, 0)
    assert out == CONTROL_OUT
    (row,) = tasks(home, repo_id)
    key = oracle(seed, repo_id, 1)[0]
    expected = {"ordinal": 1, "arm": "control", "seed_hash": key, "status": "unbound",
                "stratum": "uniform", "region_json": "[]", "ts": NOW,
                "map_version": version, "forced_control": 0}  # fmt: skip
    assert {k: row[k] for k in expected} == expected
    roles = exposures(home, row["task_id"])
    assert [len(roles[r]) for r in ("bound", "related", "drawn_not_shown")] == [2, 1, 1]
    ids = [r["item_id"] for rows in roles.values() for r in rows]
    assert len(set(ids)) == 4
    (rel,) = roles["related"]
    overlaps = [len(pair(rel) & pair(b)) for b in roles["bound"]]
    assert max(overlaps) == 1
    s = ledger(home)
    try:
        assert s.forecasts_of(row["task_id"]) == []
        assert len(s.probe_items_unexposed(repo_id, version)) == 30 - 4
    finally:
        s.close()


def test_treatment_records_grid_answers_and_drops_typed_text(
    repo: Path, home: Path
) -> None:
    repo_id, _ = freeze(repo, home)
    seed = seed_for("treatment", repo_id, 1)
    answers = ["9", "s", "1", "5", "7", "3", "9"]
    typed = "".join(f"{CANARY}\n{a}\n" for a in answers)
    code, out, _ = run(repo, home, seed, typed)
    assert code == 0
    assert out.endswith("Praxis recorded task 1 as a treatment task.\n")
    assert out.count("  Type one of the answers in the brackets.\n") == 7
    (row,) = tasks(home, repo_id)
    assert (row["arm"], row["forced_control"]) == ("treatment", 0)
    roles = exposures(home, row["task_id"])
    s = ledger(home)
    try:
        got = s.forecasts_of(row["task_id"])
    finally:
        s.close()
    assert [r["kind"] for r in got] == ["forecast"] * 5 + ["prequestion"] * 2
    assert [r["p"] for r in got] == [0.9, None, 0.1, 0.5, 0.7, 0.3, 0.9]
    components = [r["component"] for r in got[:5]]
    assert len(set(components)) == 5
    assert set(components) <= set(MODULES)
    assert [r["item_id"] for r in got[5:]] == [r["item_id"] for r in roles["bound"]]
    assert all(r["claim_class"] == "observed" and r["ts"] > NOW for r in got)
    for r in roles["bound"]:
        assert question(r) in out
    for r in roles["related"] + roles["drawn_not_shown"]:
        assert question(r) not in out
    assert CANARY.encode() not in home_bytes(home)


def test_a_second_task_supersedes_the_open_one_and_end_of_input_is_unanswered(
    repo: Path, home: Path
) -> None:
    repo_id, _ = freeze(repo, home)
    first_seed = seed_for("control", repo_id, 1)
    assert run(repo, home, first_seed)[0] == 0
    second_seed = seed_for("treatment", repo_id, 2)
    code, out, _ = run(repo, home, second_seed, "")
    assert code == 0
    assert "The previous open task is now void. This task replaces it.\n" in out
    first, second = tasks(home, repo_id)
    assert (first["status"], first["void_reason"]) == ("superseded", "superseded")
    assert (second["status"], second["arm"], second["ordinal"]) == (
        "unbound",
        "treatment",
        2,
    )
    s = ledger(home)
    try:
        got = s.forecasts_of(second["task_id"])
        assert len(s.exposures_of(first["task_id"])) == 4
    finally:
        s.close()
    assert [r["p"] for r in got] == [None] * 7


def test_empty_pool_registers_with_stratum_empty(repo: Path, home: Path) -> None:
    repo_id, _ = freeze(repo, home, [])
    code, out, _ = run(repo, home, seed_for("treatment", repo_id, 1), "5\n" * 5)
    assert code == 0
    assert "as it is now" not in out
    (row,) = tasks(home, repo_id)
    assert (row["stratum"], row["region_json"]) == ("empty", "[]")
    assert exposures(home, row["task_id"]) == {"bound": [], "related": [],
                                               "drawn_not_shown": []}  # fmt: skip


def test_region_follows_touch_rate_and_last_shows_bound_items_only(
    repo: Path, home: Path
) -> None:
    repo_id, version = freeze(repo, home)
    t1_seed = seed_for("treatment", repo_id, 1)
    assert run(repo, home, t1_seed, "9\n" * 7)[0] == 0
    (t1,) = tasks(home, repo_id)
    close_with(home, t1["task_id"], {"a", "b", "c"}, "2026-01-05T10:00:00.000Z")
    s = ledger(home)
    try:
        s.divergence(ev=EV, repo_id=repo_id, map_version=version,
                     ts="2026-01-05T10:00:00.000Z", session_id="s1",
                     task_id=t1["task_id"], src_component="a", dst_component="f",
                     rule_allowed=False, noticed=None)  # fmt: skip
    finally:
        s.close()
    buf = io.StringIO()
    assert task.last(repo, home, buf) == 0
    shown = buf.getvalue()
    lines = shown.splitlines()
    assert lines[:4] == [
        "Last closed task: task 1, a treatment task, closed at"
        " 2026-01-05T10:00:00.000Z",
        "Components that the agent changed: a, b, c",
        "  a: changed at the end yes, imports of other components 1 added, 0 removed",
        "  b: changed at the end unknown, imports of other components unknown",
    ]
    assert "  a -> f: the rule is forbidden" in lines
    roles = exposures(home, t1["task_id"])
    for r in roles["bound"]:
        answer = "yes" if r["truth"] else "no"  # the tree holds no import: now no
        assert f"  {question(r)} {answer}, now no" in lines
    for r in roles["related"] + roles["drawn_not_shown"]:
        assert question(r) not in shown
    # Every answer was 0.9, so a miss costs 0.81 and a hit 0.01: every miss (the
    # untouched d, e, f and the false items) leads the list.
    head = "Your answers and the results, the most confident wrong answers first:"
    start = lines.index(head)
    answered = lines[start + 1 :]
    assert len(answered) == 7
    outcome = ["no" if line.endswith("result no") else "yes" for line in answered]
    assert outcome == sorted(outcome)  # every "no" (a miss at 0.9) before any "yes"
    asked = "  Will the agent change code in "
    misses = [line for line in answered if asked in line and "result no" in line]
    assert {line[len(asked)] for line in misses} <= {"d", "e", "f"}

    t2_seed = seed_for("control", repo_id, 2)
    assert run(repo, home, t2_seed)[0] == 0
    t2 = tasks(home, repo_id)[1]
    close_with(home, t2["task_id"], {"a", "b", "d"}, "2026-01-05T11:00:00.000Z")
    code, out, _ = run(repo, home, seed_for("control", repo_id, 3))
    assert code == 0
    assert out.startswith("Last closed task: task 2, a control task, closed at"
                          " 2026-01-05T11:00:00.000Z\n")  # fmt: skip
    assert "Your answers" not in out
    t3 = tasks(home, repo_id)[2]
    region = json.loads(t3["region_json"])
    # Rates: a 1, b 1, c 0.5, d 0.5, e 0, f 0. h = 3: a, b and one of the tied c, d.
    assert len(region) == 3
    assert {"a", "b"} <= set(region)
    assert set(region) - {"a", "b"} <= {"c", "d"}
    assert t3["stratum"] == "region"
    for r in exposures(home, t3["task_id"])["bound"]:
        assert pair(r) & set(region)


def test_last_rereads_bound_item_truth_from_the_working_tree(
    repo: Path, home: Path
) -> None:
    # Two items only, so both are bound and nothing is related or drawn.
    planted: list[Item] = [("D1", "a", "b", True, 0, None, None),
                           ("D1", "f", "a", False, 0, None, None)]  # fmt: skip
    repo_id, _ = freeze(repo, home, planted)
    assert run(repo, home, seed_for("control", repo_id, 1))[0] == 0
    (t1,) = tasks(home, repo_id)
    # The segment: a now imports c and not b, and f no longer parses.
    (repo / "src" / "pkg" / "a.py").write_text("from pkg import c\n")
    (repo / "src" / "pkg" / "f.py").write_text("def broken(:\n")
    close_with(home, t1["task_id"], {"a", "f"}, "2026-01-05T10:00:00.000Z")
    buf = io.StringIO()
    assert task.last(repo, home, buf) == 0
    lines = buf.getvalue().splitlines()
    start = lines.index(task.BOUND.rstrip("\n"))
    assert sorted(lines[start + 1 : start + 3]) == [
        "  Does code in a import code in b? yes, now no",
        "  Does code in f import code in a? no, now unknown",
    ]


def test_an_item_listed_twice_in_the_binding_pool_is_refused(
    repo: Path, home: Path
) -> None:
    repo_id, version = freeze(repo, home)
    s = ledger(home)
    try:
        pool = s.probe_items_unexposed(repo_id, version)
    finally:
        s.close()
    assert len(task._pairs(pool)) == len(pool) == 30  # 6 modules, ordered pairs
    with pytest.raises(ValueError, match="listed twice"):
        task._pairs([*pool, pool[0]])


def test_a_validation_probed_item_leaves_the_binding_pool(
    repo: Path, home: Path
) -> None:
    # Two items: unprobed, both would be bound. The one a never-drawn probe asked
    # (task_id NULL) is no longer blind, so only the other is bound.
    planted: list[Item] = [("D1", "a", "b", True, 0, None, None),
                           ("D1", "f", "a", False, 0, None, None)]  # fmt: skip
    repo_id, version = freeze(repo, home, planted)
    s = ledger(home)
    try:
        asked, kept = s.probe_items_unexposed(repo_id, version)
        s.probe_response(item_id=asked["item_id"], task_id=None, ts=NOW, p=0.7,
                         seconds=1.0, lag_days=None, deadline_met=True,
                         truth_at_probe=True, truth_changed=False, head_sha=None,
                         tree_hash=None, dirty=None, extractor_version="x1-test",
                         ev=EV)  # fmt: skip
    finally:
        s.close()
    assert run(repo, home, seed_for("control", repo_id, 1))[0] == 0
    (t1,) = tasks(home, repo_id)
    roles = exposures(home, t1["task_id"])
    assert [r["item_id"] for r in roles["bound"]] == [kept["item_id"]]
    assert roles["related"] == roles["drawn_not_shown"] == []


# design 6.4: the start week, and design 6.2: the trailing touch window


def test_a_task_before_the_start_week_is_forced_control_and_silent(
    repo: Path, home: Path
) -> None:
    repo_id, version = freeze(repo, home, at=NOW)  # NOW is Monday of 2026-W02
    # Oracle: K_START = 6 weeks from START_GAP_WEEKS = 2 weeks after the set week.
    weeks = [f"2026-W{w:02d}" for w in range(4, 10)]
    key = hashlib.sha256(json.dumps([task.SEED, repo_id, "start"]).encode())
    week = random.Random(int(key.hexdigest(), 16)).choice(weeks)
    s = ledger(home)
    try:
        assert task.draw_start(s, repo_id, version, "2026-03-02T09:00:00Z") == week
        (start,) = s.repository_starts()
    finally:
        s.close()
    assert dict(start) == {"repo_id": repo_id, "map_version": version,
                           "drawn_ts": NOW, "start_week": week,
                           "candidates_json": json.dumps(weeks),
                           "seed_hash": key.hexdigest()}  # fmt: skip
    seed = seed_for("treatment", repo_id, 1)  # the arm draw says treatment
    code, out, consumed = run(repo, home, seed, "9\n" * 7)
    assert (code, out, consumed) == (0, CONTROL_OUT, 0)
    assert "W" not in out, "the text names a week"
    (first,) = tasks(home, repo_id)
    assert (first["arm"], first["forced_control"]) == ("control", 1)
    monday = dt.date.fromisocalendar(int(week[:4]), int(week[-2:]), 1)
    seed = seed_for("treatment", repo_id, 2)
    code, out, _ = run(repo, home, seed, "9\n" * 7, f"{monday}T09:00:00.000Z")
    assert out.endswith("Praxis recorded task 2 as a treatment task.\n")
    second = tasks(home, repo_id)[1]
    assert (second["arm"], second["forced_control"]) == ("treatment", 0)


def test_the_touch_window_moves_the_region(
    repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_id, _ = freeze(repo, home)
    # Two tasks touch d e f in November, one touches a b c the day before NOW. All
    # history: d e f 2/3, a b c 1/3. A 4-week window holds only the last: a b c 1/1.
    history = [("2025-11-03", {"d", "e", "f"}), ("2025-11-10", {"d", "e", "f"}),
               ("2026-01-04", {"a", "b", "c"})]  # fmt: skip
    for ordinal, (day, touched) in enumerate(history, 1):
        assert run(repo, home, 0, "9\n" * 7, f"{day}T09:00:00.000Z")[0] == 0
        tid = tasks(home, repo_id)[ordinal - 1]["task_id"]
        close_with(home, tid, touched, f"{day}T10:00:00.000Z")
    regions = []
    for weeks in (None, 4):
        monkeypatch.setattr(task, "TOUCH_WINDOW_WEEKS", weeks)
        assert run(repo, home, 0, "9\n" * 7)[0] == 0
        regions.append(json.loads(tasks(home, repo_id)[-1]["region_json"]))
    assert regions == [["d", "e", "f"], ["a", "b", "c"]]
    assert "TOUCH_WINDOW_WEEKS is 4 weeks" in task.window_assumption()
