"""Tests for ProfileStore.save_moments and compute_moment_id.

US-020 persists judge-emitted moments after redaction. The fields under test:
  - moment_id is sha256(session_stable_id + dim_key + turn_index)[:16]
  - secrets in any of the three free-text fields are redacted before insert
  - redacted=1 is set on rows where at least one field changed under redaction
  - re-judging a session replaces its prior moments (DELETE + INSERT)
  - calling save_moments with the heuristic-only path does not run (covered in
    test_orchestrator); here we exercise the storage layer directly.

US-031 also exercises ProfileStore.record_pass1_confidence /
recent_pass1_confidence: the rolling 4-week telemetry that powers the
calibration auto-tune is persisted via the same store and the count
aggregation must be correct for the orchestrator's threshold checks.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from praxis.models import Moment, compute_moment_id
from praxis.storage.profile_store import ProfileStore, resolve_home


def _moment(
    *,
    dim_key: str = "planning",
    turn_index: int = 0,
    excerpt: str = "an excerpt",
    why: str = "a reason",
    alt: str = "an alternative",
    coach_line: str | None = "You skipped a step.",
    severity: str = "minor",
) -> Moment:
    return Moment(
        dim_key=dim_key,
        turn_index=turn_index,
        quoted_excerpt=excerpt,
        why_it_lost_score=why,
        suggested_alternative=alt,
        coach_line=coach_line,
        severity=severity,  # type: ignore[arg-type]
    )


def test_save_moments_round_trips_coach_line(tmp_home) -> None:
    """coach_line persists and reads back through save_moments/load_moments."""
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessC",
        [_moment(coach_line="You pasted results instead of running them.")],
    )
    rows = store.load_moments("sessC")
    assert len(rows) == 1
    assert rows[0]["coach_line"] == "You pasted results instead of running them."


def test_save_moments_redacts_secret_in_coach_line(tmp_home) -> None:
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessR",
        [_moment(coach_line="You leaked sk-ant-secret123456789012345678 in the prompt.")],
    )
    rows = store.load_moments("sessR")
    assert "sk-ant-secret123456789012345678" not in (rows[0]["coach_line"] or "")
    assert rows[0]["redacted"] == 1


def test_save_moments_allows_null_coach_line(tmp_home) -> None:
    """A moment without a coach_line (None) still persists cleanly."""
    store = ProfileStore(home=resolve_home())
    store.save_moments("sessN", [_moment(coach_line=None)])
    rows = store.load_moments("sessN")
    assert rows[0]["coach_line"] is None


# ---------------------------------------------------------------------------
# compute_moment_id
# ---------------------------------------------------------------------------


def test_compute_moment_id_matches_spec_formula() -> None:
    """Spec §4.1: moment_id = sha256(stable_id + dim_key + str(turn_index))[:16]."""
    stable_id = "abc1234567890def"
    dim_key = "verification"
    turn_index = 5
    expected = hashlib.sha256(
        stable_id.encode() + dim_key.encode() + str(turn_index).encode()
    ).hexdigest()[:16]
    assert compute_moment_id(stable_id, dim_key, turn_index) == expected
    assert len(compute_moment_id(stable_id, dim_key, turn_index)) == 16


def test_compute_moment_id_is_deterministic() -> None:
    a = compute_moment_id("sid", "planning", 3)
    b = compute_moment_id("sid", "planning", 3)
    assert a == b


def test_compute_moment_id_differs_by_each_input() -> None:
    base = compute_moment_id("sid", "planning", 3)
    assert compute_moment_id("other", "planning", 3) != base
    assert compute_moment_id("sid", "context", 3) != base
    assert compute_moment_id("sid", "planning", 4) != base


# ---------------------------------------------------------------------------
# save_moments redaction + redacted flag
# ---------------------------------------------------------------------------


def test_save_moments_redacts_secret_in_quoted_excerpt(tmp_home) -> None:
    store = ProfileStore(home=resolve_home())
    # An Anthropic-shaped key, long enough to satisfy redactor's {20,} budget.
    secret_excerpt = "I pasted sk-ant-AAAAAAAAAAAAAAAAAAAAAAAAAA into chat"
    store.save_moments(
        "sess123",
        [_moment(excerpt=secret_excerpt, dim_key="planning", turn_index=0)],
    )
    rows = store.load_moments("sess123")
    assert len(rows) == 1
    assert "sk-ant-" not in rows[0]["quoted_excerpt"]
    assert "[REDACTED]" in rows[0]["quoted_excerpt"]
    assert rows[0]["redacted"] == 1


def test_save_moments_redacts_secret_in_why_field(tmp_home) -> None:
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sess123",
        [
            _moment(
                why="API key=ABCDEFGHIJKLMNOPQRSTUVWXYZ012 was leaked",
                dim_key="verification",
                turn_index=2,
            )
        ],
    )
    rows = store.load_moments("sess123")
    assert "[REDACTED]" in rows[0]["why_it_lost_score"]
    assert rows[0]["redacted"] == 1


def test_save_moments_redacts_secret_in_suggested_alternative(tmp_home) -> None:
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sess123",
        [
            _moment(
                alt="Never write password=zzzzzzzzzzzzzzzzzzzzzzzz to a log file",
                dim_key="iteration",
                turn_index=3,
            )
        ],
    )
    rows = store.load_moments("sess123")
    assert "[REDACTED]" in rows[0]["suggested_alternative"]
    assert rows[0]["redacted"] == 1


def test_save_moments_no_redaction_sets_flag_zero(tmp_home) -> None:
    """A clean moment with no secrets should land with redacted=0."""
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sess123",
        [
            _moment(
                excerpt="run the migration",
                why="did not preview the SQL",
                alt="ask: list every table this writes to",
                dim_key="verification",
                turn_index=4,
            )
        ],
    )
    rows = store.load_moments("sess123")
    assert rows[0]["redacted"] == 0


def test_save_moments_redaction_only_one_field_changed_still_flags(tmp_home) -> None:
    """redacted=1 if ANY of the three fields changed under redaction."""
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sess123",
        [
            _moment(
                excerpt="clean excerpt",
                why="contains token: AAAAAAAAAAAAAAAAAAAAAAAA hidden",
                alt="clean alternative",
            )
        ],
    )
    rows = store.load_moments("sess123")
    assert rows[0]["redacted"] == 1
    assert rows[0]["quoted_excerpt"] == "clean excerpt"
    assert rows[0]["suggested_alternative"] == "clean alternative"


# ---------------------------------------------------------------------------
# save_moments moment_id + row shape
# ---------------------------------------------------------------------------


def test_save_moments_writes_expected_moment_id(tmp_home) -> None:
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessABC",
        [_moment(dim_key="planning", turn_index=7)],
    )
    rows = store.load_moments("sessABC")
    expected_id = compute_moment_id("sessABC", "planning", 7)
    assert rows[0]["moment_id"] == expected_id


def test_save_moments_returns_persisted_moments_with_ids(tmp_home) -> None:
    """The return value lets callers render without re-reading the DB."""
    store = ProfileStore(home=resolve_home())
    persisted = store.save_moments(
        "sessRET",
        [_moment(dim_key="context", turn_index=1)],
    )
    assert len(persisted) == 1
    assert persisted[0].moment_id == compute_moment_id("sessRET", "context", 1)
    assert persisted[0].session_stable_id == "sessRET"
    assert persisted[0].created_at is not None


def test_save_moments_persists_all_required_columns(tmp_home) -> None:
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessFULL",
        [
            _moment(
                dim_key="tools",
                turn_index=2,
                excerpt="quoted",
                why="why",
                alt="alt",
                severity="major",
            )
        ],
    )
    row = store.load_moments("sessFULL")[0]
    assert row["session_stable_id"] == "sessFULL"
    assert row["dim_key"] == "tools"
    assert row["turn_index"] == 2
    assert row["quoted_excerpt"] == "quoted"
    assert row["why_it_lost_score"] == "why"
    assert row["suggested_alternative"] == "alt"
    assert row["severity"] == "major"
    assert row["created_at"]  # ISO timestamp; non-empty
    # Optional cost fields default to None when the cost-attribution pass
    # has not yet run on this moment.
    assert row["dollar_impact_estimate"] is None
    assert row["minutes_impact_estimate"] is None


# ---------------------------------------------------------------------------
# save_moments upsert / replacement semantics
# ---------------------------------------------------------------------------


def test_save_moments_re_judge_replaces_prior_set(tmp_home) -> None:
    """AC: re-judging a session replaces its moments deterministically.

    First write: 2 moments. Second write for the same session: 1 different
    moment. The result must be exactly 1 row for the session - the prior
    moments are gone, not merged.
    """
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessUP",
        [
            _moment(dim_key="planning", turn_index=0),
            _moment(dim_key="context", turn_index=1, excerpt="second"),
        ],
    )
    assert len(store.load_moments("sessUP")) == 2

    store.save_moments(
        "sessUP",
        [_moment(dim_key="verification", turn_index=5, excerpt="brand new")],
    )
    rows = store.load_moments("sessUP")
    assert len(rows) == 1
    assert rows[0]["dim_key"] == "verification"
    assert rows[0]["turn_index"] == 5


def test_save_moments_empty_list_clears_prior_moments(tmp_home) -> None:
    """A re-judge that found no moments clears the prior set rather than
    leaving stale rows. This matches `verify_moment_substrings` semantics:
    if the new judge has nothing to coach on, the session shows nothing."""
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessEMPTY",
        [_moment(dim_key="planning", turn_index=0)],
    )
    assert len(store.load_moments("sessEMPTY")) == 1

    store.save_moments("sessEMPTY", [])
    assert store.load_moments("sessEMPTY") == []


def test_save_moments_same_id_replaces_in_place(tmp_home) -> None:
    """Re-judging the same (session, dim, turn) with a different excerpt
    replaces the row at the same moment_id (INSERT OR REPLACE)."""
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessSAME",
        [
            _moment(
                dim_key="iteration",
                turn_index=2,
                excerpt="first cut",
                why="first reason",
            )
        ],
    )
    expected_id = compute_moment_id("sessSAME", "iteration", 2)

    store.save_moments(
        "sessSAME",
        [
            _moment(
                dim_key="iteration",
                turn_index=2,
                excerpt="second cut",
                why="updated reason",
            )
        ],
    )
    rows = store.load_moments("sessSAME")
    assert len(rows) == 1
    assert rows[0]["moment_id"] == expected_id
    assert rows[0]["quoted_excerpt"] == "second cut"
    assert rows[0]["why_it_lost_score"] == "updated reason"


def test_save_moments_other_sessions_unaffected_by_replacement(tmp_home) -> None:
    """Replacing one session's moments must not touch another session's rows."""
    store = ProfileStore(home=resolve_home())
    store.save_moments(
        "sessA",
        [_moment(dim_key="planning", turn_index=0, excerpt="A1")],
    )
    store.save_moments(
        "sessB",
        [_moment(dim_key="planning", turn_index=0, excerpt="B1")],
    )
    # Re-judge only sessA.
    store.save_moments(
        "sessA",
        [_moment(dim_key="context", turn_index=1, excerpt="A2")],
    )
    a_rows = store.load_moments("sessA")
    b_rows = store.load_moments("sessB")
    assert len(a_rows) == 1
    assert a_rows[0]["dim_key"] == "context"
    assert len(b_rows) == 1
    assert b_rows[0]["quoted_excerpt"] == "B1"


# ---------------------------------------------------------------------------
# US-031: pass-1 confidence-distribution telemetry persistence
# ---------------------------------------------------------------------------


def test_record_pass1_confidence_writes_run_log_row(tmp_home) -> None:
    """AC: each weekly run logs the pass-1 confidence distribution.

    record_pass1_confidence must produce a discoverable ``kind='pass1_conf'``
    row carrying low/medium/high counts. We assert via a raw sqlite query so
    we know the storage format - not just that the round trip works.
    """
    store = ProfileStore(home=resolve_home())
    store.record_pass1_confidence(low=2, medium=7, high=3)
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            conn.execute(
                "SELECT notes FROM run_log WHERE kind = 'pass1_conf'"
            ).fetchall()
        )
    finally:
        conn.close()
    assert len(rows) == 1
    import json
    parsed = json.loads(rows[0]["notes"])
    assert parsed == {"low": 2, "medium": 7, "high": 3}


def test_recent_pass1_confidence_aggregates_rows_in_window(tmp_home) -> None:
    """Counts across multiple rows in the 4-week window must sum correctly."""
    store = ProfileStore(home=resolve_home())
    store.record_pass1_confidence(low=1, medium=3, high=5)
    store.record_pass1_confidence(low=2, medium=4, high=6)
    store.record_pass1_confidence(low=0, medium=1, high=2)
    rolling = store.recent_pass1_confidence(weeks=4)
    assert rolling == {"low": 3, "medium": 8, "high": 13}


def test_recent_pass1_confidence_returns_zeros_when_empty(tmp_home) -> None:
    """A fresh install has no pass1_conf rows; the result must be all zeros
    so callers can safely check ``total > 0`` before computing a share."""
    store = ProfileStore(home=resolve_home())
    rolling = store.recent_pass1_confidence(weeks=4)
    assert rolling == {"low": 0, "medium": 0, "high": 0}


def test_recent_pass1_confidence_excludes_rows_older_than_window(tmp_home) -> None:
    """Rows older than the 4-week cutoff are excluded.

    Inject a row with a timestamp set 5 weeks in the past; the rolling query
    must ignore it and only count rows from inside the window.
    """
    store = ProfileStore(home=resolve_home())
    # In-window row first - this one should count.
    store.record_pass1_confidence(low=1, medium=1, high=1)
    # Now hand-write an old row by going through sqlite directly.
    old_ts = (datetime.now(timezone.utc) - timedelta(weeks=5)).isoformat()
    import json
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (old_ts, "pass1_conf", 0, 0, json.dumps({"low": 99, "medium": 99, "high": 99})),
        )
        conn.commit()
    finally:
        conn.close()
    rolling = store.recent_pass1_confidence(weeks=4)
    # Only the recent row counts. The 99/99/99 row is past the cutoff.
    assert rolling == {"low": 1, "medium": 1, "high": 1}


def test_recent_pass1_confidence_ignores_malformed_rows(tmp_home) -> None:
    """A pass1_conf row with non-JSON notes must not crash the query.

    Defensive: a future code path or hand-edit could leave garbage in the
    notes column. We log nothing - parsing simply skips the row so the
    rolling share stays sensible.
    """
    store = ProfileStore(home=resolve_home())
    store.record_pass1_confidence(low=1, medium=2, high=3)
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "INSERT INTO run_log (run_at, kind, sessions_seen, sessions_new, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), "pass1_conf", 0, 0, "not-json"),
        )
        conn.commit()
    finally:
        conn.close()
    rolling = store.recent_pass1_confidence(weeks=4)
    assert rolling == {"low": 1, "medium": 2, "high": 3}


def test_recent_pass1_confidence_only_counts_pass1_conf_kind(tmp_home) -> None:
    """Other run_log rows (kind='full' etc.) must not be parsed as distribution
    data. The notes column on a full-run row carries free-text summaries that
    would otherwise crash json.loads or, worse, spuriously match key names."""
    store = ProfileStore(home=resolve_home())
    store.log_run(
        kind="full",
        sessions_seen=10,
        sessions_new=5,
        notes='{"low": 99, "medium": 99, "high": 99}',
    )
    rolling = store.recent_pass1_confidence(weeks=4)
    # A 'full' row with a notes string that happens to be valid JSON must
    # still be ignored - the kind filter is what makes the query safe.
    assert rolling == {"low": 0, "medium": 0, "high": 0}
