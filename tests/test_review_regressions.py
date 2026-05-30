"""Regression tests for the adversarial-review hardening pass.

Each test pins a specific bug that the pre-existing (green) suite did NOT
cover -- the fixes live exactly where the old tests never looked, so without
these guards the bugs could silently return. Grouped by the criterion each
defends: F1 parse safety, F3 scoring robustness, F4 statistical honesty,
F5 time correctness, F6 data integrity, N1 privacy.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from praxis.behavior.signals import extract
from praxis.models import Provider, Role, Session, Turn
from praxis.redactor import PLACEHOLDER, redact_secrets
from praxis.scanners import ClaudeScanner, CodexScanner
from praxis.scoring.judge import _compact_transcript, _parse_response, _session_corpus
from praxis.storage.profile_store import ProfileStore, resolve_home


def _session(turns: list[Turn]) -> Session:
    return Session(
        provider=Provider.CLAUDE,
        session_id="regression",
        started_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
        turns=turns,
        source_path="/tmp/regression.jsonl",
        model_hint="claude-haiku-4-5",
    )


# --- F1: one malformed file must never abort a provider's whole scan -------

def test_claude_non_object_json_line_does_not_abort_scan(synthetic_claude_session):
    """A line that is valid JSON but not an object (bare array/number/
    string/bool/null) used to raise AttributeError out of parse() -- not a
    JSONDecodeError -- aborting the scan and silently dropping every other
    session for the provider."""
    with synthetic_claude_session.open("a", encoding="utf-8") as f:
        f.write("[1, 2, 3]\n42\n\"bare\"\nnull\ntrue\n")
    sessions = list(ClaudeScanner().scan())
    assert len(sessions) == 1
    assert sessions[0].turn_count == 3  # the real events still parsed


def test_codex_non_object_and_string_action_do_not_abort_scan(synthetic_codex_session):
    """Codex had the same non-object hazard plus a function_call branch that
    assumed `action` was always a dict."""
    with synthetic_codex_session.open("a", encoding="utf-8") as f:
        f.write("[1, 2, 3]\n")
        f.write('{"type":"function_call","payload":{"action":"shell"}}\n')
    sessions = list(CodexScanner().scan())
    assert len(sessions) == 1
    assert sessions[0].turn_count >= 2


# --- F5: a missing-zone timestamp coerces to UTC (no naive/aware crash) ----

def test_claude_naive_timestamp_coerced_to_utc(synthetic_claude_session):
    """A timestamp with no 'Z' and no offset parsed to a naive datetime,
    which later crashed the week-window filters (naive vs aware compare)."""
    sibling = synthetic_claude_session.parent / "naive-ts.jsonl"
    sibling.write_text(
        '{"type":"user","timestamp":"2026-05-28T10:00:00",'
        '"message":{"role":"user","content":"a naive timestamp session body"}}\n',
        encoding="utf-8",
    )
    by_id = {s.session_id: s for s in ClaudeScanner().scan()}
    assert by_id["naive-ts"].started_at.tzinfo is not None


# --- F3: hostile LLM output shapes must not crash or fabricate -------------

def test_parse_response_tolerates_wrong_container_types():
    for payload in (
        '{"scores": [1, 2, 3]}',
        '{"scores": "high"}',
        '{"scores": 7}',
        '{"scores": {}, "rationale": "nope"}',
        '{"scores": {}, "rationale": ["a", "b"]}',
        '{"scores": {}, "standout_moments": 5}',
        '{"scores": {}, "failure_modes": 9}',
    ):
        r = _parse_response(payload, model="m")  # must not raise
        assert isinstance(r.dimension_scores, dict)
        assert isinstance(r.standout_moments, list)
        assert isinstance(r.failure_modes, list)
    # A string standout must NOT char-splat into single-character "moments".
    r = _parse_response('{"scores": {}, "standout_moments": "abc"}', model="m")
    assert r.standout_moments == []


def test_parse_response_non_finite_scores_become_neutral():
    """json.loads accepts NaN/Infinity, and max(0, min(10, nan)) == 10.0 in
    CPython, so a garbage score silently became a perfect 10."""
    r = _parse_response(
        '{"scores": {"planning": NaN, "context": Infinity, "verification": -Infinity}}',
        model="m",
    )
    assert r.dimension_scores == {"planning": 5.0, "context": 5.0, "verification": 5.0}
    clamped = _parse_response('{"scores": {"a": 99, "b": -5}}', model="m")
    assert clamped.dimension_scores == {"a": 10.0, "b": 0.0}


# --- N1: secrets must be redacted before the transcript leaves the machine -

def test_transcript_to_llm_and_corpus_are_redacted():
    s = _session([
        Turn(role=Role.USER, content="key sk-ant-" + "A" * 28 + " and AKIA1234567890ABCDEF"),
        Turn(role=Role.ASSISTANT, content="pat github_pat_" + "A" * 82),
    ])
    for blob in (_compact_transcript(s), _session_corpus(s)):
        assert "sk-ant-AAAA" not in blob
        assert "AKIA1234567890ABCDEF" not in blob
        assert "github_pat_AAAA" not in blob
        assert PLACEHOLDER in blob


def test_redactor_covers_google_slack_and_pem():
    google = "AIza" + "B" * 35
    # Built by concatenation so the source carries no contiguous token-shaped
    # literal (GitHub push protection blocks a real-looking Slack token); the
    # runtime value still matches the redactor's xox[baprs]- pattern.
    slack = "xox" + "b-EXAMPLEonly0not0a0real0slack0token"
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAfakefakefakefakefakefake\n"
        "-----END RSA PRIVATE KEY-----"
    )
    for secret in (google, slack):
        out = redact_secrets(f"value: {secret} end")
        assert secret not in out and PLACEHOLDER in out
    pem_out = redact_secrets(f"before {pem} after")
    assert "BEGIN RSA PRIVATE KEY" not in pem_out
    assert "MIIEowIBAAKCAQEAfakefakefakefakefakefake" not in pem_out
    assert pem_out.startswith("before ") and pem_out.endswith(" after")


# --- F4: a pure-delegator verdict needs enough turns to be real ------------

def test_pure_delegator_needs_minimum_turns():
    """One terse "fix this" trips three atrophy patterns at once and used to
    flag a pure delegator on a single turn, driving a user-facing headline."""
    one = extract(_session([Turn(role=Role.USER, content="fix this")]))
    assert one.user_turn_count == 1
    assert one.is_pure_delegator is False
    two = extract(_session([
        Turn(role=Role.USER, content="fix this"),
        Turn(role=Role.USER, content="just make it work"),
    ]))
    assert two.is_pure_delegator is False


# --- F6: a lost migration ledger must not re-run a destructive migration ----

def test_migration_ledger_loss_preserves_follow_up_columns():
    """If schema_migrations is lost while follow_ups is already v4-shape, the
    runner must NOT re-run 001's destructive CREATE/INSERT/DROP/RENAME and
    wipe display_text/user_chosen/superseded_by; it reconciles the ledger."""
    store = ProfileStore(home=resolve_home())
    db = store.db_path
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, target_metric, "
        "baseline_value, measured_value, outcome, user_chosen, display_text, superseded_by) "
        "VALUES ('2026-W21','planning','do X','metric',5.0,NULL,'pending',1,'MY COMMITMENT',NULL)"
    )
    conn.execute("DROP TABLE schema_migrations")  # simulate ledger loss
    conn.commit()
    conn.close()

    ProfileStore(home=resolve_home())  # re-open -> migrations run with empty ledger

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT user_chosen, display_text FROM follow_ups WHERE week_iso='2026-W21'"
    ).fetchone()
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    conn.close()
    assert row == (1, "MY COMMITMENT")  # data preserved, not wiped
    assert "001_follow_ups_active_commitment.sql" in applied  # ledger reconciled
