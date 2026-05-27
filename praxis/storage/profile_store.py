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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> datetime:
    """Tz-aware UTC now. Wraps datetime.now(timezone.utc) for terseness."""
    return datetime.now(timezone.utc)

from praxis.follow_up import FollowUp, Outcome
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

CREATE TABLE IF NOT EXISTS daily_consolidations (
    consolidation_date TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    coaching_json TEXT NOT NULL,
    sessions_in_window INTEGER NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    sessions_seen INTEGER NOT NULL,
    sessions_new INTEGER NOT NULL,
    notes TEXT
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
            conn.executescript(SCHEMA)

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

    # ---- daily consolidation --------------------------------------------

    def latest_consolidation_date(self) -> date | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT consolidation_date FROM daily_consolidations "
                "ORDER BY consolidation_date DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return date.fromisoformat(row["consolidation_date"])

    def save_consolidation(
        self,
        for_date: date,
        snapshot: ProfileSnapshot,
        coaching: dict,
        sessions_in_window: int,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_consolidations
                (consolidation_date, snapshot_json, coaching_json,
                 sessions_in_window, generated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    for_date.isoformat(),
                    json.dumps(
                        {
                            "overall": snapshot.overall,
                            "dimension_means": snapshot.dimension_means,
                            "session_count": snapshot.session_count,
                            "provider_breakdown": snapshot.provider_breakdown,
                            "strongest_dimension": snapshot.strongest_dimension,
                            "weakest_dimension": snapshot.weakest_dimension,
                            "standout_moments": snapshot.standout_moments,
                            "failure_modes": snapshot.failure_modes,
                        }
                    ),
                    json.dumps(coaching),
                    sessions_in_window,
                    _utcnow().isoformat(),
                ),
            )

    def load_consolidation(self, for_date: date) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM daily_consolidations WHERE consolidation_date = ?",
                (for_date.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return {
            "consolidation_date": row["consolidation_date"],
            "snapshot": json.loads(row["snapshot_json"]),
            "coaching": json.loads(row["coaching_json"]),
            "sessions_in_window": row["sessions_in_window"],
            "generated_at": row["generated_at"],
        }

    def consolidation_history(self, days: int = 30) -> list[dict]:
        cutoff = (_utcnow().date() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_consolidations "
                "WHERE consolidation_date >= ? "
                "ORDER BY consolidation_date ASC",
                (cutoff,),
            ).fetchall()
        return [
            {
                "consolidation_date": r["consolidation_date"],
                "snapshot": json.loads(r["snapshot_json"]),
                "sessions_in_window": r["sessions_in_window"],
            }
            for r in rows
        ]

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
