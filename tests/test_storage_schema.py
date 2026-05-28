"""Tests for the v0.2 storage schema (PRAXIS_V0_2_SPEC.md Section 14).

US-001 only verifies that ProfileStore creates the new tables and indexes
with the right columns, CHECK constraints, and FK references. Migration
behavior (drop daily_consolidations, schema_version=2, one-shot detection,
backup) lives in US-002/003/004.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from praxis.storage import profile_store as profile_store_mod
from praxis.storage.profile_store import MigrationError, ProfileStore, resolve_home


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(ProfileStore(home=resolve_home()).db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---- moments ----------------------------------------------------------------


def test_moments_table_columns(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "moments")
    assert cols["moment_id"]["pk"] == 1
    assert cols["session_stable_id"]["notnull"] == 1
    assert cols["dim_key"]["notnull"] == 1
    assert cols["turn_index"]["type"] == "INTEGER"
    assert cols["quoted_excerpt"]["notnull"] == 1
    assert cols["why_it_lost_score"]["notnull"] == 1
    assert cols["suggested_alternative"]["notnull"] == 1
    assert cols["dollar_impact_estimate"]["type"] == "REAL"
    assert cols["dollar_impact_estimate"]["notnull"] == 0
    assert cols["minutes_impact_estimate"]["type"] == "INTEGER"
    assert cols["minutes_impact_estimate"]["notnull"] == 0
    assert cols["severity"]["notnull"] == 1
    assert cols["created_at"]["notnull"] == 1
    assert cols["redacted"]["notnull"] == 1
    assert cols["redacted"]["dflt_value"] == "0"


def test_moments_severity_check_rejects_unknown_value(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO moments (moment_id, session_stable_id, dim_key, turn_index, "
                "quoted_excerpt, why_it_lost_score, suggested_alternative, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("m1", "s1", "verification", 0, "q", "why", "alt", "catastrophic", "2026-05-26"),
            )


def test_moments_severity_check_accepts_valid_values(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        for i, sev in enumerate(("minor", "moderate", "major")):
            conn.execute(
                "INSERT INTO moments (moment_id, session_stable_id, dim_key, turn_index, "
                "quoted_excerpt, why_it_lost_score, suggested_alternative, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"m{i}", "s1", "verification", i, "q", "why", "alt", sev, "2026-05-26"),
            )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) AS c FROM moments").fetchone()["c"]
        assert count == 3


def test_moments_indexes_exist(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'moments'"
            ).fetchall()
        }
    assert "idx_moments_session" in names
    assert "idx_moments_created" in names


# ---- tasks ------------------------------------------------------------------


def test_tasks_table_columns(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "tasks")
    assert cols["task_id"]["pk"] == 1
    assert cols["label"]["notnull"] == 1
    assert cols["task_type"]["notnull"] == 1
    assert cols["project_hint"]["notnull"] == 0
    assert cols["started_at"]["notnull"] == 1
    assert cols["ended_at"]["notnull"] == 1
    assert cols["session_count"]["type"] == "INTEGER"
    assert cols["session_count"]["notnull"] == 1
    assert cols["total_cost_estimate_usd"]["type"] == "REAL"
    assert cols["label_source"]["notnull"] == 1


def test_tasks_label_source_check_rejects_unknown_value(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks (task_id, label, task_type, started_at, ended_at, "
                "session_count, label_source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t1", "label", "debugging", "2026-05-26", "2026-05-26", 1, "manual"),
            )


def test_tasks_label_source_check_accepts_llm_and_fallback(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        for i, src in enumerate(("llm", "fallback")):
            conn.execute(
                "INSERT INTO tasks (task_id, label, task_type, started_at, ended_at, "
                "session_count, label_source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"t{i}", "label", "debugging", "2026-05-26", "2026-05-26", 1, src),
            )
        conn.commit()


# ---- task_members -----------------------------------------------------------


def test_task_members_has_composite_pk_and_fk_to_tasks(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "task_members")
        fks = conn.execute("PRAGMA foreign_key_list(task_members)").fetchall()
    assert cols["task_id"]["pk"] == 1
    assert cols["session_stable_id"]["pk"] == 2
    matching = [
        f for f in fks
        if f["table"] == "tasks" and f["from"] == "task_id" and f["to"] == "task_id"
    ]
    assert len(matching) == 1, "task_members must have FK task_id -> tasks(task_id)"
    assert matching[0]["on_delete"] == "CASCADE"


def test_task_members_cascade_delete_enforced_when_pragma_on(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO tasks (task_id, label, task_type, started_at, ended_at, "
            "session_count, label_source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t-cascade", "label", "debugging", "2026-05-26", "2026-05-26", 2, "llm"),
        )
        conn.execute(
            "INSERT INTO task_members (task_id, session_stable_id) VALUES (?, ?)",
            ("t-cascade", "s1"),
        )
        conn.execute(
            "INSERT INTO task_members (task_id, session_stable_id) VALUES (?, ?)",
            ("t-cascade", "s2"),
        )
        conn.commit()
        conn.execute("DELETE FROM tasks WHERE task_id = ?", ("t-cascade",))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM task_members WHERE task_id = ?", ("t-cascade",)
        ).fetchone()["c"]
    assert remaining == 0


# ---- weekly_digests ---------------------------------------------------------


def test_weekly_digests_table_columns_and_fk(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "weekly_digests")
        fks = conn.execute("PRAGMA foreign_key_list(weekly_digests)").fetchall()
    assert cols["week_iso"]["pk"] == 1
    assert cols["generated_at"]["notnull"] == 1
    assert cols["trajectory_label"]["notnull"] == 1
    assert cols["trajectory_headline"]["notnull"] == 1
    assert cols["headline_moment_id"]["notnull"] == 0
    assert cols["cost_total_usd"]["type"] == "REAL"
    assert cols["cost_baseline_usd"]["type"] == "REAL"
    assert cols["snapshot_json"]["notnull"] == 1
    assert cols["html_path"]["notnull"] == 0
    matching = [
        f for f in fks
        if f["table"] == "moments"
        and f["from"] == "headline_moment_id"
        and f["to"] == "moment_id"
    ]
    assert len(matching) == 1, "weekly_digests must reference moments(moment_id)"


# ---- follow_ups -------------------------------------------------------------


def test_follow_ups_table_columns(tmp_home):
    # Post-US-002 the table is rebuilt with `id INTEGER PRIMARY KEY
    # AUTOINCREMENT` (so multiple commitments per week can coexist) and
    # week_iso becomes a plain NOT NULL column.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "follow_ups")
    assert cols["id"]["pk"] == 1
    assert cols["id"]["type"] == "INTEGER"
    assert cols["week_iso"]["pk"] == 0
    assert cols["week_iso"]["notnull"] == 1
    assert cols["dim_key"]["notnull"] == 1
    assert cols["commitment_text"]["notnull"] == 1
    assert cols["target_metric"]["notnull"] == 1
    assert cols["baseline_value"]["notnull"] == 1
    assert cols["baseline_value"]["type"] == "REAL"
    assert cols["measured_value"]["type"] == "REAL"
    assert cols["measured_value"]["notnull"] == 0
    assert cols["outcome"]["notnull"] == 1


def test_follow_ups_outcome_check_rejects_unknown_value(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, target_metric, "
                "baseline_value, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-W21", "verification", "ask before running", "verification_rate", 5.0, "tbd"),
            )


def test_follow_ups_outcome_check_accepts_all_four_values(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        for i, outcome in enumerate(("improved", "unchanged", "worse", "pending")):
            conn.execute(
                "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, target_metric, "
                "baseline_value, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"2026-W{21 + i:02d}",
                    "verification",
                    "ask before running",
                    "verification_rate",
                    5.0,
                    outcome,
                ),
            )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) AS c FROM follow_ups").fetchone()["c"]
        assert count == 4


# ---- US-002: new columns + partial-unique active-commitment index ----------


def _insert_active_pending(conn, week_iso: str, commitment: str = "ask first") -> int:
    """Insert one row with outcome='pending' and superseded_by NULL, return its id."""
    cur = conn.execute(
        "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, target_metric, "
        "baseline_value, outcome) VALUES (?, ?, ?, ?, ?, ?)",
        (week_iso, "verification", commitment, "verification_rate", 0.4, "pending"),
    )
    return cur.lastrowid


def test_follow_ups_has_user_chosen_column(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "follow_ups")
    assert cols["user_chosen"]["type"] == "INTEGER"
    assert cols["user_chosen"]["notnull"] == 1
    assert cols["user_chosen"]["dflt_value"] == "0"


def test_follow_ups_has_display_text_column(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "follow_ups")
    assert cols["display_text"]["type"] == "TEXT"
    assert cols["display_text"]["notnull"] == 0


def test_follow_ups_has_superseded_by_column_with_self_fk(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "follow_ups")
        fks = conn.execute("PRAGMA foreign_key_list(follow_ups)").fetchall()
    assert cols["superseded_by"]["type"] == "INTEGER"
    assert cols["superseded_by"]["notnull"] == 0
    matching = [
        f for f in fks
        if f["table"] == "follow_ups"
        and f["from"] == "superseded_by"
        and f["to"] == "id"
    ]
    assert len(matching) == 1, "superseded_by must FK-reference follow_ups(id)"


def test_active_commitment_partial_unique_index_exists(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        row = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_follow_ups_one_active_per_week'"
        ).fetchone()
    assert row is not None, "partial-unique index must exist"
    sql = row["sql"].lower()
    assert "unique" in sql
    assert "where" in sql
    assert "outcome" in sql and "pending" in sql
    assert "superseded_by" in sql


def test_second_active_pending_row_for_same_week_raises_integrity_error(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        _insert_active_pending(conn, "2026-W21")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_active_pending(conn, "2026-W21", commitment="second active try")


def test_second_active_row_succeeds_after_first_is_superseded(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        first_id = _insert_active_pending(conn, "2026-W21")
        conn.commit()
        # Mark the first row as superseded so the partial index predicate
        # no longer matches it; a second active row can now coexist.
        conn.execute(
            "UPDATE follow_ups SET superseded_by = ? WHERE id = ?",
            (first_id + 99, first_id),
        )
        conn.commit()
        new_id = _insert_active_pending(conn, "2026-W21", commitment="replacement")
        conn.commit()
        rows = conn.execute(
            "SELECT id, outcome, superseded_by FROM follow_ups "
            "WHERE week_iso = ? ORDER BY id ASC",
            ("2026-W21",),
        ).fetchall()
    assert [r["id"] for r in rows] == [first_id, new_id]
    assert rows[0]["superseded_by"] == first_id + 99
    assert rows[1]["superseded_by"] is None


def test_distinct_outcomes_for_same_week_do_not_conflict(tmp_home):
    # The partial index only restricts pending+unsuperseded rows. Once a row
    # is closed (outcome != 'pending') the next week's pending row coexists.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        conn.execute(
            "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, target_metric, "
            "baseline_value, outcome) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-W21", "verification", "ask first", "verification_rate", 0.4, "improved"),
        )
        _insert_active_pending(conn, "2026-W21", commitment="next attempt")
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM follow_ups WHERE week_iso = ?", ("2026-W21",)
        ).fetchone()["c"]
    assert count == 2


def test_existing_follow_ups_rows_preserved_through_migration(tmp_home):
    """A v0.2 follow_ups row written before US-002 should survive the rebuild
    with its values intact and the three new columns defaulted."""
    # Seed a v0.1-shaped DB so the v0.2 migration runs to create follow_ups
    # via SCHEMA, then we seed one row, then the US-002 migration recreates
    # the table. We can't go through ProfileStore for the seed (it would
    # already have applied US-002), so we open the DB by hand to insert,
    # then reopen via ProfileStore to trigger the runner.
    home = tmp_home / ".praxis"
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "profile.db"
    # First open: applies v0.2 SCHEMA + US-002 migration.
    ProfileStore(home=resolve_home())
    # Roll the table back to v0.2 shape so we can simulate a row that pre-
    # dates US-002, then re-trigger the runner.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_follow_ups_one_active_per_week")
        conn.execute("DROP TABLE follow_ups")
        conn.execute(
            "CREATE TABLE follow_ups ("
            "week_iso TEXT PRIMARY KEY, dim_key TEXT NOT NULL, "
            "commitment_text TEXT NOT NULL, target_metric TEXT NOT NULL, "
            "baseline_value REAL NOT NULL, measured_value REAL, "
            "outcome TEXT NOT NULL CHECK (outcome IN ('improved','unchanged','worse','pending')))"
        )
        conn.execute(
            "INSERT INTO follow_ups VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-W14", "verification", "old commitment", "verification_rate",
             0.4, 0.7, "improved"),
        )
        # Remove the schema_migrations row so the runner re-applies US-002.
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            ("001_follow_ups_active_commitment.sql",),
        )
        conn.commit()
    finally:
        conn.close()
    # Reopen: US-002 migration re-runs against the seeded v0.2 row.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        row = conn.execute(
            "SELECT week_iso, dim_key, commitment_text, target_metric, "
            "baseline_value, measured_value, outcome, "
            "user_chosen, display_text, superseded_by "
            "FROM follow_ups WHERE week_iso = ?",
            ("2026-W14",),
        ).fetchone()
    assert row is not None
    assert row["dim_key"] == "verification"
    assert row["commitment_text"] == "old commitment"
    assert row["target_metric"] == "verification_rate"
    assert row["baseline_value"] == 0.4
    assert row["measured_value"] == 0.7
    assert row["outcome"] == "improved"
    # New columns get default values.
    assert row["user_chosen"] == 0
    assert row["display_text"] is None
    assert row["superseded_by"] is None


# ---- US-002: drop daily_consolidations, preserve session_scores and run_log -

# v0.1 schema literal, used to seed a pre-migration DB. Kept inline (not
# imported) so this test still asserts the v0.2 migration even if the v0.1
# DDL is later removed from the live code path.
_V0_1_SCHEMA = """
CREATE TABLE session_scores (
    stable_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    started_at TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    overall REAL NOT NULL,
    dimension_scores_json TEXT NOT NULL,
    heuristic_scores_json TEXT NOT NULL,
    judge_result_json TEXT,
    features_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    judge_model TEXT
);
CREATE INDEX idx_session_started_at ON session_scores(started_at);
CREATE INDEX idx_session_provider ON session_scores(provider);

