"""Design 6.4's start weeks in the ledger: the repository_starts writer and readers, the
draw in task.draw_start, and the forced_control column an older ledger gains, all on a
real Store under a temporary $PRAXIS_HOME. The refusals of each CHECK are in
test_store.py beside the other tables'. Expected values are written before each run.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import sqlite3
from typing import TYPE_CHECKING

import pytest

from praxis import task
from praxis.components import FIXED, ComponentMap, Rule, map_version, to_json
from praxis.store import Store

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

REPO = "0123456789abcdef"
RULES = FIXED + tuple(Rule(c, (f"src/{c}.py",)) for c in "abcde")
CMAP = ComponentMap(REPO, "mechanical", RULES, map_version(RULES), ())
SET_AT = "2026-12-21T09:00:00.000Z"  # Monday of 2026-W52; 2026 has 53 ISO weeks


def ledger(home: Path) -> Store:
    s = Store(home / "ledger.db").open()
    s.component_map_put(to_json(CMAP), SET_AT)
    s.component_map_freeze(REPO, CMAP.version, SET_AT)
    return s


def test_the_start_is_drawn_once_across_a_year_end(tmp_path: Path) -> None:
    s = ledger(tmp_path)
    try:
        assert s.repository_start(REPO) is None
        assert s.repository_starts() == []
        # 2 weeks after 2026-W52 is 2027-W01: 2026-W53 is skipped by the gap.
        weeks = [f"2027-W{w:02d}" for w in range(1, 7)]
        key = hashlib.sha256(json.dumps([task.SEED, REPO, "start"]).encode())
        week = random.Random(int(key.hexdigest(), 16)).choice(weeks)
        assert task.draw_start(s, REPO, CMAP.version, SET_AT) == week
        assert task.draw_start(s, REPO, CMAP.version, "2027-06-07T00:00:00Z") == week
        (row,) = s.repository_starts()
        assert dict(row) == {"repo_id": REPO, "map_version": CMAP.version,
                             "drawn_ts": SET_AT, "start_week": week,
                             "candidates_json": json.dumps(weeks),
                             "seed_hash": key.hexdigest()}  # fmt: skip
        assert dict(s.repository_start(REPO) or {}) == dict(row)
        assert task.week_of(dt.datetime(2026, 12, 28, tzinfo=dt.UTC)) == "2026-W53"
        again = {"repo_id": REPO, "map_version": CMAP.version, "drawn_ts": "t",
                 "start_week": week, "seed_hash": "0" * 64}  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):  # one start per repository
            s.repository_start_put(candidates=weeks, **again)
        with pytest.raises(ValueError, match="twice"):
            s.repository_start_put(candidates=[week, week], **again)
        for sql in ("UPDATE repository_starts SET start_week = start_week",
                    "DELETE FROM repository_starts"):  # fmt: skip
            with pytest.raises(sqlite3.IntegrityError):
                s.db.execute(sql)
    finally:
        s.close()


def test_an_old_ledger_gains_forced_control_and_its_trigger(tmp_path: Path) -> None:
    s = ledger(tmp_path)
    s.task_register(
        region=[], bound=[], related=[], drawn=[], task_id="T1", repo_id=REPO,
        map_version=CMAP.version, ordinal=1, ts="t1", arm="control",
        seed_hash="5" * 64, stratum="empty",
    )  # fmt: skip
    # A ledger from before the column: its trigger names no forced_control.
    s.db.execute("DROP TRIGGER tasks_update_never")
    s.db.execute("ALTER TABLE tasks DROP COLUMN forced_control")
    s.db.execute(
        "CREATE TRIGGER tasks_update_never BEFORE UPDATE OF task_id, repo_id,"
        " map_version, ordinal, ts, arm, seed_hash, region_json, stratum ON tasks"
        " BEGIN SELECT RAISE(ABORT, 'tasks: update refused'); END"
    )
    s.db.execute("PRAGMA user_version = 0")
    s.close()
    again = Store(tmp_path / "ledger.db").open()
    try:
        (row,) = again.task_rows(REPO)
        assert (row["task_id"], row["forced_control"]) == ("T1", None)
        with pytest.raises(sqlite3.IntegrityError):
            again.db.execute("UPDATE tasks SET forced_control = 1")
        with pytest.raises(sqlite3.IntegrityError):
            again.db.execute("UPDATE tasks SET arm = 'treatment'")
    finally:
        again.close()
