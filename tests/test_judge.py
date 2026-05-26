"""Tests for the LLM-as-judge response parsing.

US-017 extends the judge to emit a structured `moments` array alongside
scores and rationales. US-018 adds a substring verifier that drops any
moment whose excerpt the judge invented. These tests cover the parser,
the system prompt, and the verifier - they do NOT hit a live model.
The Anthropic/OpenAI client codepaths are exercised separately.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from praxis.models import Moment, Provider, Role, Session, Turn
from praxis.scoring.judge import (
    JudgeResult,
    _build_system_prompt,
    _parse_moments,
    _parse_response,
    verify_moment_substrings,
)


def _make_session(turns: list[tuple[Role, str]]) -> Session:
    """Build a minimal Session with the given (role, content) turns."""
    return Session(
        provider=Provider.CLAUDE,
        session_id="test-session",
        started_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
        turns=[Turn(role=r, content=c) for r, c in turns],
        source_path="/tmp/fake.jsonl",
    )


def _make_moment(
    excerpt: str,
    *,
    dim_key: str = "planning",
    turn_index: int = 0,
) -> Moment:
    return Moment(
        dim_key=dim_key,
        turn_index=turn_index,
        quoted_excerpt=excerpt,
        why_it_lost_score="reason",
        suggested_alternative="alt",
        severity="minor",
    )


def _full_response_payload(moments: list[dict[str, object]] | None = None) -> str:
    """Return a JSON string matching the judge output contract."""
    payload: dict[str, object] = {
        "scores": {
            "planning": 7,
            "context": 6,
            "iteration": 5,
            "tools": 5,
            "fit": 5,
            "verification": 4,
        },
        "rationale": {
            "planning": "Stated the goal and constraints up front.",
            "context": "Pasted the failing test output.",
            "iteration": "Accepted the first answer.",
            "tools": "Used Read but no chained execution.",
            "fit": "Insufficient signal.",
            "verification": "Did not check the migration before running.",
        },
        "standout_moments": ["Stated done-when criteria"],
        "failure_modes": ["No verification before destructive op"],
        "overall_note": "Solid prompt, weak verification habit.",
    }
    if moments is not None:
        payload["moments"] = moments
    return json.dumps(payload)


def test_system_prompt_instructs_moments_array() -> None:
    """AC: judge prompt instructs model to emit a moments array per session."""
    prompt = _build_system_prompt()
    assert "moments" in prompt
    assert "at most one moment per" in prompt.lower() or "at most one" in prompt.lower()


def test_system_prompt_documents_all_required_moment_fields() -> None:
    """AC: each moment must include the 6 named fields with the right caps."""
    prompt = _build_system_prompt()
    for field in (
        "dim_key",
        "turn_index",
        "quoted_excerpt",
        "why_it_lost_score",
        "suggested_alternative",
        "severity",
    ):
        assert field in prompt, f"prompt missing field name {field!r}"
    assert "240" in prompt, "quoted_excerpt cap (240) not documented"
    assert "180" in prompt, "why_it_lost_score cap (180) not documented"
    assert "220" in prompt, "suggested_alternative cap (220) not documented"
    for sev in ("minor", "moderate", "major"):
        assert sev in prompt, f"severity value {sev!r} missing from prompt"


def test_system_prompt_documents_empty_array_for_no_lapse() -> None:
    """AC: sessions with no specific coachable lapse return an empty moments array."""
    prompt = _build_system_prompt()
    assert "empty" in prompt.lower()
    assert '"moments": []' in prompt or "moments\": []" in prompt or "empty `moments` array" in prompt.lower()


def test_parse_response_attaches_moments() -> None:
    """A well-formed moments array is parsed into typed Moment objects on JudgeResult."""
    text = _full_response_payload(
        moments=[
            {
                "dim_key": "verification",
                "turn_index": 4,
                "quoted_excerpt": "let's run the migration",
                "why_it_lost_score": "Accepted the SQL block without checking touched tables.",
                "suggested_alternative": "Ask: list every table this migration writes to, before you run it.",
                "severity": "moderate",
            }
        ]
    )
    result = _parse_response(text, model="claude-opus-4-7")
    assert isinstance(result, JudgeResult)
    assert len(result.moments) == 1
    m = result.moments[0]
    assert isinstance(m, Moment)
    assert m.dim_key == "verification"
    assert m.turn_index == 4
    assert m.severity == "moderate"


def test_parse_response_empty_moments_array_is_supported() -> None:
    """AC: an empty moments array round-trips to no Moments."""
    text = _full_response_payload(moments=[])
    result = _parse_response(text, model="claude-opus-4-7")
    assert result.moments == []


def test_parse_response_missing_moments_key_defaults_to_empty() -> None:
    """The judge omitting the key entirely should not break parsing."""
    text = _full_response_payload(moments=None)
    result = _parse_response(text, model="claude-opus-4-7")
    assert result.moments == []


def test_parse_moments_drops_moment_with_invalid_severity() -> None:
    raw = [
        {
            "dim_key": "planning",
            "turn_index": 0,
            "quoted_excerpt": "ok",
            "why_it_lost_score": "no plan",
            "suggested_alternative": "state a plan",
            "severity": "catastrophic",
        }
    ]
    assert _parse_moments(raw) == []


def test_parse_moments_drops_moment_with_unknown_dim_key() -> None:
    raw = [
        {
            "dim_key": "vibes",
            "turn_index": 0,
            "quoted_excerpt": "ok",
            "why_it_lost_score": "no",
            "suggested_alternative": "yes",
            "severity": "minor",
        }
    ]
    assert _parse_moments(raw) == []


def test_parse_moments_drops_moment_with_excerpt_over_cap() -> None:
    """quoted_excerpt > 240 chars cannot be silently truncated — that would
    break the US-018 substring check. Drop it instead."""
    raw = [
        {
            "dim_key": "planning",
            "turn_index": 0,
            "quoted_excerpt": "x" * 241,
            "why_it_lost_score": "too long",
            "suggested_alternative": "shorter",
            "severity": "minor",
        }
    ]
    assert _parse_moments(raw) == []


def test_parse_moments_truncates_why_and_alt_to_their_caps() -> None:
    """The explanation fields are free-text; over-length truncation is acceptable."""
    raw = [
        {
            "dim_key": "iteration",
            "turn_index": 1,
            "quoted_excerpt": "fine",
            "why_it_lost_score": "y" * 300,
            "suggested_alternative": "z" * 400,
            "severity": "major",
        }
    ]
    out = _parse_moments(raw)
    assert len(out) == 1
    assert len(out[0].why_it_lost_score) == 180
    assert len(out[0].suggested_alternative) == 220


def test_parse_moments_caps_one_per_dim_key() -> None:
    """Spec §4.2: at most one moment per dim per session. Defensive cap in case
    the LLM emits two."""
    raw = [
        {
            "dim_key": "verification",
            "turn_index": 2,
            "quoted_excerpt": "first",
            "why_it_lost_score": "first reason",
            "suggested_alternative": "first alt",
            "severity": "minor",
        },
        {
            "dim_key": "verification",
            "turn_index": 7,
            "quoted_excerpt": "second",
            "why_it_lost_score": "second reason",
            "suggested_alternative": "second alt",
            "severity": "major",
        },
        {
            "dim_key": "planning",
            "turn_index": 0,
            "quoted_excerpt": "plan ok",
            "why_it_lost_score": "no goal stated",
            "suggested_alternative": "state goal",
            "severity": "moderate",
        },
    ]
    out = _parse_moments(raw)
    dim_keys = [m.dim_key for m in out]
    assert dim_keys.count("verification") == 1
    assert "planning" in dim_keys
    first_verification = next(m for m in out if m.dim_key == "verification")
    assert first_verification.turn_index == 2, "the first verification moment should win"


def test_parse_moments_drops_non_dict_entries() -> None:
    raw = ["not a dict", 42, None]
    assert _parse_moments(raw) == []


def test_parse_moments_drops_missing_required_fields() -> None:
    raw = [
        {
            "dim_key": "tools",
            "turn_index": 1,
            # missing quoted_excerpt
            "why_it_lost_score": "no tools used",
            "suggested_alternative": "use Read",
            "severity": "minor",
        }
    ]
    assert _parse_moments(raw) == []


def test_parse_moments_drops_negative_turn_index() -> None:
    raw = [
        {
            "dim_key": "context",
            "turn_index": -1,
            "quoted_excerpt": "x",
            "why_it_lost_score": "y",
            "suggested_alternative": "z",
            "severity": "minor",
        }
    ]
    assert _parse_moments(raw) == []


def test_parse_moments_rejects_non_list_input() -> None:
    """A judge that returns moments as a dict (or string) should not crash."""
    assert _parse_moments({"not": "a list"}) == []
    assert _parse_moments("garbage") == []
    assert _parse_moments(None) == []


# ---------------------------------------------------------------------------
# US-018: verify_moment_substrings
# ---------------------------------------------------------------------------


def test_verify_keeps_exact_substring_match() -> None:
    """AC: an excerpt copied verbatim from the transcript passes."""
    session = _make_session(
        [(Role.USER, "Please help me write a binary search tree in Python.")]
    )
    moments = [_make_moment("help me write a binary search tree")]
    assert verify_moment_substrings(session, moments) == moments


def test_verify_passes_when_only_whitespace_differs() -> None:
    """AC: the substring check is whitespace-normalized.

    The transcript has tabs / newlines / runs of spaces; the excerpt has
    single spaces between the same words. After normalization they match.
    """
    session = _make_session(
        [(Role.USER, "Please\n\thelp  me  write\n  a   tree")]
    )
    moments = [_make_moment("help me write a tree")]
    assert verify_moment_substrings(session, moments) == moments


def test_verify_drops_invented_excerpt_and_logs(
    capsys: object,
) -> None:
    """AC: a moment whose excerpt is not in the transcript is discarded
    and a `[scorer] moment failed substring check` log line is emitted."""
    session = _make_session([(Role.USER, "I need help with my React app.")])
    moments = [_make_moment("write a Django view", dim_key="planning")]
    assert verify_moment_substrings(session, moments) == []
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "[scorer] moment failed substring check" in err


def test_verify_no_fuzzy_match_fallback(capsys: object) -> None:
    """AC: no fuzzy or approximate match is used.

    A one-character difference (extra trailing 's') is enough to fail
    the check. The wrong-quote moment must be dropped, not approximated.
    """
    session = _make_session([(Role.USER, "let's run the migration")])
    moments = [_make_moment("let's run the migrations")]
    assert verify_moment_substrings(session, moments) == []
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "[scorer] moment failed substring check" in err


def test_verify_mixed_pass_and_fail_preserves_only_survivors(
    capsys: object,
) -> None:
    """A batch with one valid and one invented moment: valid survives,
    invented is dropped, and exactly one failure line is emitted."""
    session = _make_session(
        [(Role.USER, "Please verify the SQL before running the migration.")]
    )
    moments = [
        _make_moment("verify the SQL", dim_key="verification"),
        _make_moment("totally invented text", dim_key="planning"),
    ]
    survivors = verify_moment_substrings(session, moments)
    assert len(survivors) == 1
    assert survivors[0].dim_key == "verification"
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert err.count("[scorer] moment failed substring check") == 1


def test_verify_returns_empty_for_empty_input() -> None:
    """No moments in, no moments out, no log line."""
    session = _make_session([(Role.USER, "anything")])
    assert verify_moment_substrings(session, []) == []


def test_verify_accepts_excerpt_from_assistant_turn() -> None:
    """Spec §4.1: the excerpt may be the assistant's words when that is
    what shows the missed verification. The corpus covers all turns."""
    session = _make_session(
        [
            (Role.USER, "Add an email column."),
            (
                Role.ASSISTANT,
                "Done. I added the email column without backfilling existing rows.",
            ),
        ]
    )
    moments = [
        _make_moment(
            "added the email column without backfilling",
            dim_key="verification",
        )
    ]
    assert verify_moment_substrings(session, moments) == moments


def test_verify_excerpt_can_span_turn_boundary() -> None:
    """Turns are joined with a single space in the corpus, so an excerpt
    that straddles two adjacent turns matches after normalization."""
    session = _make_session(
        [
            (Role.USER, "Run the migration."),
            (Role.ASSISTANT, "OK, applying."),
        ]
    )
    moments = [_make_moment("migration. OK")]
    assert verify_moment_substrings(session, moments) == moments


def test_verify_drops_whitespace_only_excerpt(capsys: object) -> None:
    """An excerpt that normalizes to an empty string must not vacuously
    match (`'' in transcript` is always True)."""
    session = _make_session([(Role.USER, "real content here")])
    moments = [_make_moment("   \n\t  ")]
    assert verify_moment_substrings(session, moments) == []
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "[scorer] moment failed substring check" in err


def test_verify_log_line_includes_dim_and_turn_context(
    capsys: object,
) -> None:
    """The exact prefix `[scorer] moment failed substring check` is
    required; extra context after it makes failures debuggable."""
    session = _make_session([(Role.USER, "actual transcript content")])
    moments = [
        _make_moment("invented quote", dim_key="iteration", turn_index=3)
    ]
    verify_moment_substrings(session, moments)
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "[scorer] moment failed substring check" in err
    assert "iteration" in err
    assert "turn=3" in err


def test_verify_case_sensitive_match() -> None:
    """Spec §4.4 specifies whitespace normalization only - case is NOT
    normalized. An excerpt that differs only in case is not a substring."""
    session = _make_session([(Role.USER, "Run the Migration")])
    moments = [_make_moment("run the migration")]
    assert verify_moment_substrings(session, moments) == []


def test_verify_does_not_mutate_input_list() -> None:
    """The verifier returns a new list and does not mutate moments in place."""
    session = _make_session([(Role.USER, "good content")])
    good = _make_moment("good content", dim_key="planning")
    bad = _make_moment("invented", dim_key="iteration")
    inputs = [good, bad]
    verify_moment_substrings(session, inputs)
    assert inputs == [good, bad]
