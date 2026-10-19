"""The ledger, driven through a real Store on a real SQLite file under a temporary
$PRAXIS_HOME. Every writer is round-tripped; every CHECK, unique index and trigger is
shown refusing one row that a valid base row next to it proves is otherwise insertable;
the survival lookup is run on a planted duplicate; and the bytes of every file under
$PRAXIS_HOME are searched for canary text that the schema must have refused. Expected
values are written before each run.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest

from praxis import store as store_mod
from praxis.components import FIXED, ComponentMap, Rule, map_version, to_json
from praxis.schema import CLAIMS, SCHEMA_VERSION
from praxis.store import Evidence, Store, item_id

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

REPO = "0123456789abcdef"
PRODUCT = ("a", "b", "c", "d", "e")
SEED = "5" * 64
SHA = "c" * 40
EV = Evidence("full", ["session:s1:turn:0"], "test-v1", ["assumed"], [])
CANARY = "CANARYSTORE9f2e"


def make_map(source: str = "mechanical", extra: str = "") -> ComponentMap:
    product = [Rule(c, (f"src/{c}{extra}.py",)) for c in PRODUCT]
    rules = FIXED + tuple(product)
    return ComponentMap(REPO, source, rules, map_version(rules), ("a warning",))


CMAP = make_map()
V = CMAP.version


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    praxis_home = tmp_path / "praxis-home"
    monkeypatch.setenv("PRAXIS_HOME", str(praxis_home))
    return praxis_home


@pytest.fixture
def db(home: Path) -> Iterator[Store]:
    assert store_mod.home() == home
    ledger = Store(store_mod.home() / "ledger.db").open()
    try:
        yield ledger
    finally:
        ledger.close()


def some(row: sqlite3.Row | None) -> sqlite3.Row:
    assert row is not None
    return row


# fmt: off
def world(s: Store) -> list[str]:
    """A frozen map, four items, task T1 exposing the first, decision 1."""
    s.component_map_put(to_json(CMAP), "2026-10-19T00:00:00Z")
    s.component_map_freeze(REPO, V, "2026-10-19T00:00:01Z")
    rows = [("D1", "a", "b", True, 0, 2, True), ("D1", "b", "a", False, 0, None, None),
            ("D2", "a", "c", False, 0, None, None),
            ("reach", "c", "a", True, 0, None, None)]
    ids: list[str] = s.probe_items_put(
        repo_id=REPO, map_version=V, truth_ts="t0", extractor_version="x1-v1",
        rows=rows, ev=EV,
    )
    s.task_register(
        task_id="T1", repo_id=REPO, map_version=V, ordinal=1, ts="t1",
        arm="treatment", seed_hash=SEED, region=["a"], stratum="region",
        bound=[ids[0]], related=[], drawn=[],
    )
    s.touch_session_repo("s1", REPO, "t1")
    s.decision(
        session_id="s1", tool_use_id="tu1", tool_name="AskUserQuestion",
        prompt_id="p1", ts="t2", option_count=3, chosen_index=1,
        options_hash="e" * 64, ev=EV,
    )
    return ids


# -- round trips ---------------------------------------------------------------------


def test_component_map_round_trip_freeze_and_supersede(db: Store) -> None:
    assert db.component_map_frozen(REPO) is None
    assert db.component_map_put(to_json(CMAP), "t0") == V
    assert db.component_map_put(to_json(CMAP), "t9") == V  # same rules, one map
    with pytest.raises(ValueError, match="another source"):
        db.component_map_put(to_json(make_map(source="owner")), "t0")
    db.component_map_freeze(REPO, V, "t1")
    db.component_map_freeze(REPO, V, "t2")  # refreezing the frozen map: no change
    assert db.component_map_frozen(REPO) == to_json(CMAP)
    second = make_map(extra="2")
    db.component_map_put(to_json(second), "t3")
    with pytest.raises(ValueError, match="reason code"):
        db.component_map_freeze(REPO, second.version, "t4")
    db.component_map_freeze(REPO, second.version, "t4", reason="regrouped")
    assert db.component_map_frozen(REPO) == to_json(second)
    rows = {r["version"]: r for r in db.db.execute("SELECT * FROM component_maps")}
    assert (rows[V]["frozen_ts"], rows[V]["superseded_ts"]) == ("t1", "t4")
    assert rows[second.version]["reason"] == "regrouped"
    with pytest.raises(sqlite3.IntegrityError):  # a superseded map stays superseded
        db.component_map_freeze(REPO, V, "t5", reason="correction")
    with pytest.raises(KeyError):
        db.component_map_freeze(REPO, "f" * 16, "t6", reason="correction")


def test_recorder_writers_keep_the_new_pointers(db: Store) -> None:
    db.hook_event(
        session_id="s1", event="Stop", tool_name=None, tool_use_id=None, ts="t",
        duration_ms=1.5, outcome="ok", note=None, prompt_id="p1", turn_id=None,
        permission_mode="default", agent_id="ag", cli_version="2.1.303",
        head_sha=SHA,
    )
    db.hook_event(
        session_id="s1", event="Stop", tool_name=None, tool_use_id=None, ts="t",
        duration_ms=1.0, outcome="ok", note=None,
    )
    rows = db.hook_event_rows("s1")
    assert [r["head_sha"] for r in rows] == [SHA, None]
    assert [r["cli_version"] for r in rows] == ["2.1.303", None]
    db.agent_delta("s1", "tu1", "Edit", "t", [("f.py", "h1", 2)], prompt_id="p1")
    db.agent_delta("s1", "tu2", "Edit", "t", [("f.py", "h1", -1)])
    got = [tuple(r) for r in db.db.execute(
        "SELECT tool_use_id, delta, prompt_id, turn_id FROM agent_deltas"
        " ORDER BY id"
    )]
    assert got == [("tu1", 2, "p1", None), ("tu2", -1, None, None)]
    assert db.agent_multiset("s1")[0] == {"f.py": Counter({"h1": 1})}


def test_edges_scenarios_and_rules_round_trip(db: Store) -> None:
    world(db)
    db.file_edges(
        session_id="s1", repo_id=REPO, map_version=V, ts="t5",
        scanned={"src/a.py": "a", "src/b.py": "b", "src/c.py": "c"},
        edges=[("src/a.py", "b", 2, True), ("src/a.py", "c", 1, False)],
        extractor_version="x1-v1", ev=EV,
    )
    got = [(r["src_file"], r["src_component"], r["dst_component"], r["sites"],
            r["module_level"], r["claim_class"]) for r in db.edge_history(REPO)]
    assert got == [
        ("src/a.py", "a", "b", 2, 1, "derived"),
        ("src/a.py", "a", "c", 1, 0, "derived"),
        ("src/b.py", "b", None, None, None, "derived"),
        ("src/c.py", "c", None, None, None, "derived"),
    ]
    with pytest.raises(ValueError, match="twice"):
        db.file_edges(
            session_id="s1", repo_id=REPO, map_version=V, ts="t6",
            scanned={"src/a.py": "a"}, extractor_version="x1-v1", ev=EV,
            edges=[("src/a.py", "b", 1, True), ("src/a.py", "b", 1, True)],
        )
    with pytest.raises(KeyError):
        db.file_edges(
            session_id="s1", repo_id=REPO, map_version=V, ts="t7", scanned={},
            edges=[("src/z.py", "b", 1, True)], extractor_version="x1-v1", ev=EV,
        )
    db.boundary_rule_put(REPO, V, "a", "b", False, "t")
    db.boundary_rule_put(REPO, V, "b", "a", True, "t")
    rules = [(r["src_component"], r["allowed"]) for r in db.boundary_rules(REPO, V)]
    assert rules == [("a", 0), ("b", 1)]
    db.scenario_run(
        repo_id=REPO, map_version=V, ts="t", scenario_id="hook-edit",
        command_hash="0" * 16, outcome="ok", exit_code=0, duration_ms=12.0,
        components=["b", "a"], call_edges={("a", "b"): 3},
        extractor_version="x2-v1", ev=EV,
    )
    (run,) = db.db.execute("SELECT * FROM scenario_runs")
    assert json.loads(run["components_json"]) == ["a", "b"]
    assert json.loads(run["call_edges_json"]) == [["a", "b", 3]]


def test_task_lifecycle_and_item_consumption(db: Store) -> None:
    ids = world(db)
    assert [r["item_id"] for r in db.probe_items_unexposed(REPO, V)] == sorted(ids[1:])
    assert some(db.probe_item(ids[0]))["sites"] == 2
    assert some(db.task_open(REPO))["task_id"] == "T1"
    superseded = db.task_register(
        task_id="T2", repo_id=REPO, map_version=V, ordinal=2, ts="t3",
        arm="control", seed_hash=SEED, region=[], stratum="uniform",
        bound=[ids[1]], related=[ids[2]], drawn=[ids[3]],
    )
    assert superseded == "T1"
    assert [r["status"] for r in db.task_rows(REPO)] == ["superseded", "unbound"]
    assert db.probe_items_unexposed(REPO, V) == []
    roles = [(r["role"], r["truth"]) for r in db.exposures_of("T2")]
    assert roles == [("bound", 0), ("related", 0), ("drawn_not_shown", 1)]
    with pytest.raises(sqlite3.IntegrityError):  # closing needs a binding first
        db.task_close("T2", "t4")
    db.task_bind("T2", "s1", "p1", "t4")
    with pytest.raises(sqlite3.IntegrityError):  # set once
        db.task_bind("T2", "s2", "p2", "t5")
    db.forecast("T2", [("a", 0.7, "forecast", None), ("b", None, "forecast", None),
                       (None, 0.1, "prequestion", ids[1])], "t3", ev=EV)
    got = [(r["kind"], r["component"], r["p"]) for r in db.forecasts_of("T2")]
    assert got == [("forecast", "a", 0.7), ("forecast", "b", None),
                   ("prequestion", None, 0.1)]
    db.outcome("T2", [("a", True, None, 1, 0), ("b", False, False, None, None)],
               "t6", ev=EV)
    outcomes = [(r["component"], r["o_first"], r["o_final"], r["edges_added"])
                for r in db.outcomes_of("T2")]
    assert outcomes == [("a", 1, None, 1), ("b", 0, 0, None)]
    db.task_close("T2", "t7")
    assert db.task_open(REPO) is None
    assert some(db.task_last_closed(REPO))["task_id"] == "T2"
    with pytest.raises(sqlite3.IntegrityError):  # a closed task is not superseded
        db.task_void("T2", "superseded", "t8")
    db.task_void("T2", "hook_error", "t8")
    assert db.task_last_closed(REPO) is None
    assert db.task_count(REPO) == 2
    with pytest.raises(KeyError):
        db.task_close("T9", "t9")


def test_task_register_is_atomic_and_needs_the_frozen_map(db: Store) -> None:
    ids = world(db)
    fields: dict[str, Any] = {
        "repo_id": REPO, "ordinal": 2, "ts": "t3", "arm": "control",
        "seed_hash": SEED, "region": [], "stratum": "uniform", "related": [],
        "drawn": [],
    }
    with pytest.raises(ValueError, match="frozen map"):
        db.task_register(task_id="T2", map_version="f" * 16, bound=[], **fields)
    with pytest.raises(sqlite3.IntegrityError):  # an item exposed twice
        db.task_register(task_id="T2", map_version=V, bound=[ids[1]] * 2, **fields)
    assert [r["task_id"] for r in db.task_rows(REPO)] == ["T1"]
    # The supersede of T1 rolled back with the refused registration.
    assert some(db.task_open(REPO))["status"] == "unbound"


def test_probe_decision_and_other_responses_round_trip(db: Store) -> None:
    ids = world(db)
    common: dict[str, Any] = {
        "seconds": 4.0, "deadline_met": True, "truth_at_probe": True,
        "truth_changed": False, "head_sha": SHA, "tree_hash": None, "dirty": False,
        "extractor_version": "x1-v1", "ev": EV,
    }
    db.probe_response(item_id=ids[0], task_id="T1", ts="t9", p=0.9, lag_days=7.0,
                      **common)
    db.probe_response(item_id=ids[1], task_id=None, ts="t9", p=None, lag_days=None,
                      **common)
    got = [tuple(r) for r in db.db.execute(
        "SELECT p, skipped, lag_days, agent_attached FROM probe_responses"
        " ORDER BY id"
    )]
    assert got == [(0.9, 0, 7.0, 0), (None, 1, None, 0)]
    (pending,) = db.decisions_unprobed(REPO)
    assert (pending["option_count"], pending["chosen_index"]) == (3, 1)
    with pytest.raises(ValueError, match="outside"):
        db.decision_response(decision_id=pending["id"], ts="t", recalled_index=3,
                             lag_days=8.0, ev=EV)
    db.decision_response(decision_id=pending["id"], ts="t", recalled_index=None,
                         lag_days=8.0, ev=EV)
    assert db.decisions_unprobed(REPO) == []
    db.drift_response(repo_id=REPO, map_version=V, week="2026-W42", component="a",
                      said_changed=None, did_change=True, ts="t", ev=EV)
    db.divergence(session_id="s1", task_id="T1", ts="t", repo_id=REPO,
                  map_version=V, src_component="b", dst_component="a",
                  rule_allowed=False, noticed=None, ev=EV)
    db.knockout(repo_id=REPO, map_version=V, ts="t", component_knocked="c",
                scenario_id=None, components_named=["c", "d"], hit=True, ev=EV)
    db.pathfinder_run(repo_id=REPO, map_version=V, ts="t", n_components=5,
                      pfsim=None, pfsim_minus_chance=None, ev=EV)
    db.flag(ts="t", code="owner_answered_question", detail={"turns": 2},
            task_id="T1")
    expected = {
        "drift_responses": ("said_changed", None), "divergences": ("noticed", None),
        "knockouts": ("components_named_json", '["c", "d"]'),
        "pathfinder_runs": ("pfsim", None), "flags": ("detail_json", '{"turns": 2}'),
        "decision_responses": ("skipped", 1),
    }
    for table, (column, value) in expected.items():
        (row,) = db.db.execute(f"SELECT {column} FROM {table}")  # noqa: S608
        assert row[0] == value, table
    for table, claim in CLAIMS.items():
        sql = f"SELECT DISTINCT claim_class FROM {table}"  # noqa: S608
        assert {r[0] for r in db.db.execute(sql)} <= {claim}, table


# -- the readers the recorder, task, probe and report modules ask through ------------


def hook(s: Store, event: str, ts: str, **more: Any) -> None:
    row: dict[str, Any] = {"session_id": "s1", "event": event, "tool_name": None,
                           "tool_use_id": None, "ts": ts, "duration_ms": 1.0,
                           "outcome": "ok", "note": None}
    s.hook_event(**{**row, **more})


def test_recorder_readers_round_trip(db: Store) -> None:
    assert (db.session_base("s1"), db.last_stop_ts("s1")) == (None, None)
    assert (db.deltas_files_since("s1", None), db.head_sequence("s1")) == ([], [])
    assert (db.recorder_gaps("s1", ""), db.task_bound_to("s1")) == (0, None)
    hook(db, "SessionStart", "t1", head_sha=SHA)
    hook(db, "SessionStart", "t1", head_sha="d" * 40)  # the first baseline holds
    hook(db, "PreToolUse", "t2", tool_use_id="tu1", head_sha=SHA)
    hook(db, "Stop", "t3")
    hook(db, "Stop", "t5", outcome="unobservable")
    hook(db, "Stop", "t9", session_id="s2")
    db.agent_delta("s1", "tu1", "Edit", "t2", [("f.py", "h", 1), ("g.py", "h", 1)])
    db.agent_delta("s1", "tu2", "Edit", "t4", [("g.py", "h", 1), ("h.py", "h", 2)])
    db.agent_delta("s2", "tu3", "Edit", "t4", [("z.py", "h", 1)])
    db.pending_put("s1", "tu4", "t4", {})
    assert db.session_base("s1") == SHA
    assert db.last_stop_ts("s1") == "t5"
    assert db.deltas_files_since("s1", None) == ["f.py", "g.py", "h.py"]
    assert db.deltas_files_since("s1", "t3") == ["g.py", "h.py"]
    assert db.recorder_gaps("s1", "t3") == 2  # the unobservable Stop, the waiting Pre
    assert db.recorder_gaps("s1", "t6") == 0
    assert [tuple(r) for r in db.head_sequence("s1")] == [
        ("SessionStart", None, SHA), ("SessionStart", None, "d" * 40),
        ("PreToolUse", "tu1", SHA),
    ]
    world(db)
    db.task_bind("T1", "s1", "p1", "t6")
    assert some(db.task_bound_to("s1"))["task_id"] == "T1"
    assert db.task_bound_to("s2") is None


def test_the_session_reads_use_the_hook_events_index(db: Store) -> None:
    plan = " ".join(str(r[-1]) for r in db.db.execute(
        "EXPLAIN QUERY PLAN SELECT MAX(ts) FROM hook_events WHERE session_id = ?"
        " AND event = 'Stop'", ("s1",)))
    assert "hook_events_session" in plan


def test_segment_close_is_one_transaction(db: Store) -> None:
    world(db)
    rows = [("a", True, None, 1, 0), ("b", False, None, None, None)]
    with pytest.raises(sqlite3.IntegrityError):  # T1 is unbound: it cannot close
        db.segment_close("T1", rows, "t3", ev=EV)
    assert db.outcomes_of("T1") == []  # so its outcomes rolled back with it
    assert some(db.task_open(REPO))["status"] == "unbound"
    db.task_bind("T1", "s1", "p1", "t2")
    db.segment_close("T1", rows, "t3", ev=EV)
    assert [(r["component"], r["o_first"]) for r in db.outcomes_of("T1")] == [
        ("a", 1), ("b", 0)]
    assert some(db.task_last_closed(REPO))["closed_ts"] == "t3"


def test_task_probe_and_report_readers_round_trip(db: Store) -> None:
    ids = world(db)
    common: dict[str, Any] = {"repo_id": REPO, "map_version": V, "ts": "t", "ev": EV}
    db.divergence(**common, session_id="s1", task_id="T1", src_component="b",
                  dst_component="a", rule_allowed=False, noticed=None)
    db.divergence(**common, session_id="s1", task_id=None, src_component="c",
                  dst_component="a", rule_allowed=True, noticed=None)
    got = [(r["src_component"], r["rule_allowed"]) for r in db.divergences_of("T1")]
    assert got == [("b", 0)]
    db.flag(ts="t", code="late_prediction", detail={}, task_id="T1")
    db.flag(ts="t", code="owner_answered_question", detail={}, task_id="T1")
    db.flag(ts="t", code="agent_previewed", detail={}, repo_id=REPO)
    db.flag(ts="t", code="transcript_swept", detail={"decision_id": 1},
            session_id="s1")
    db.flag(ts="t", code="reversed", detail={}, session_id="s1")
    db.flag(ts="t", code="abandoned", detail={"decision_id": 7}, repo_id=REPO)
    assert db.tasks_flagged(["late_prediction", "agent_previewed"]) == {"T1"}
    assert db.tasks_flagged(["agent_previewed"]) == frozenset()
    assert db.decisions_flagged(["transcript_swept", "reversed"]) == {1}
    (due,) = db.exposures_unanswered(REPO, V)
    assert (due["item_id"], due["task_id"], due["role"], due["exposed_ts"]) == (
        ids[0], "T1", "bound", "t1")
    answer: dict[str, Any] = {
        "seconds": 1.0, "deadline_met": True, "truth_at_probe": True,
        "truth_changed": False, "head_sha": None, "tree_hash": None, "dirty": None,
        "extractor_version": "x1-v1", "ev": EV,
    }
    db.probe_response(item_id=ids[1], task_id=None, ts="t8", p=0.3, lag_days=None,
                      **answer)
    db.probe_response(item_id=ids[0], task_id="T1", ts="t7", p=0.9, lag_days=7.0,
                      **answer)
    assert db.exposures_unanswered(REPO, V) == []
    answered = [(r["item_id"], r["p"], r["role"], r["exposed_task"], r["relation"])
                for r in db.probe_responses_of(REPO, V)]
    assert answered == [(ids[0], 0.9, "bound", "T1", "D1"),
                        (ids[1], 0.3, None, None, "D1")]
    assert db.probe_responses_of(REPO, "f" * 16) == []
    assert sorted(tuple(r) for r in db.probe_items_d1(REPO, V)) == [
        ("a", "b", 1, "t0"), ("b", "a", 0, "t0")]
    assert db.drift_responses_for(REPO, V, "2026-W42") == []
    db.drift_response(**common, week="2026-W42", component="a", said_changed=True,
                      did_change=None)
    assert [r["component"] for r in db.drift_responses_for(REPO, V, "2026-W42")] == [
        "a"]
    assert db.drift_responses_for(REPO, V, "2026-W43") == []
    for ts, scenario in (("t2", "late"), ("t1", "early")):
        db.scenario_run(**common | {"ts": ts}, scenario_id=scenario,
                        command_hash="0" * 16, outcome="ok", exit_code=0,
                        duration_ms=1.0, components=["a"], call_edges={},
                        extractor_version="x2-v1")
    assert [r["scenario_id"] for r in db.scenario_runs_for(REPO, V)] == [
        "early", "late"]
# fmt: on


def test_reopen_skips_the_ddl_and_keeps_rows(home: Path) -> None:
    first = Store(home / "ledger.db").open()
    world(first)
    first.close()
    again = Store(home / "ledger.db").open()
    (version,) = again.db.execute("PRAGMA user_version").fetchone()
    assert version == SCHEMA_VERSION
    assert again.task_count(REPO) == 1
    again.close()


# fmt: off
def test_an_old_ledger_gains_the_pointers_and_loses_the_drill(home: Path) -> None:
    home.mkdir(parents=True)
    old = sqlite3.connect(home / "ledger.db")
    old.execute(
        "CREATE TABLE hook_events (id INTEGER PRIMARY KEY, session_id TEXT,"
        " event TEXT, tool_name TEXT, tool_use_id TEXT, ts TEXT NOT NULL,"
        " duration_ms REAL NOT NULL, outcome TEXT NOT NULL, note TEXT) STRICT"
    )
    old.execute(
        "INSERT INTO hook_events (ts, duration_ms, outcome) VALUES ('t', 1, 'ok')"
    )
    # The rejected drill's tables, with an owner-typed test command in repo_config.
    old.executescript(
        "CREATE TABLE repo_config (repo_id TEXT PRIMARY KEY, test_cmd TEXT NOT NULL,"
        " ts TEXT NOT NULL) STRICT;"
        "CREATE TABLE items (item_id TEXT PRIMARY KEY) STRICT;"
        "CREATE TABLE results (id INTEGER PRIMARY KEY,"
        " item_id TEXT NOT NULL REFERENCES items (item_id)) STRICT;"
        "INSERT INTO items VALUES ('i1'); INSERT INTO results VALUES (1, 'i1');"
    )
    old.execute("INSERT INTO repo_config VALUES (?, ?, 't')", (REPO, CANARY))
    old.commit()
    old.close()
    assert CANARY.encode() in (home / "ledger.db").read_bytes()
    ledger = Store(home / "ledger.db").open()
    tables = {r[0] for r in ledger.db.execute("SELECT name FROM sqlite_master")}
    assert not tables & {"repo_config", "items", "results"}
    stop: dict[str, Any] = {
        "session_id": "s", "event": "Stop", "tool_name": None, "tool_use_id": None,
        "ts": "t", "duration_ms": 1, "outcome": "ok", "note": None,
    }
    ledger.hook_event(**stop, head_sha=SHA)
    assert [r["head_sha"] for r in ledger.hook_event_rows()] == [None, SHA]
    with pytest.raises(sqlite3.IntegrityError):  # the added column keeps its CHECK
        ledger.hook_event(**stop, head_sha=CANARY)
    ledger.close()
    for path in (p for p in home.rglob("*") if p.is_file()):
        assert CANARY.encode() not in path.read_bytes(), path.name


# -- survival: duplicate is not one --------------------------------------------------


def test_survival_is_a_multiset_intersection(db: Store) -> None:
    # The agent added two identical lines (one delta of 2, as the recorder writes a
    # Counter difference); the probe tree holds three copies of that line.
    db.agent_delta("s1", "tu1", "Edit", "t", [("f.py", "dup", 2), ("f.py", "gone", 1)])
    db.agent_delta("s2", "tu2", "Edit", "t", [("f.py", "gone", -1), ("g.py", "mv", 1)])
    db.agent_delta("s2", "tu3", "Edit", "t", [("f.py", "mv", -1)])
    probe = Counter({"dup": 3, "gone": 1, "mv": 1, "owner": 4})
    assert db.survival(["s1"], probe) == Counter({"dup": 2, "gone": 1})
    # Across sessions the agent's own removal cancels its addition; a line moved out
    # of an owner's file is the agent's where it landed (per-file clip, as attribution).
    assert db.survival(["s1", "s2"], probe) == Counter({"dup": 2, "mv": 1})
    assert db.survival([], probe) == Counter()
    with pytest.raises(ValueError, match="twice"):
        db.survival(["s1", "s1"], probe)


# -- every CHECK, unique index and trigger refuses -----------------------------------


def ev(table: str) -> dict[str, object]:
    return {"claim_class": CLAIMS[table], "coverage": "full", "sources": '["s:1"]',
            "reducer_version": "v1", "assumptions": "[]", "warnings": "[]"}


def bases(ids: list[str]) -> dict[str, dict[str, object]]:
    m = {"repo_id": REPO, "map_version": V}
    new_item = item_id(REPO, V, "D1", "c", "d", 0)
    return {
        "hook_events": {"ts": "t", "duration_ms": 1.0, "outcome": "ok",
                        "head_sha": SHA},
        "component_maps": {"repo_id": REPO, "version": "1" * 16, "source": "owner",
                           "rules_json": '[["a", "a/*"]]', "warnings_json": "[]",
                           "created_ts": "t", "frozen_ts": None,
                           "superseded_ts": None, "reason": None},
        "boundary_rules": {**m, "src_component": "c", "dst_component": "d",
                           "allowed": 1, "ts": "t"},
        "file_edges": {**m, "session_id": "s1", "ts": "t", "src_file": "src/a.py",
                       "src_component": "a", "dst_component": "b", "sites": 1,
                       "module_level": 1, "extractor_version": "x1",
                       **ev("file_edges")},
        "scenario_runs": {**m, "scenario_id": "x", "command_hash": "0" * 16,
                          "ts": "t", "outcome": "failed", "exit_code": 1,
                          "duration_ms": 1.0, "components_json": "[]",
                          "call_edges_json": "[]", "extractor_version": "x2",
                          **ev("scenario_runs")},
        "tasks": {**m, "task_id": "T7", "ordinal": 7, "ts": "t", "arm": "control",
                  "seed_hash": SEED, "region_json": "[]", "stratum": "empty",
                  "status": "closed", "bound_session_id": "s1",
                  "bound_prompt_id": None, "bound_ts": "t", "closed_ts": "t",
                  "void_reason": None, "void_ts": None},
        "probe_items": {**m, "item_id": new_item, "relation": "D1",
                        "src_component": "c", "dst_component": "d", "truth": 1,
                        "truth_ts": "t", "extractor_version": "x1", "epoch": 0,
                        "sites": 1, "module_level": 0, **ev("probe_items")},
        "exposures": {"item_id": ids[3], "task_id": "T1", "ts": "t",
                      "role": "related"},
        "forecasts": {"task_id": "T1", "kind": "forecast", "component": "a",
                      "item_id": None, "p": 0.7, "ts": "t", **ev("forecasts")},
        "outcomes": {"task_id": "T1", "ts": "t", "component": "a", "o_first": 1,
                     "o_final": 1, "edges_added": 0, "edges_removed": 0,
                     **ev("outcomes")},
        "probe_responses": {"item_id": ids[0], "task_id": "T1", "ts": "t",
                            "p": 0.5, "skipped": 0, "seconds": 1.0,
                            "lag_days": 7.0, "deadline_met": 1,
                            "truth_at_probe": 1, "truth_changed": 0,
                            "agent_attached": 0, "head_sha": SHA, "tree_hash": SHA,
                            "dirty": 0, "extractor_version": "x1",
                            **ev("probe_responses")},
        "drift_responses": {**m, "week": "2026-W42", "component": "a",
                            "said_changed": 1, "did_change": 0, "ts": "t",
                            **ev("drift_responses")},
        "divergences": {**m, "session_id": "s1", "task_id": None, "ts": "t",
                        "src_component": "a", "dst_component": "b",
                        "rule_allowed": 0, "noticed": None, **ev("divergences")},
        "decisions": {"session_id": "s1", "tool_use_id": "tu9",
                      "tool_name": "ExitPlanMode", "prompt_id": None, "ts": "t",
                      "option_count": 2, "chosen_index": 0,
                      "options_hash": "a" * 64, **ev("decisions")},
        "decision_responses": {"decision_id": 1, "ts": "t", "recalled_index": 2,
                               "skipped": 0, "lag_days": 9.0,
                               **ev("decision_responses")},
        "knockouts": {**m, "ts": "t", "component_knocked": "a",
                      "scenario_id": None, "components_named_json": "[]",
                      "hit": 0, **ev("knockouts")},
        "pathfinder_runs": {**m, "ts": "t", "n_components": 5, "pfsim": 0.4,
                            "pfsim_minus_chance": 0.1, **ev("pathfinder_runs")},
        "flags": {"ts": "t", "repo_id": REPO, "session_id": None, "task_id": None,
                  "code": "abandoned", "detail_json": "{}"},
    }


Case = tuple[str, dict[str, object] | None]
TWICE = None  # the override that inserts the base row a second time
EVIDENCE: list[Case] = [("file_edges", {k: v}) for k, v in (
    ("claim_class", "comparative"), ("coverage", "no_baseline"), ("sources", "[]"),
    ("sources", '"s:1"'), ("sources", "not json"), ("reducer_version", ""),
    ("assumptions", "{}"), ("warnings", "nope"),
)]
CASES: list[Case] = [
    *EVIDENCE,
    ("hook_events", {"head_sha": CANARY}),
    ("component_maps", {"repo_id": "r1"}), ("component_maps", {"version": "v"}),
    ("component_maps", {"source": "judge"}),
    ("component_maps", {"rules_json": "[]"}),
    ("component_maps", {"warnings_json": "{}"}),
    ("component_maps", {"reason": "whim", "frozen_ts": "t", "superseded_ts": "t"}),
    ("component_maps", {"superseded_ts": "t"}),
    ("component_maps", {"version": V}),
    ("component_maps", {"frozen_ts": "t"}),  # a second current frozen map
    ("boundary_rules", {"allowed": 2}), ("boundary_rules", {"dst_component": "c"}),
    ("boundary_rules", {"map_version": "f" * 16}), ("boundary_rules", TWICE),
    ("file_edges", {"src_file": ""}), ("file_edges", {"sites": 0}),
    ("file_edges", {"module_level": 2}), ("file_edges", {"dst_component": None}),
    ("file_edges", {"dst_component": None, "sites": None}),
    ("file_edges", {"dst_component": "a"}), ("file_edges", TWICE),
    ("scenario_runs", {"command_hash": "zz"}),
    ("scenario_runs", {"outcome": "boom"}),
    ("scenario_runs", {"duration_ms": -1.0}),
    ("scenario_runs", {"components_json": "{}"}),
    ("scenario_runs", {"call_edges_json": "x"}),
    ("scenario_runs", {"outcome": "ok"}), ("scenario_runs", {"exit_code": 0}),
    ("scenario_runs", {"outcome": "timeout"}),
    ("scenario_runs", {"outcome": "not_python", "exit_code": None}),
    ("tasks", {"ordinal": 0}), ("tasks", {"arm": "both"}),
    ("tasks", {"seed_hash": "5" * 63}), ("tasks", {"region_json": "{}"}),
    ("tasks", {"stratum": "lucky"}), ("tasks", {"status": "done"}),
    ("tasks", {"status": "void", "void_reason": "bored", "void_ts": "t"}),
    ("tasks", {"bound_ts": None}), ("tasks", {"void_reason": "hook_error"}),
    ("tasks", {"status": "unbound"}), ("tasks", {"status": "bound"}),
    ("tasks", {"closed_ts": None}),
    ("tasks", {"status": "superseded", "closed_ts": None,
               "void_reason": "map_changed", "void_ts": "t"}),
    ("tasks", {"status": "void", "void_reason": "superseded", "void_ts": "t"}),
    ("tasks", {"ordinal": 1}),
    ("tasks", {"status": "unbound", "bound_session_id": None, "bound_ts": None,
               "closed_ts": None}),  # a second open task
    ("tasks", {"map_version": "f" * 16}),
    ("probe_items", {"item_id": CANARY}), ("probe_items", {"relation": "RT"}),
    ("probe_items", {"truth": 2}), ("probe_items", {"epoch": -1}),
    ("probe_items", {"sites": 0}), ("probe_items", {"module_level": 2}),
    ("probe_items", {"dst_component": "c"}),
    ("probe_items", {"module_level": None}),
    ("probe_items", {"truth": 0}), ("probe_items", {"relation": "reach"}),
    ("probe_items", TWICE),
    ("exposures", {"role": "shown"}), ("exposures", {"item_id": "f" * 16}),
    ("exposures", TWICE),
    ("forecasts", {"kind": "guess"}), ("forecasts", {"p": 0.2}),
    ("forecasts", {"component": None}), ("forecasts", {"kind": "prequestion"}),
    ("forecasts", {"kind": "prequestion", "component": None}),
    ("forecasts", {"kind": "prequestion", "component": None, "item_id": "f" * 16}),
    ("forecasts", {"task_id": "T9"}), ("forecasts", TWICE),
    ("outcomes", {"o_first": 2}), ("outcomes", {"o_final": 2}),
    ("outcomes", {"edges_added": -1}), ("outcomes", {"edges_removed": -1}),
    ("outcomes", {"edges_removed": None}), ("outcomes", TWICE),
    ("probe_responses", {"p": 0.4}), ("probe_responses", {"skipped": 2}),
    ("probe_responses", {"seconds": -1.0}),
    ("probe_responses", {"lag_days": -1.0}),
    ("probe_responses", {"deadline_met": 2}),
    ("probe_responses", {"truth_at_probe": 2}),
    ("probe_responses", {"truth_changed": 2}),
    ("probe_responses", {"agent_attached": 1}),
    ("probe_responses", {"head_sha": CANARY}),
    ("probe_responses", {"tree_hash": "c" * 41}),
    ("probe_responses", {"dirty": 2}),
    ("probe_responses", {"skipped": 1}), ("probe_responses", {"task_id": None}),
    ("drift_responses", {"week": "2026-42"}),
    ("drift_responses", {"said_changed": 2}),
    ("drift_responses", {"did_change": 2}), ("drift_responses", TWICE),
    ("divergences", {"rule_allowed": 2}), ("divergences", {"noticed": 2}),
    ("divergences", {"dst_component": "a"}),
    ("decisions", {"tool_name": "Bash"}), ("decisions", {"option_count": 0}),
    ("decisions", {"options_hash": CANARY}),
    ("decisions", {"options_hash": None}), ("decisions", {"chosen_index": 2}),
    ("decisions", {"option_count": None, "options_hash": None}),
    ("decisions", TWICE),
    ("decision_responses", {"recalled_index": -1}),
    ("decision_responses", {"skipped": 2}),
    ("decision_responses", {"lag_days": -1.0}),
    ("decision_responses", {"skipped": 1}),
    ("decision_responses", {"decision_id": 99}),
    ("knockouts", {"components_named_json": "{}"}), ("knockouts", {"hit": 2}),
    ("pathfinder_runs", {"n_components": 1}), ("pathfinder_runs", {"pfsim": 1.5}),
    ("flags", {"repo_id": CANARY}), ("flags", {"code": "whatever"}),
    ("flags", {"detail_json": "[]"}), ("flags", {"repo_id": None}),
]
# fmt: on


def insert(s: Store, table: str, row: dict[str, object]) -> None:
    marks = ", ".join("?" * len(row))
    sql = f"INSERT INTO {table} ({', '.join(row)}) VALUES ({marks})"  # noqa: S608
    s.db.execute(sql, list(row.values()))


@pytest.mark.parametrize(
    ("table", "override"), CASES, ids=[f"{t}-{o}" for t, o in CASES]
)
def test_the_database_refuses(
    db: Store, table: str, override: dict[str, object] | None
) -> None:
    base = bases(world(db))[table]
    db.db.execute("SAVEPOINT base")
    insert(db, table, base)  # the base row is valid, so the refusal is the override's
    if override is not None:
        db.db.execute("ROLLBACK TO base")
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, table, base if override is None else {**base, **override})


TRIGGERS = [
    "UPDATE probe_items SET truth = 0",
    "DELETE FROM forecasts",
    "UPDATE exposures SET role = 'bound'",
    "DELETE FROM tasks",
    "UPDATE tasks SET arm = 'control'",
    "UPDATE tasks SET status = 'unbound', bound_session_id = NULL, bound_ts = NULL",
    "UPDATE tasks SET bound_session_id = 's9'",
    "UPDATE component_maps SET rules_json = '[[\"x\"]]'",
    "UPDATE component_maps SET frozen_ts = 'later'",
    "DELETE FROM component_maps",
]


@pytest.mark.parametrize("sql", TRIGGERS)
def test_rows_are_append_only(db: Store, sql: str) -> None:
    world(db)
    db.forecast("T1", [("a", 0.5, "forecast", None)], "t", ev=EV)
    db.task_bind("T1", "s1", None, "t")
    with pytest.raises(sqlite3.IntegrityError):
        db.db.execute(sql)


def test_evidence_refuses_a_bare_string_before_the_database(db: Store) -> None:
    with pytest.raises(TypeError):
        Evidence("full", CANARY, "v1", [], [])
    world(db)
    empty = Evidence("full", [], "v1", [], [])
    with pytest.raises(sqlite3.IntegrityError):
        db.forecast("T1", [("a", 0.5, "forecast", None)], "t", ev=empty)


# -- no text reaches the bytes -------------------------------------------------------


# fmt: off
def test_refused_text_leaves_no_byte_under_praxis_home(home: Path) -> None:
    ledger = Store(home / "ledger.db").open()
    ids = world(ledger)
    second = ledger.component_map_put(to_json(make_map(extra="2")), "t")
    m: dict[str, Any] = {"repo_id": REPO, "map_version": V, "ts": "t", "ev": EV}
    attempts: list[Callable[[], object]] = [
        lambda: ledger.hook_event(
            session_id="s", event="Stop", tool_name=None, tool_use_id=None, ts="t",
            duration_ms=1, outcome="ok", note=None, head_sha=CANARY),
        lambda: ledger.decision(
            session_id="s1", tool_use_id="tu2", tool_name="AskUserQuestion",
            prompt_id=None, ts="t", option_count=2, chosen_index=None,
            options_hash=CANARY, ev=EV),
        lambda: ledger.task_register(
            task_id="T2", repo_id=REPO, map_version=V, ordinal=2, ts="t",
            arm="control", seed_hash=CANARY, region=[], stratum="empty", bound=[],
            related=[], drawn=[]),
        lambda: ledger.task_void("T1", CANARY, "t"),
        lambda: ledger.flag(ts="t", code=CANARY, detail={}, repo_id=REPO),
        lambda: ledger.drift_response(
            **m, week=CANARY, component="a", said_changed=True, did_change=True),
        lambda: ledger.component_map_freeze(REPO, second, "t", reason=CANARY),
        lambda: ledger.scenario_run(
            **m, scenario_id="s", command_hash=CANARY, outcome="ok", exit_code=0,
            duration_ms=1.0, components=[], call_edges={}, extractor_version="x2"),
        lambda: ledger.probe_response(
            item_id=ids[1], task_id=None, ts="t", p=None, seconds=1.0,
            lag_days=None, deadline_met=True, truth_at_probe=True,
            truth_changed=False, head_sha=None, tree_hash=CANARY, dirty=None,
            extractor_version="x1", ev=EV),
    ]
    refused = 0
    for attempt in attempts:
        try:
            attempt()
        except sqlite3.IntegrityError:
            refused += 1
    ledger.close()
    assert refused == len(attempts)
    files = sorted(p for p in home.rglob("*") if p.is_file())
    assert files, "nothing was written"
    for path in files:
        assert CANARY.encode() not in path.read_bytes(), path.name
# fmt: on
