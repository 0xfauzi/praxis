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

from praxis.models import Moment, compute_moment_id
from praxis.redactor import redact_secrets
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
