"""Tests for the SQL-file migration runner (US-001).

The runner reads numbered ``*.sql`` files from
``praxis/storage/migrations/`` (configurable per-test via monkeypatching
``ProfileStore._migrations_dir``) and applies each pending file in lexical
order, wrapping the body + the ``schema_migrations`` insert in one
transaction so failures rollback cleanly and the next init re-attempts the
same version.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from praxis.storage.profile_store import ProfileStore, resolve_home


def _patch_migrations_dir(monkeypatch, path: Path) -> None:
    """Point ProfileStore._migrations_dir at ``path`` for the duration of one test."""
    monkeypatch.setattr(
        ProfileStore,
        "_migrations_dir",
        classmethod(lambda cls: path),
    )


def _open_db(store: ProfileStore) -> sqlite3.Connection:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _write_migration(migrations_dir: Path, name: str, body: str) -> Path:
    migrations_dir.mkdir(parents=True, exist_ok=True)
    path = migrations_dir / name
    path.write_text(body)
    return path


# ---- schema_migrations table -------------------------------------------------


def test_schema_migrations_table_created_on_first_init(tmp_home, tmp_path, monkeypatch):
    _patch_migrations_dir(monkeypatch, tmp_path / "migrations")
    store = ProfileStore(home=resolve_home())
    with _open_db(store) as conn:
        cols = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
        }
    assert "version" in cols
    assert "applied_at" in cols
    assert cols["version"]["type"] == "TEXT"
    assert cols["version"]["pk"] == 1
    assert cols["applied_at"]["type"] == "TEXT"
    assert cols["applied_at"]["notnull"] == 1


def test_schema_migrations_table_persists_across_reopens(tmp_home, tmp_path, monkeypatch):
    _patch_migrations_dir(monkeypatch, tmp_path / "migrations")
    ProfileStore(home=resolve_home())
    ProfileStore(home=resolve_home())
    with _open_db(ProfileStore(home=resolve_home())) as conn:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
    assert present is not None


# ---- lexical-order application ----------------------------------------------


def test_pending_migrations_applied_in_lexical_order(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "002_second.sql", "CREATE TABLE second (x INTEGER);")
    _write_migration(migrations_dir, "001_first.sql", "CREATE TABLE first (x INTEGER);")
    _write_migration(migrations_dir, "003_third.sql", "CREATE TABLE third (x INTEGER);")
    _patch_migrations_dir(monkeypatch, migrations_dir)

    store = ProfileStore(home=resolve_home())
    with _open_db(store) as conn:
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY applied_at ASC"
        ).fetchall()
    assert [r["version"] for r in rows] == ["001_first.sql", "002_second.sql", "003_third.sql"]
    with _open_db(store) as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"first", "second", "third"}.issubset(tables)


def test_migration_records_inserted_with_iso_timestamp(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "001_init.sql", "CREATE TABLE init (x INTEGER);")
    _patch_migrations_dir(monkeypatch, migrations_dir)

    store = ProfileStore(home=resolve_home())
    with _open_db(store) as conn:
        row = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchone()
    assert row["version"] == "001_init.sql"
    # ISO-8601 with timezone marker (Python's datetime.isoformat() default).
    assert "T" in row["applied_at"]
    assert row["applied_at"].endswith("+00:00")


# ---- idempotency: second init does not re-run -------------------------------


def test_second_init_does_not_re_run_applied_migrations(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "001_init.sql", "CREATE TABLE once_only (x INTEGER);")
    _patch_migrations_dir(monkeypatch, migrations_dir)

    ProfileStore(home=resolve_home())
    # A second open would re-trigger the migration if the runner did not
    # consult schema_migrations; re-running would fail with "table already
    # exists" because the migration does not use IF NOT EXISTS.
    ProfileStore(home=resolve_home())

    with _open_db(ProfileStore(home=resolve_home())) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM schema_migrations WHERE version = '001_init.sql'"
        ).fetchone()[0]
    assert count == 1


def test_repeated_opens_do_not_duplicate_marker_rows(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "001_init.sql", "CREATE TABLE keep_one (x INTEGER);")
    _patch_migrations_dir(monkeypatch, migrations_dir)

    for _ in range(4):
        ProfileStore(home=resolve_home())

    with _open_db(ProfileStore(home=resolve_home())) as conn:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert [r["version"] for r in rows] == ["001_init.sql"]


def test_new_migration_added_later_is_applied_on_next_init(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "001_init.sql", "CREATE TABLE first (x INTEGER);")
    _patch_migrations_dir(monkeypatch, migrations_dir)

    ProfileStore(home=resolve_home())

    # Now author adds a new migration file...
    _write_migration(migrations_dir, "002_later.sql", "CREATE TABLE later (x INTEGER);")
    ProfileStore(home=resolve_home())

    with _open_db(ProfileStore(home=resolve_home())) as conn:
        versions = {
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert versions == {"001_init.sql", "002_later.sql"}
    assert {"first", "later"}.issubset(tables)


# ---- transactional rollback on failure --------------------------------------


def test_failing_migration_rolls_back_and_leaves_marker_absent(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    # The first statement creates a side-effect table; the second statement
    # fails because the same table is created again without IF NOT EXISTS.
    # Atomic semantics require both to be rolled back.
    _write_migration(
        migrations_dir,
        "001_partial.sql",
        "CREATE TABLE will_rollback (x INTEGER);\nCREATE TABLE will_rollback (x INTEGER);",
    )
    _patch_migrations_dir(monkeypatch, migrations_dir)

    with pytest.raises(sqlite3.OperationalError):
        ProfileStore(home=resolve_home())

    db_path = tmp_home / ".praxis" / "profile.db"
    conn = sqlite3.connect(db_path)
    try:
        marker = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='001_partial.sql'"
        ).fetchone()[0]
        side_effect = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='will_rollback'"
        ).fetchone()
    finally:
        conn.close()
    assert marker == 0
    assert side_effect is None, "rolled-back migration must leave no DDL side effects"


def test_failed_migration_is_re_attempted_on_next_init(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    bad_sql = "CREATE TABLE temp (x INTEGER);\nCREATE TABLE temp (x INTEGER);"
    sql_path = _write_migration(migrations_dir, "001_fix_me.sql", bad_sql)
    _patch_migrations_dir(monkeypatch, migrations_dir)

    with pytest.raises(sqlite3.OperationalError):
        ProfileStore(home=resolve_home())

    # Author fixes the migration and re-runs.
    sql_path.write_text("CREATE TABLE temp (x INTEGER);")
    ProfileStore(home=resolve_home())

    with _open_db(ProfileStore(home=resolve_home())) as conn:
        rows = conn.execute(
            "SELECT version FROM schema_migrations WHERE version='001_fix_me.sql'"
        ).fetchall()
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='temp'"
        ).fetchone()
    assert len(rows) == 1
    assert present is not None


def test_failure_in_later_migration_does_not_undo_earlier_ones(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "001_ok.sql", "CREATE TABLE ok (x INTEGER);")
    _write_migration(
        migrations_dir,
        "002_bad.sql",
        "CREATE TABLE bad (x INTEGER);\nCREATE TABLE bad (x INTEGER);",
    )
    _patch_migrations_dir(monkeypatch, migrations_dir)

    with pytest.raises(sqlite3.OperationalError):
        ProfileStore(home=resolve_home())

    db_path = tmp_home / ".praxis" / "profile.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        versions = {
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    # ok migration committed; bad migration rolled back fully.
    assert "001_ok.sql" in versions
    assert "002_bad.sql" not in versions
    assert "ok" in tables
    assert "bad" not in tables


# ---- empty / missing directory cases ----------------------------------------


def test_missing_migrations_directory_is_a_noop(tmp_home, tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist"
    assert not missing.exists()
    _patch_migrations_dir(monkeypatch, missing)
    # ProfileStore() must succeed even when the migrations dir is absent;
    # only schema_migrations should be created.
    store = ProfileStore(home=resolve_home())
    with _open_db(store) as conn:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert rows == []


def test_empty_migrations_directory_creates_table_but_no_rows(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _patch_migrations_dir(monkeypatch, migrations_dir)
    store = ProfileStore(home=resolve_home())
    with _open_db(store) as conn:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
    assert rows == []
    assert present is not None


def test_non_sql_files_in_migrations_dir_are_ignored(tmp_home, tmp_path, monkeypatch):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "README.md").write_text("notes for the migration set")
    (migrations_dir / "001_init.sql").write_text("CREATE TABLE init (x INTEGER);")
    (migrations_dir / "ignore.txt").write_text("not a migration")
    _patch_migrations_dir(monkeypatch, migrations_dir)

    store = ProfileStore(home=resolve_home())
    with _open_db(store) as conn:
        versions = [
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]
    assert versions == ["001_init.sql"]
