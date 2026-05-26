"""Tests for the LLM-as-judge response parsing.

US-017 extends the judge to emit a structured `moments` array alongside
scores and rationales. These tests cover the parser and the system
prompt — they do NOT hit a live model. The Anthropic/OpenAI client
codepaths are exercised separately.
"""
from __future__ import annotations

import json

from praxis.models import Moment
from praxis.scoring.judge import (
    JudgeResult,
    _build_system_prompt,
    _parse_moments,
    _parse_response,
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
