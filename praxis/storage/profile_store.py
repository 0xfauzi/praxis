"""Persistent profile store.

Tracks every session score across all-time so we can:
  - Avoid re-scoring sessions we've already seen
  - Surface trends ("you've improved 1.4 points on planning over 30 days")
  - Run daily consolidation that compares today to baseline
  - Track which coaching the user has already seen, so we don't repeat ourselves

Schema is intentionally simple — three tables, no migrations needed for v1.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> datetime:
    """Tz-aware UTC now. Wraps datetime.now(timezone.utc) for terseness."""
    return datetime.now(timezone.utc)

from praxis.follow_up import FollowUp, Outcome
from praxis.models import Moment, compute_moment_id
from praxis.redactor import redact_secrets
from praxis.scoring.aggregate import ProfileSnapshot, SessionScore


class MigrationError(RuntimeError):
    """Raised when the v0.2 schema migration fails. The message includes the
    absolute path of the pre-migration backup so the user can recover."""


def resolve_home() -> Path:
    """Resolve the scorecard home, honoring PRAXIS_HOME for tests/sandboxing."""
    override = os.environ.get("PRAXIS_HOME")
    if override:
        return Path(override)
    return Path.home() / ".praxis"


# Module-level default for back-compat with code that imports it directly.
# Resolved once at import time. Pass an explicit `home=` to ProfileStore or
# set PRAXIS_HOME before import to override.
DEFAULT_HOME = resolve_home()


SCHEMA = """
CREATE TABLE IF NOT EXISTS session_scores (
    stable_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    started_at TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    overall REAL NOT NULL,
    dimension_scores_json TEXT NOT NULL,
    judge_result_json TEXT,
    features_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    judge_model TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_started_at ON session_scores(started_at);
CREATE INDEX IF NOT EXISTS idx_session_provider ON session_scores(provider);

DROP TABLE IF EXISTS daily_consolidations;

CREATE TABLE IF NOT EXISTS run_log (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    sessions_seen INTEGER NOT NULL,
    sessions_new INTEGER NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS moments (
    moment_id TEXT PRIMARY KEY,
    session_stable_id TEXT NOT NULL,
    dim_key TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    quoted_excerpt TEXT NOT NULL,
    why_it_lost_score TEXT NOT NULL,
    suggested_alternative TEXT NOT NULL,
    dollar_impact_estimate REAL,
    minutes_impact_estimate INTEGER,
    severity TEXT NOT NULL CHECK (severity IN ('minor','moderate','major')),
    created_at TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_moments_session ON moments(session_stable_id);
CREATE INDEX IF NOT EXISTS idx_moments_created ON moments(created_at);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    task_type TEXT NOT NULL,
    project_hint TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    session_count INTEGER NOT NULL,
    total_cost_estimate_usd REAL,
    label_source TEXT NOT NULL CHECK (label_source IN ('llm','fallback'))
);

CREATE TABLE IF NOT EXISTS task_members (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    session_stable_id TEXT NOT NULL,
    PRIMARY KEY (task_id, session_stable_id)
);

CREATE TABLE IF NOT EXISTS weekly_digests (
    week_iso TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    trajectory_label TEXT NOT NULL,
    trajectory_headline TEXT NOT NULL,
    headline_moment_id TEXT REFERENCES moments(moment_id),
    cost_total_usd REAL,
    cost_baseline_usd REAL,
    snapshot_json TEXT NOT NULL,
    html_path TEXT
);

CREATE TABLE IF NOT EXISTS follow_ups (
    week_iso TEXT PRIMARY KEY,
    dim_key TEXT NOT NULL,
    commitment_text TEXT NOT NULL,
    target_metric TEXT NOT NULL,
    baseline_value REAL NOT NULL,
    measured_value REAL,
    outcome TEXT NOT NULL CHECK (outcome IN ('improved','unchanged','worse','pending'))
);
"""


class ProfileStore:
    def __init__(self, home: Path | None = None):
        # Re-resolve at construction time so callers that set
        # PRAXIS_HOME after import (CLI, tests) get the override.
        self.home = home or resolve_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "profile.db"
        self._init_schema()

    def _init_schema(self) -> None:
        # Check existence before opening a connection: sqlite3.connect creates
        # the file as a side effect, which would hide whether this was a fresh
        # install or an upgrade.
        db_existed = self.db_path.exists()
        if db_existed:
            with self._conn() as conn:
                if self._has_schema_v2_marker(conn):
                    return

        # Migration needed (either fresh DB or v0.1 DB without the marker).
        # Per spec Appendix A.7: back up the live DB before any DDL, and on
        # failure restore from backup so the DB is never half-migrated.
        backup_path = self._backup_db_if_exists()
        try:
            with self._conn() as conn:
                self._apply_v2_schema(conn)
                self._mark_schema_v2(conn)
        except Exception as exc:
            if backup_path is not None:
                self._restore_db_from_backup(backup_path)
            raise MigrationError(
                self._migration_failure_message(backup_path, exc)
            ) from exc

    def _apply_v2_schema(self, conn: sqlite3.Connection) -> None:
        # Wrapped in a method so tests can monkeypatch it to inject failures
        # without having to corrupt the SCHEMA constant.
        conn.executescript(SCHEMA)

    def _backup_db_if_exists(self) -> Path | None:
        if not self.db_path.exists():
            return None
        ts = _utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.db_path.parent / f"profile.db.backup-{ts}"
        shutil.copy2(self.db_path, backup_path)
        return backup_path

    def _restore_db_from_backup(self, backup_path: Path) -> None:
        shutil.copy2(backup_path, self.db_path)

    @staticmethod
    def _migration_failure_message(backup_path: Path | None, exc: Exception) -> str:
        if backup_path is None:
            return (
                f"v0.2 schema migration failed (no backup made; fresh DB): {exc}"
            )
        return (
            f"v0.2 schema migration failed: {exc}\n"
            f"Database has been restored from backup at: {backup_path.resolve()}"
        )

    @staticmethod
    def _has_schema_v2_marker(conn: sqlite3.Connection) -> bool:
        # Detection is from existing schema state, not from a config flag:
        # fresh DBs have no run_log table at all, and v0.1 DBs have run_log
        # without a schema_version row. Either case means migration is needed.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'run_log'"
        )
        if cur.fetchone() is None:
            return False
        cur = conn.execute(
            "SELECT 1 FROM run_log WHERE kind = 'schema_version' AND notes = '2' LIMIT 1"
        )
        return cur.fetchone() is not None

    @staticmethod
    def _mark_schema_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (_utcnow().isoformat(), "schema_version", 0, 0, "2"),
        )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- session scores --------------------------------------------------

    def has_session(self, stable_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT 1 FROM session_scores WHERE stable_id = ?", (stable_id,)
            )
            return cur.fetchone() is not None

    def save_session_score(self, score: SessionScore) -> None:
        judge_json = None
        judge_model = None
        if score.judge_result is not None:
            judge_json = json.dumps(
                {
                    "dimension_scores": score.judge_result.dimension_scores,
                    "rationale": score.judge_result.rationale,
                    "standout_moments": score.judge_result.standout_moments,
                    "failure_modes": score.judge_result.failure_modes,
                    "overall_note": score.judge_result.overall_note,
                    "judge_model": score.judge_result.judge_model,
                }
            )
            judge_model = score.judge_result.judge_model

        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO session_scores
                (stable_id, provider, started_at, scored_at, overall,
                 dimension_scores_json, judge_result_json,
                 features_json, source_path, judge_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    score.session_stable_id,
                    score.provider,
                    score.started_at.isoformat(),
                    _utcnow().isoformat(),
                    score.overall,
                    json.dumps(score.dimension_scores),
                    judge_json,
                    json.dumps(asdict(score.features)),
                    score.source_path,
                    judge_model,
                ),
            )

    def load_session_scores(
        self, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM session_scores"
        args: tuple = ()
        if since is not None:
            sql += " WHERE started_at >= ?"
            args = (since.isoformat(),)
        sql += " ORDER BY started_at ASC"
        with self._conn() as conn:
            rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
        for row in rows:
            row["dimension_scores"] = json.loads(row["dimension_scores_json"])
            row["features"] = json.loads(row["features_json"])
            row["judge_result"] = (
                json.loads(row["judge_result_json"]) if row["judge_result_json"] else None
            )
        return rows

    def load_one_session_score(self, stable_id: str) -> dict[str, Any] | None:
        """Return one row of session_scores by stable_id, or None.

        Used by ``praxis re-score`` to look up the source_path/provider for a
        single session before re-running the judge against it (spec section
        12.1: re-score is the single-session counterpart to ``praxis scan``).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM session_scores WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["dimension_scores"] = json.loads(out["dimension_scores_json"])
        out["features"] = json.loads(out["features_json"])
        out["judge_result"] = (
            json.loads(out["judge_result_json"]) if out["judge_result_json"] else None
        )
        return out

    # ---- moments --------------------------------------------------------

    def save_moments(
        self, session_stable_id: str, moments: list[Moment]
    ) -> list[Moment]:
        """Replace this session's moments with the given list, after redaction.

        Per spec §4.4 + AC for US-020:
        - Each surviving moment is redacted before insert.
        - moment_id is sha256(session_stable_id + dim_key + turn_index)[:16].
        - Re-judging a session replaces its moments deterministically: the
          whole prior set for this session is removed first, then the new
          set is inserted. An empty `moments` list therefore clears the row.
        - `redacted=1` is set on rows where at least one of the three
          free-text fields changed under `redact_secrets`.

        Returns the persisted Moment objects with moment_id, session_stable_id,
        and created_at filled in. Caller may use these for rendering without
        re-reading the database.
        """
        now = _utcnow().isoformat()
        persisted: list[Moment] = []
        rows: list[tuple[Any, ...]] = []
        for m in moments:
            redacted_excerpt = redact_secrets(m.quoted_excerpt)
            redacted_why = redact_secrets(m.why_it_lost_score)
            redacted_alt = redact_secrets(m.suggested_alternative)
            was_redacted = (
                redacted_excerpt != m.quoted_excerpt
                or redacted_why != m.why_it_lost_score
                or redacted_alt != m.suggested_alternative
            )
            moment_id = compute_moment_id(session_stable_id, m.dim_key, m.turn_index)
            rows.append(
                (
                    moment_id,
                    session_stable_id,
                    m.dim_key,
                    m.turn_index,
                    redacted_excerpt,
                    redacted_why,
                    redacted_alt,
                    m.dollar_impact_estimate,
                    m.minutes_impact_estimate,
                    m.severity,
                    now,
                    1 if was_redacted else 0,
                )
            )
            persisted.append(
                Moment(
                    dim_key=m.dim_key,
                    turn_index=m.turn_index,
                    quoted_excerpt=redacted_excerpt,
                    why_it_lost_score=redacted_why,
                    suggested_alternative=redacted_alt,
                    severity=m.severity,
                    moment_id=moment_id,
                    session_stable_id=session_stable_id,
                    created_at=_utcnow(),
                    dollar_impact_estimate=m.dollar_impact_estimate,
                    minutes_impact_estimate=m.minutes_impact_estimate,
                )
            )
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM moments WHERE session_stable_id = ?",
                (session_stable_id,),
            )
            if rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO moments
                    (moment_id, session_stable_id, dim_key, turn_index,
                     quoted_excerpt, why_it_lost_score, suggested_alternative,
                     dollar_impact_estimate, minutes_impact_estimate,
                     severity, created_at, redacted)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        return persisted

    def load_moments(self, session_stable_id: str) -> list[dict[str, Any]]:
        """Return every persisted moment for the given session, oldest first."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM moments WHERE session_stable_id = ? "
                "ORDER BY turn_index ASC, dim_key ASC",
                (session_stable_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ---- daily consolidation (v0.2: table dropped; stubs keep callers alive
    #      until the orchestrator/CLI/reports refactor lands) ----

    def latest_consolidation_date(self) -> date | None:
        return None

    def save_consolidation(
        self,
        for_date: date,
        snapshot: ProfileSnapshot,
        coaching: dict,
        sessions_in_window: int,
    ) -> None:
        return None

    def load_consolidation(self, for_date: date) -> dict | None:
        return None

    def consolidation_history(self, days: int = 30) -> list[dict]:
        return []

    # ---- run log --------------------------------------------------------

    def log_run(self, kind: str, sessions_seen: int, sessions_new: int, notes: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (_utcnow().isoformat(), kind, sessions_seen, sessions_new, notes),
            )

    # ---- follow-ups -----------------------------------------------------

    def save_follow_up(self, follow_up: FollowUp) -> None:
        """Persist (or replace) one row of follow_ups keyed by week_iso."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO follow_ups
                (week_iso, dim_key, commitment_text, target_metric,
                 baseline_value, measured_value, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    follow_up.week_iso,
                    follow_up.dim_key,
                    follow_up.commitment_text,
                    follow_up.target_metric,
                    follow_up.baseline_value,
                    follow_up.measured_value,
                    follow_up.outcome,
                ),
            )

    def load_follow_up(self, week_iso: str) -> FollowUp | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT week_iso, dim_key, commitment_text, target_metric, "
                "       baseline_value, measured_value, outcome "
                "FROM follow_ups WHERE week_iso = ?",
                (week_iso,),
            ).fetchone()
        if row is None:
            return None
        outcome: Outcome = row["outcome"]
        return FollowUp(
            week_iso=row["week_iso"],
            dim_key=row["dim_key"],
            commitment_text=row["commitment_text"],
            target_metric=row["target_metric"],
            baseline_value=row["baseline_value"],
            measured_value=row["measured_value"],
            outcome=outcome,
        )

    def prior_follow_up(self, before_week_iso: str) -> FollowUp | None:
        """Return the most recent follow_up with week_iso strictly before the given one.

        ISO week strings are zero-padded (YYYY-Www), so lexical order matches
        chronological order; a plain `<` comparison correctly handles the
        year boundary (e.g. '2025-W52' < '2026-W01').
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT week_iso, dim_key, commitment_text, target_metric, "
                "       baseline_value, measured_value, outcome "
                "FROM follow_ups WHERE week_iso < ? "
                "ORDER BY week_iso DESC LIMIT 1",
                (before_week_iso,),
            ).fetchone()
        if row is None:
            return None
        outcome: Outcome = row["outcome"]
        return FollowUp(
            week_iso=row["week_iso"],
            dim_key=row["dim_key"],
            commitment_text=row["commitment_text"],
            target_metric=row["target_metric"],
            baseline_value=row["baseline_value"],
            measured_value=row["measured_value"],
            outcome=outcome,
        )

    def latest_follow_up(self) -> FollowUp | None:
        """Return the most recent follow_up by week_iso, or None if the table is empty.

        Same lexical-equals-chronological argument as `prior_follow_up`.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT week_iso, dim_key, commitment_text, target_metric, "
                "       baseline_value, measured_value, outcome "
                "FROM follow_ups ORDER BY week_iso DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        outcome: Outcome = row["outcome"]
        return FollowUp(
            week_iso=row["week_iso"],
            dim_key=row["dim_key"],
            commitment_text=row["commitment_text"],
            target_metric=row["target_metric"],
            baseline_value=row["baseline_value"],
            measured_value=row["measured_value"],
            outcome=outcome,
        )
