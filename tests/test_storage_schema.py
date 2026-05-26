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
