"""Tests for the v0.2 storage schema (PRAXIS_V0_2_SPEC.md Section 14).

US-001 only verifies that ProfileStore creates the new tables and indexes
with the right columns, CHECK constraints, and FK references. Migration
behavior (drop daily_consolidations, schema_version=2, one-shot detection,
backup) lives in US-002/003/004.
"""
from __future__ import annotations

import sqlite3

import pytest

from praxis.storage.profile_store import ProfileStore, resolve_home


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
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "follow_ups")
    assert cols["week_iso"]["pk"] == 1
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
    _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        cols = _table_columns(conn, "session_scores")
    assert set(cols.keys()) == {
        "stable_id", "provider", "started_at", "scored_at", "overall",
        "dimension_scores_json", "heuristic_scores_json", "judge_result_json",
        "features_json", "source_path", "judge_model",
    }
    assert cols["stable_id"]["pk"] == 1


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
        "SELECT * FROM run_log WHERE kind = 'schema_version' AND notes = '2'"
    ).fetchall()


def test_fresh_db_records_schema_version_2_in_run_log(tmp_home):
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = _schema_version_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "schema_version"
    assert row["notes"] == "2"
    assert row["sessions_seen"] == 0
    assert row["sessions_new"] == 0
    assert row["run_at"]  # ISO timestamp, non-empty


def test_v0_1_migration_records_schema_version_2_marker(tmp_home):
    _seed_v0_1_db(tmp_home)
    ProfileStore(home=resolve_home())
    with _open_db() as conn:
        rows = _schema_version_rows(conn)
    assert len(rows) == 1
    assert rows[0]["notes"] == "2"


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
