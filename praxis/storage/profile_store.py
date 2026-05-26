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
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> datetime:
    """Tz-aware UTC now. Wraps datetime.now(timezone.utc) for terseness."""
    return datetime.now(timezone.utc)

from praxis.scoring.aggregate import ProfileSnapshot, SessionScore


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
    heuristic_scores_json TEXT NOT NULL,
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
        with self._conn() as conn:
            if self._has_schema_v2_marker(conn):
                return
            conn.executescript(SCHEMA)
            self._mark_schema_v2(conn)

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
                 dimension_scores_json, heuristic_scores_json, judge_result_json,
                 features_json, source_path, judge_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    score.session_stable_id,
                    score.provider,
                    score.started_at.isoformat(),
                    _utcnow().isoformat(),
                    score.overall,
                    json.dumps(score.dimension_scores),
                    json.dumps(score.heuristic_scores),
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
            row["heuristic_scores"] = json.loads(row["heuristic_scores_json"])
            row["features"] = json.loads(row["features_json"])
            row["judge_result"] = (
                json.loads(row["judge_result_json"]) if row["judge_result_json"] else None
            )
        return rows

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