CREATE TABLE daily_consolidations (
    consolidation_date TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    coaching_json TEXT NOT NULL,
    sessions_in_window INTEGER NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE run_log (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    sessions_seen INTEGER NOT NULL,
    sessions_new INTEGER NOT NULL,
    notes TEXT
);
"""


def _seed_v0_1_db(tmp_home) -> dict[str, list[dict]]:
    """Build a v0.1 profile.db at the path ProfileStore would open. Returns
    the seeded rows keyed by table for later comparison."""
    db_path = tmp_home / ".praxis" / "profile.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    seeded = {
        "session_scores": [
            {
                "stable_id": "claude:abc:1",
                "provider": "claude",
                "started_at": "2026-05-01T10:00:00+00:00",
                "scored_at": "2026-05-01T10:05:00+00:00",
                "overall": 7.2,
                "dimension_scores_json": "{}",
                "heuristic_scores_json": "{}",
                "judge_result_json": None,
                "features_json": "{}",
                "source_path": "/tmp/abc.jsonl",
                "judge_model": None,
            }
        ],
        "daily_consolidations": [
            {
                "consolidation_date": "2026-05-01",
                "snapshot_json": "{}",
                "coaching_json": "{}",
                "sessions_in_window": 1,
                "generated_at": "2026-05-01T18:30:00+00:00",
            }
        ],
        "run_log": [
            {
                "run_at": "2026-05-01T18:30:00+00:00",
                "kind": "full",
                "sessions_seen": 1,
                "sessions_new": 1,
                "notes": "v0.1 row",
            }
        ],
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_V0_1_SCHEMA)
        for row in seeded["session_scores"]:
            conn.execute(
                "INSERT INTO session_scores (stable_id, provider, started_at, scored_at, "
                "overall, dimension_scores_json, heuristic_scores_json, judge_result_json, "
                "features_json, source_path, judge_model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row.values()),
            )
        for row in seeded["daily_consolidations"]:
            conn.execute(
                "INSERT INTO daily_consolidations VALUES (?, ?, ?, ?, ?)",
                tuple(row.values()),
            )
        for row in seeded["run_log"]:
            conn.execute(
                "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                tuple(row.values()),
            )
        conn.commit()
    finally:
        conn.close()
    return seeded


def test_daily_consolidations_dropped_after_v0_2_open(tmp_home):
    _seed_v0_1_db(tmp_home)
    # Triggers the migration via SCHEMA -> executescript.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daily_consolidations'"
        ).fetchone()
    assert present is None


def test_session_scores_rows_preserved_across_v0_2_migration(tmp_home):
    seeded = _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM session_scores").fetchall()]
    assert len(rows) == len(seeded["session_scores"])
    assert rows[0]["stable_id"] == seeded["session_scores"][0]["stable_id"]
    assert rows[0]["overall"] == seeded["session_scores"][0]["overall"]


def test_session_scores_schema_unchanged_across_v0_2_migration(tmp_home):
    """v0.3 (US-029): session_scores gains a ``judge_pass`` column and a
    composite (stable_id, judge_pass) primary key so pass-1 and pass-2 rows
    coexist. v0.3.1 adds ``signals_json`` for the weekly-bucketed trajectory
    (spec section 7). The v0.1 ``heuristic_scores_json`` column is also
    dropped as part of the table recreation."""
    _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "session_scores")
    assert set(cols.keys()) == {
        "stable_id", "provider", "started_at", "scored_at", "overall",
        "dimension_scores_json", "judge_result_json",
        "features_json", "source_path", "judge_model", "judge_pass",
        "signals_json",
    }
    # Composite PK on (stable_id, judge_pass): pk indices reflect column order.
    assert cols["stable_id"]["pk"] == 1
    assert cols["judge_pass"]["pk"] == 2
    assert cols["judge_pass"]["type"] == "INTEGER"
    assert cols["judge_pass"]["notnull"] == 1


def test_run_log_rows_preserved_across_v0_2_migration(tmp_home):
    seeded = _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        # US-003 adds a schema_version='2' marker row during migration;
        # filter it out so the assertion still expresses "v0.1 rows survive".
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM run_log WHERE kind != 'schema_version' "
                "ORDER BY run_id ASC"
            ).fetchall()
        ]
    assert len(rows) == len(seeded["run_log"])
    assert rows[0]["notes"] == "v0.1 row"
    assert rows[0]["kind"] == "full"


def test_run_log_schema_unchanged_across_v0_2_migration(tmp_home):
    _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "run_log")
    assert set(cols.keys()) == {
        "run_id", "run_at", "kind", "sessions_seen", "sessions_new", "notes",
    }
    assert cols["run_id"]["pk"] == 1


def test_drop_is_idempotent_on_fresh_v0_2_db(tmp_home):
    # No v0.1 seed: the DB starts empty and the SCHEMA's DROP TABLE IF EXISTS
    # must not raise on a fresh install.
    ProfileStore(home=resolve_home())
    # A second open is also a no-op.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daily_consolidations'"
        ).fetchone()
    assert present is None


# ---- US-003: schema_version=2 marker + one-shot migration -------------------


def _schema_version_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM run_log WHERE kind = 'schema_version' AND notes = '3'"
    ).fetchall()


def test_fresh_db_records_schema_version_2_in_run_log(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = _schema_version_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "schema_version"
    assert row["notes"] == "3"
    assert row["sessions_seen"] == 0
    assert row["sessions_new"] == 0
    assert row["run_at"]  # ISO timestamp, non-empty


def test_v0_1_migration_records_schema_version_2_marker(tmp_home):
    _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = _schema_version_rows(conn)
    assert len(rows) == 1
    assert rows[0]["notes"] == "3"


def test_reopen_when_marker_present_is_a_no_op(tmp_home):
    # First open creates v0.2 schema and marker.
    ProfileStore(home=resolve_home())
    db_path = ProfileStore(home=resolve_home()).db_path
    # Drop a v0.2 table by hand. If a subsequent open re-executed SCHEMA,
    # CREATE TABLE IF NOT EXISTS would resurrect it.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE moments")
        conn.commit()
    finally:
        conn.close()
    # Reopen. The marker is present, so SCHEMA must not run.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'moments'"
        ).fetchone()
    assert present is None, "reopen with marker present must not execute DDL"


def test_repeated_opens_do_not_duplicate_schema_version_marker(tmp_home):
    for _ in range(5):
        ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = _schema_version_rows(conn)
    assert len(rows) == 1


def test_migration_detected_from_schema_not_config(tmp_home, monkeypatch):
    # No PRAXIS_* config flags, no config files: detection must come from
    # the on-disk DB alone. We deliberately clear any incidental env vars
    # that future code might key off and assert the marker is still written.
    for var in ("PRAXIS_SCHEMA_VERSION", "PRAXIS_MIGRATE", "PRAXIS_FORCE_MIGRATE"):
        monkeypatch.delenv(var, raising=False)
    _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = _schema_version_rows(conn)
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daily_consolidations'"
        ).fetchone()
    assert len(rows) == 1
    assert present is None  # v0.1 detection + drop happened with no config input


def test_marker_absent_on_v0_1_db_triggers_migration(tmp_home):
    # Seed a v0.1 DB and confirm the marker is absent before ProfileStore opens.
    _seed_v0_1_db(tmp_home)
    db_path = tmp_home / ".praxis" / "profile.db"
    conn = sqlite3.connect(db_path)
    try:
        rows_before = conn.execute(
            "SELECT 1 FROM run_log WHERE kind = 'schema_version'"
        ).fetchall()
    finally:
        conn.close()
    assert rows_before == []
    # Now open via ProfileStore: marker absent means SCHEMA runs.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        # New v0.2 tables now exist (proof DDL ran).
        moments = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'moments'"
        ).fetchone()
        rows_after = _schema_version_rows(conn)
    assert moments is not None
    assert len(rows_after) == 1


# ---- US-004: backup before migration + non-zero exit on failure -------------


def _list_backups(tmp_home) -> list:
    return sorted((tmp_home / ".praxis").glob("profile.db.backup-*"))


def test_backup_created_before_v0_1_to_v0_2_migration(tmp_home):
    _seed_v0_1_db(tmp_home)
    assert _list_backups(tmp_home) == []
    ProfileStore(home=resolve_home())
    backups = _list_backups(tmp_home)
    assert len(backups) == 1, "exactly one backup file should appear"
    # The backup must hold the v0.1 state: daily_consolidations table still
    # present and seeded row preserved.
    bconn = sqlite3.connect(backups[0])
    try:
        tables = {
            r[0]
            for r in bconn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        consolidation_rows = bconn.execute(
            "SELECT consolidation_date FROM daily_consolidations"
        ).fetchall()
    finally:
        bconn.close()
    assert "daily_consolidations" in tables
    assert consolidation_rows == [("2026-05-01",)]


def test_backup_filename_is_timestamped(tmp_home):
    _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    backups = _list_backups(tmp_home)
    assert len(backups) == 1
    # Format: profile.db.backup-YYYYMMDDTHHMMSS<microseconds>Z
    assert re.fullmatch(
        r"profile\.db\.backup-\d{8}T\d{6}\d+Z", backups[0].name
    ), f"unexpected backup name: {backups[0].name}"


def test_no_backup_when_marker_already_present(tmp_home):
    # First open: fresh DB, no pre-existing file, so no backup expected.
    ProfileStore(home=resolve_home())
    assert _list_backups(tmp_home) == []
    # Second open: marker present, migration is a no-op, no backup.
    ProfileStore(home=resolve_home())
    assert _list_backups(tmp_home) == []


def test_no_backup_on_fresh_db_install(tmp_home):
    # Fresh install: profile.db does not exist before ProfileStore() runs,
    # so there is no live DB to back up.
    db_path = tmp_home / ".praxis" / "profile.db"
    assert not db_path.exists()
    ProfileStore(home=resolve_home())
    assert _list_backups(tmp_home) == []


def test_migration_failure_raises_migration_error(tmp_home, monkeypatch):
    _seed_v0_1_db(tmp_home)

    def fail(self, conn):
        raise sqlite3.OperationalError("simulated DDL failure")

    monkeypatch.setattr(ProfileStore, "_apply_v2_schema", fail)
    with pytest.raises(MigrationError):
        ProfileStore(home=resolve_home())


def test_migration_failure_message_contains_absolute_backup_path(tmp_home, monkeypatch):
    _seed_v0_1_db(tmp_home)

    def fail(self, conn):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(ProfileStore, "_apply_v2_schema", fail)
    with pytest.raises(MigrationError) as exc_info:
        ProfileStore(home=resolve_home())
    backups = _list_backups(tmp_home)
    assert len(backups) == 1
    abs_backup = str(backups[0].resolve())
    msg = str(exc_info.value)
    assert abs_backup in msg
    # Sanity: the path embedded in the message is an absolute path.
    assert abs_backup.startswith("/")


def test_db_restored_from_backup_on_migration_failure(tmp_home, monkeypatch):
    seeded = _seed_v0_1_db(tmp_home)
    db_path = tmp_home / ".praxis" / "profile.db"

    def fail(self, conn):
        # Simulate partial progress: create one table successfully, then fail.
        # The DB on disk now has a v0.2 table mixed with v0.1 tables, which
        # is exactly the half-migrated state US-004 forbids.
        conn.execute("CREATE TABLE moments (moment_id TEXT PRIMARY KEY)")
        conn.commit()
        raise sqlite3.OperationalError("aborting mid-migration")

    monkeypatch.setattr(ProfileStore, "_apply_v2_schema", fail)
    with pytest.raises(MigrationError):
        ProfileStore(home=resolve_home())

    # DB should be back to the v0.1 state: daily_consolidations present,
    # moments absent, seeded row intact.
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        consolidations = conn.execute(
            "SELECT consolidation_date FROM daily_consolidations"
        ).fetchall()
    finally:
        conn.close()
    assert "daily_consolidations" in tables
    assert "moments" not in tables
    assert consolidations == [(seeded["daily_consolidations"][0]["consolidation_date"],)]


def test_backup_happens_before_ddl_runs(tmp_home, monkeypatch):
    # Verify ordering: the backup file must exist by the time _apply_v2_schema
    # is called. We assert this from inside the patched apply method.
    _seed_v0_1_db(tmp_home)
    seen = {"backup_existed_at_apply_time": False}

    def check_backup_then_fail(self, conn):
        backups = _list_backups(tmp_home)
        seen["backup_existed_at_apply_time"] = len(backups) == 1
        raise sqlite3.OperationalError("stop after backup check")

    monkeypatch.setattr(ProfileStore, "_apply_v2_schema", check_backup_then_fail)
    with pytest.raises(MigrationError):
        ProfileStore(home=resolve_home())
    assert seen["backup_existed_at_apply_time"], (
        "backup must exist before _apply_v2_schema is invoked"
    )


def test_failed_migration_then_retry_succeeds(tmp_home, monkeypatch):
    # After a failed migration the DB is restored to v0.1 state; opening
    # ProfileStore() again (with no patched failure) must complete migration.
    _seed_v0_1_db(tmp_home)

    def fail_once(self, conn):
        raise sqlite3.OperationalError("first attempt fails")

    monkeypatch.setattr(ProfileStore, "_apply_v2_schema", fail_once)
    with pytest.raises(MigrationError):
        ProfileStore(home=resolve_home())

    monkeypatch.undo()
    # Retry: migration should now succeed and the marker should be set.
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = _schema_version_rows(conn)
        moments = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'moments'"
        ).fetchone()
        daily = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daily_consolidations'"
        ).fetchone()
    assert len(rows) == 1
    assert moments is not None
    assert daily is None


def test_migration_error_chains_original_exception(tmp_home, monkeypatch):
    _seed_v0_1_db(tmp_home)
    original = sqlite3.OperationalError("root cause")

    def fail(self, conn):
        raise original

    monkeypatch.setattr(ProfileStore, "_apply_v2_schema", fail)
    with pytest.raises(MigrationError) as exc_info:
        ProfileStore(home=resolve_home())
    assert exc_info.value.__cause__ is original


def test_migration_error_when_no_backup_makes_message_explicit(tmp_home, monkeypatch):
    # Fresh DB (no live file): if DDL fails, there is no backup to restore.
    # The message should still be coherent and identify the no-backup case.
    db_path = tmp_home / ".praxis" / "profile.db"
    assert not db_path.exists()

    def fail(self, conn):
        raise sqlite3.OperationalError("ddl boom")

    monkeypatch.setattr(ProfileStore, "_apply_v2_schema", fail)
    with pytest.raises(MigrationError) as exc_info:
        ProfileStore(home=resolve_home())
    msg = str(exc_info.value)
    assert "no backup" in msg.lower()
    # And of course no backup file was created.
    assert _list_backups(tmp_home) == []


def test_migration_error_is_runtime_error_subclass():
    # Documenting the type for callers that catch broad RuntimeError.
    assert issubclass(MigrationError, RuntimeError)
    # Sanity: importing from the storage package exposes it too.
    from praxis.storage import MigrationError as Exported

    assert Exported is MigrationError


def test_profile_store_module_exports_migration_error():
    # Bind through the imported module to keep linters happy and to verify
    # the symbol is reachable via praxis.storage.profile_store.
    assert profile_store_mod.MigrationError is MigrationError


# ---- US-003: session_reflections table -------------------------------------


def _seed_follow_up_id(conn: sqlite3.Connection, week_iso: str = "2026-W21") -> int:
    """Insert one follow_ups row and return its id, for use as FK target."""
    cur = conn.execute(
        "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, target_metric, "
        "baseline_value, outcome) VALUES (?, ?, ?, ?, ?, ?)",
        (week_iso, "verification", "ask first", "verification_rate", 0.4, "pending"),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_session_reflections_table_columns(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "session_reflections")
    assert cols["id"]["pk"] == 1
    assert cols["id"]["type"] == "INTEGER"
    assert cols["session_stable_id"]["notnull"] == 1
    assert cols["session_stable_id"]["type"] == "TEXT"
    assert cols["follow_up_id"]["notnull"] == 1
    assert cols["follow_up_id"]["type"] == "INTEGER"
    assert cols["self_report"]["notnull"] == 1
    assert cols["self_report"]["type"] == "TEXT"
    assert cols["note"]["type"] == "TEXT"
    assert cols["note"]["notnull"] == 0
    assert cols["created_at"]["notnull"] == 1
    assert cols["created_at"]["type"] == "TEXT"


def test_session_reflections_has_fk_to_follow_ups(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fks = conn.execute(
            "PRAGMA foreign_key_list(session_reflections)"
        ).fetchall()
    matching = [
        f for f in fks
        if f["table"] == "follow_ups"
        and f["from"] == "follow_up_id"
        and f["to"] == "id"
    ]
    assert len(matching) == 1, "session_reflections must FK-reference follow_ups(id)"


def test_session_reflections_index_exists(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        row = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_reflections_follow_up'"
        ).fetchone()
    assert row is not None, "idx_reflections_follow_up must exist"
    assert "follow_up_id" in row["sql"]


def test_session_reflections_self_report_check_rejects_unknown_value(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fid = _seed_follow_up_id(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_reflections "
                "(session_stable_id, follow_up_id, self_report, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("s1", fid, "maybe", None, "2026-05-26T10:00:00+00:00"),
            )


def test_session_reflections_self_report_check_accepts_all_four_values(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fid = _seed_follow_up_id(conn)
        conn.commit()
        for i, val in enumerate(("yes", "no", "partial", "skip")):
            conn.execute(
                "INSERT INTO session_reflections "
                "(session_stable_id, follow_up_id, self_report, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"s{i}", fid, val, None, "2026-05-26T10:00:00+00:00"),
            )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM session_reflections"
        ).fetchone()["c"]
    assert count == 4


def test_session_reflections_note_can_be_null(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fid = _seed_follow_up_id(conn)
        conn.commit()
        conn.execute(
            "INSERT INTO session_reflections "
            "(session_stable_id, follow_up_id, self_report, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s-null-note", fid, "yes", None, "2026-05-26T10:00:00+00:00"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT note FROM session_reflections WHERE session_stable_id = ?",
            ("s-null-note",),
        ).fetchone()
    assert row is not None
    assert row["note"] is None


def test_insert_session_reflection_helper_exists_on_profile_store():
    # The acceptance criterion requires "exposed on profile_store.py" so the
    # caller can write reflections without hand-rolling SQL.
    assert hasattr(ProfileStore, "insert_session_reflection")
    assert callable(ProfileStore.insert_session_reflection)


def test_insert_session_reflection_writes_row_and_returns_id(tmp_home):
    store = ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fid = _seed_follow_up_id(conn)
        conn.commit()
    row_id = store.insert_session_reflection(
        session_stable_id="claude:abc:1",
        follow_up_id=fid,
        self_report="yes",
        note="felt focused",
    )
    assert isinstance(row_id, int) and row_id > 0
    with _open_db() as conn:
        row = conn.execute(
            "SELECT session_stable_id, follow_up_id, self_report, note "
            "FROM session_reflections WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["session_stable_id"] == "claude:abc:1"
    assert row["follow_up_id"] == fid
    assert row["self_report"] == "yes"
    assert row["note"] == "felt focused"


def test_insert_session_reflection_writes_iso_8601_utc_timestamp(tmp_home):
    store = ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fid = _seed_follow_up_id(conn)
        conn.commit()
    row_id = store.insert_session_reflection(
        session_stable_id="claude:abc:1",
        follow_up_id=fid,
        self_report="partial",
    )
    with _open_db() as conn:
        created_at = conn.execute(
            "SELECT created_at FROM session_reflections WHERE id = ?",
            (row_id,),
        ).fetchone()["created_at"]
    # ISO-8601 with a UTC offset marker (Python's datetime.isoformat() default
    # for a tz-aware UTC datetime produces a "+00:00" suffix).
    assert "T" in created_at
    assert created_at.endswith("+00:00")
    # Round-trip parses to a tz-aware UTC datetime.
    parsed = datetime.fromisoformat(created_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_insert_session_reflection_note_defaults_to_none(tmp_home):
    store = ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fid = _seed_follow_up_id(conn)
        conn.commit()
    row_id = store.insert_session_reflection(
        session_stable_id="claude:abc:1",
        follow_up_id=fid,
        self_report="skip",
    )
    with _open_db() as conn:
        row = conn.execute(
            "SELECT note FROM session_reflections WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["note"] is None


def test_insert_session_reflection_rejects_invalid_self_report(tmp_home):
    store = ProfileStore(home=resolve_home())
    with _open_db() as conn:
        fid = _seed_follow_up_id(conn)
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_session_reflection(
            session_stable_id="claude:abc:1",
            follow_up_id=fid,
            self_report="maybe",
        )
