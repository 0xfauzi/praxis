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
from datetime import date, datetime, timedelta, timezone
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
    stable_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    started_at TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    overall REAL NOT NULL,
    dimension_scores_json TEXT NOT NULL,
    judge_result_json TEXT,
    features_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    judge_model TEXT,
    judge_pass INTEGER NOT NULL DEFAULT 1,
    signals_json TEXT,
    PRIMARY KEY (stable_id, judge_pass)
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
                if self._has_schema_v3_marker(conn):
                    # Already at v3+. Run any later additive column migrations
                    # in-place so older v3 DBs gain new optional columns
                    # (signals_json) without a full table rebuild.
                    self._ensure_session_scores_columns(conn)
                    return

        # Migration needed (fresh DB, v0.1, or v0.2 DB without the v3 marker).
        # Per spec Appendix A.7: back up the live DB before any DDL, and on
        # failure restore from backup so the DB is never half-migrated.
        backup_path = self._backup_db_if_exists()
        try:
            with self._conn() as conn:
                self._apply_v2_schema(conn)
                self._mark_schema_v3(conn)
        except Exception as exc:
            if backup_path is not None:
                self._restore_db_from_backup(backup_path)
            raise MigrationError(
                self._migration_failure_message(backup_path, exc)
            ) from exc

    @staticmethod
    def _ensure_session_scores_columns(conn: sqlite3.Connection) -> None:
        """Add additive columns introduced after the v3 marker landed.

        Currently only `signals_json` (spec section 7 - persists per-session
        BehavioralSignals so the weekly-bucketed trajectory can replay 90
        days of history without re-parsing source files). Idempotent: a
        DB that already has the column is left alone.
        """
        cur = conn.execute("PRAGMA table_info(session_scores)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "signals_json" not in existing_cols:
            conn.execute(
                "ALTER TABLE session_scores ADD COLUMN signals_json TEXT"
            )

    def _apply_v2_schema(self, conn: sqlite3.Connection) -> None:
        # Wrapped in a method so tests can monkeypatch it to inject failures
        # without having to corrupt the SCHEMA constant. The name is kept for
        # back-compat with existing monkeypatch tests; despite the v2 suffix,
        # the method now also runs the v0.3 session_scores migration so any
        # prior state (fresh, v0.1, v0.2) is brought to v0.3 in one shot.
        conn.executescript(SCHEMA)
        self._migrate_session_scores_to_v3(conn)

    def _migrate_session_scores_to_v3(self, conn: sqlite3.Connection) -> None:
        """Spec §9.6 (US-029): add ``judge_pass`` to session_scores and make
        (stable_id, judge_pass) the composite primary key so both pass-1 and
        pass-2 rows can coexist for the same session.

        Also adds ``signals_json`` (spec §7) for persisting per-session
        BehavioralSignals so the weekly-bucketed trajectory model can read
        90 days of history at runtime.

        SQLite can't ALTER a primary key in place, so we recreate the table.
        Idempotent: if ``judge_pass`` and ``signals_json`` are already
        columns the method returns without touching the table.
        """
        cur = conn.execute("PRAGMA table_info(session_scores)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "judge_pass" in existing_cols and "signals_json" in existing_cols:
            return
        # If we already have judge_pass but not signals_json (an older
        # v0.3 DB), just add the column - no table rebuild needed.
        if "judge_pass" in existing_cols and "signals_json" not in existing_cols:
            conn.execute("ALTER TABLE session_scores ADD COLUMN signals_json TEXT")
            return
        # We rebuild the table to drop the unused v0.1 ``heuristic_scores_json``
        # column (carried forward through the v0.2 migration via CREATE TABLE
        # IF NOT EXISTS) and to install the composite PK.
        conn.executescript(
            """
            CREATE TABLE session_scores_v3 (
                stable_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                started_at TEXT NOT NULL,
                scored_at TEXT NOT NULL,
                overall REAL NOT NULL,
                dimension_scores_json TEXT NOT NULL,
                judge_result_json TEXT,
                features_json TEXT NOT NULL,
                source_path TEXT NOT NULL,
                judge_model TEXT,
                judge_pass INTEGER NOT NULL DEFAULT 1,
                signals_json TEXT,
                PRIMARY KEY (stable_id, judge_pass)
            );
            INSERT INTO session_scores_v3
                (stable_id, provider, started_at, scored_at, overall,
                 dimension_scores_json, judge_result_json, features_json,
                 source_path, judge_model, judge_pass)
            SELECT stable_id, provider, started_at, scored_at, overall,
                   dimension_scores_json, judge_result_json, features_json,
                   source_path, judge_model, 1
            FROM session_scores;
            DROP TABLE session_scores;
            ALTER TABLE session_scores_v3 RENAME TO session_scores;
            CREATE INDEX IF NOT EXISTS idx_session_started_at
                ON session_scores(started_at);
            CREATE INDEX IF NOT EXISTS idx_session_provider
                ON session_scores(provider);
            """
        )

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
    def _has_schema_v3_marker(conn: sqlite3.Connection) -> bool:
        # Detection is from existing schema state, not from a config flag:
        # fresh DBs have no run_log table, v0.1 DBs have run_log without any
        # schema_version row, and v0.2 DBs have ``schema_version='2'``. In
        # all of those cases the v0.3 migration still needs to run, so the
        # gate keys on the v3 marker specifically.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'run_log'"
        )
        if cur.fetchone() is None:
            return False
        cur = conn.execute(
            "SELECT 1 FROM run_log WHERE kind = 'schema_version' AND notes = '3' LIMIT 1"
        )
        return cur.fetchone() is not None

    @staticmethod
    def _mark_schema_v3(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (_utcnow().isoformat(), "schema_version", 0, 0, "3"),
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

    def save_session_score(
        self,
        score: SessionScore,
        *,
        signals: dict[str, float] | None = None,
    ) -> None:
        """Persist one SessionScore row, optionally with behavioral signals.

        ``signals`` carries per-session BehavioralSignals as a dict (engagement_rate,
        delegation_rate, independence_rate, etc) so the weekly-bucketed
        trajectory (spec §7) can replay the 90-day window without re-parsing
        source files. None means the caller didn't compute signals; the column
        is set to NULL and the trajectory model will skip that session.
        """
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
        signals_json = json.dumps(signals) if signals is not None else None

        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO session_scores
                (stable_id, provider, started_at, scored_at, overall,
                 dimension_scores_json, judge_result_json,
                 features_json, source_path, judge_model, judge_pass,
                 signals_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    score.judge_pass,
                    signals_json,
                ),
            )

    def load_session_scores(
        self,
        since: datetime | None = None,
        *,
        include_all_passes: bool = False,
    ) -> list[dict[str, Any]]:
        """Return persisted session_scores rows ordered by ``started_at``.

        By default, when a session has both a pass-1 and a pass-2 row, only
        the pass-2 (winning frontier judgment) is returned so callers that
        snapshot or count sessions don't see the same session twice. Pass
        ``include_all_passes=True`` to retrieve every persisted row (US-029
        auditing: pass-1 + pass-2 disagreement analysis).
        """
        sql = "SELECT * FROM session_scores"
        args: tuple = ()
        if since is not None:
            sql += " WHERE started_at >= ?"
            args = (since.isoformat(),)
        sql += " ORDER BY started_at ASC, judge_pass ASC"
        with self._conn() as conn:
            rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
        for row in rows:
            row["dimension_scores"] = json.loads(row["dimension_scores_json"])
            row["features"] = json.loads(row["features_json"])
            row["judge_result"] = (
                json.loads(row["judge_result_json"]) if row["judge_result_json"] else None
            )
        if include_all_passes:
            return rows
        # Dedupe to highest pass per session, preserving order from the SQL
        # ordering (rows are already sorted by started_at then judge_pass ASC,
        # so the last-seen row for each stable_id is the winning pass).
        by_session: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_session[row["stable_id"]] = row
        return list(by_session.values())

    def load_one_session_score(self, stable_id: str) -> dict[str, Any] | None:
        """Return the authoritative row of session_scores for stable_id, or None.

        With the (stable_id, judge_pass) composite PK (US-029), a session
        can have both a pass-1 and a pass-2 row. The pass-2 row is the
        frontier judgment and overrides pass-1, so this function returns
        the highest-pass row available.

        Used by ``praxis re-score`` to look up the source_path/provider for a
        single session before re-running the judge against it (spec section
        12.1: re-score is the single-session counterpart to ``praxis scan``).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM session_scores WHERE stable_id = ? "
                "ORDER BY judge_pass DESC LIMIT 1",
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

    def load_moment_by_id(self, moment_id: str) -> dict[str, Any] | None:
        """Look up one moment row by moment_id, returning None if missing."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM moments WHERE moment_id = ?",
                (moment_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    # ---- tasks ----------------------------------------------------------

    def save_task(
        self,
        *,
        task_id: str,
        label: str,
        task_type: str,
        project_hint: str | None,
        started_at: datetime,
        ended_at: datetime,
        session_stable_ids: list[str],
        total_cost_estimate_usd: float | None,
        label_source: str,
    ) -> None:
        """Persist one task cluster + its members.

        Spec section 14: ``tasks`` and ``task_members`` are the v0.2
        tables; each weekly run UPSERTs the current week's clusters so
        that re-running the same week is idempotent. The members are
        replaced as a set (delete-then-insert under the same task_id) so
        a re-cluster that shifts a session between tasks does not leave
        a stale row.
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                (task_id, label, task_type, project_hint, started_at,
                 ended_at, session_count, total_cost_estimate_usd, label_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    label,
                    task_type,
                    project_hint,
                    started_at.isoformat(),
                    ended_at.isoformat(),
                    len(session_stable_ids),
                    total_cost_estimate_usd,
                    label_source,
                ),
            )
            conn.execute(
                "DELETE FROM task_members WHERE task_id = ?",
                (task_id,),
            )
            if session_stable_ids:
                conn.executemany(
                    "INSERT INTO task_members (task_id, session_stable_id) "
                    "VALUES (?, ?)",
                    [(task_id, sid) for sid in session_stable_ids],
                )

    def load_tasks_for_week(
        self, week_start: datetime, week_end: datetime
    ) -> list[dict[str, Any]]:
        """Return tasks that started within [week_start, week_end), with members."""
        with self._conn() as conn:
            task_rows = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM tasks WHERE started_at >= ? AND started_at < ? "
                    "ORDER BY started_at ASC",
                    (week_start.isoformat(), week_end.isoformat()),
                ).fetchall()
            ]
            for t in task_rows:
                members = conn.execute(
                    "SELECT session_stable_id FROM task_members WHERE task_id = ?",
                    (t["task_id"],),
                ).fetchall()
                t["session_stable_ids"] = [m["session_stable_id"] for m in members]
        return task_rows

    # ---- run log --------------------------------------------------------

    def log_run(self, kind: str, sessions_seen: int, sessions_new: int, notes: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (_utcnow().isoformat(), kind, sessions_seen, sessions_new, notes),
            )

    # ---- pass-1 confidence distribution telemetry (spec §9.6, US-031) ----

    def record_pass1_confidence(self, low: int, medium: int, high: int) -> None:
        """Append one pass-1 confidence-distribution row to ``run_log``.

        Spec §9.6 (US-031): each weekly run logs the pass-1 confidence
        distribution so the rolling 4-week share of high/low can be
        computed for the calibration auto-tune. We use ``kind='pass1_conf'``
        so the new rows can be queried without touching the existing
        ``kind='full'`` notes format; the counts are stored as a small
        JSON blob in ``notes`` to keep the schema unchanged.
        """
        payload = json.dumps({"low": low, "medium": medium, "high": high})
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (_utcnow().isoformat(), "pass1_conf", 0, 0, payload),
            )

    def recent_pass1_confidence(self, weeks: int = 4) -> dict[str, int]:
        """Return aggregated pass-1 confidence counts over the last N weeks.

        Reads every ``kind='pass1_conf'`` row whose ``run_at`` is within the
        rolling window and sums the counts. Returns zeros when no rows fall
        in the window (e.g. on a fresh install) so the caller can divide
        safely after a total > 0 check.
        """
        cutoff = _utcnow() - timedelta(weeks=weeks)
        counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT notes FROM run_log "
                "WHERE kind = 'pass1_conf' AND run_at >= ?",
                (cutoff.isoformat(),),
            ).fetchall()
        for row in rows:
            try:
                parsed = json.loads(row["notes"] or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            for key in counts:
                value = parsed.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    counts[key] += value
        return counts

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

    # ---- weekly digests -------------------------------------------------

    def save_weekly_digest(
        self,
        week_iso: str,
        trajectory_label: str,
        trajectory_headline: str,
        snapshot: ProfileSnapshot,
        headline_moment_id: str | None = None,
        cost_total_usd: float | None = None,
        cost_baseline_usd: float | None = None,
        html_path: str | None = None,
        generated_at: datetime | None = None,
    ) -> None:
        """UPSERT one weekly_digests row keyed by week_iso (spec section 14).

        snapshot_json is the full ProfileSnapshot for the week (US-071 AC #2);
        we round-trip via asdict so future schema additions to ProfileSnapshot
        flow through automatically. Re-running for the same week_iso replaces
        the row in-place (idempotent: `INSERT OR REPLACE` against the PK).
        """
        when = (generated_at or _utcnow()).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO weekly_digests
                (week_iso, generated_at, trajectory_label, trajectory_headline,
                 headline_moment_id, cost_total_usd, cost_baseline_usd,
                 snapshot_json, html_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    week_iso,
                    when,
                    trajectory_label,
                    trajectory_headline,
                    headline_moment_id,
                    cost_total_usd,
                    cost_baseline_usd,
                    json.dumps(asdict(snapshot)),
                    html_path,
                ),
            )

    def load_weekly_digest(self, week_iso: str) -> dict[str, Any] | None:
        """Read back one weekly_digests row, with snapshot_json parsed.

        Returns None if no row exists for the given week_iso.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT week_iso, generated_at, trajectory_label, trajectory_headline, "
                "       headline_moment_id, cost_total_usd, cost_baseline_usd, "
                "       snapshot_json, html_path "
                "FROM weekly_digests WHERE week_iso = ?",
                (week_iso,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["snapshot"] = json.loads(data["snapshot_json"])
        return data

    def count_weekly_digests(self) -> int:
        """Number of rows in weekly_digests. Used by idempotency tests."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM weekly_digests"
            ).fetchone()["c"]

    def weekly_cost_baseline(
        self, before_week_iso: str, lookback_days: int = 90
    ) -> float | None:
        """Mean cost_total_usd across prior digests in the last 90 days.

        Spec 10.1: the cost ledger baseline is the rolling weekly mean.
        Returns None when no prior digest has a recorded cost (e.g. the
        first weekly run, or a stretch of digests written before cost
        tracking landed). The current week is excluded by the `<` on
        week_iso so this week's own cost cannot leak into its own
        baseline (matches the score-baseline rule in spec 8.1).
        """
        cutoff = (_utcnow() - timedelta(days=lookback_days)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT AVG(cost_total_usd) AS avg_cost "
                "FROM weekly_digests "
                "WHERE week_iso < ? "
                "  AND generated_at >= ? "
                "  AND cost_total_usd IS NOT NULL",
                (before_week_iso, cutoff),
            ).fetchone()
        if row is None or row["avg_cost"] is None:
            return None
        return float(row["avg_cost"])

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
