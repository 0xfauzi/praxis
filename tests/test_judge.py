"""Tests for the LLM-as-judge response parsing.

US-017 extends the judge to emit a structured `moments` array alongside
scores and rationales. US-018 adds a substring verifier that drops any
moment whose excerpt the judge invented. US-019 adds a confidence
self-flag and confidence_reason. These tests cover the parser, the
system prompt, and the verifier - they do NOT hit a live model. The
Anthropic/OpenAI client codepaths are exercised separately.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from praxis.models import Moment, Provider, Role, Session, Turn
from praxis.scoring.judge import (
    CLAUDE_CHEAP_MODEL,
    CLAUDE_FRONTIER_MODEL,
    OPENAI_CHEAP_MODEL,
    OPENAI_FRONTIER_MODEL,
    JudgeResult,
    _build_system_prompt,
    _parse_confidence,
    _parse_moments,
    _parse_response,
    derive_coach_line,
    score_session_pass1,
    score_session_pass2,
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


_SENTINEL = object()


def _full_response_payload(
    moments: list[dict[str, object]] | None = None,
    *,
    confidence: object = _SENTINEL,
    confidence_reason: object = _SENTINEL,
) -> str:
    """Return a JSON string matching the judge output contract.

    Pass `_SENTINEL` (the default) to omit the key entirely; pass any
    other value (including None) to include the key with that value.
    """
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
    if confidence is not _SENTINEL:
        payload["confidence"] = confidence
    if confidence_reason is not _SENTINEL:
        payload["confidence_reason"] = confidence_reason
    return json.dumps(payload)


def test_system_prompt_instructs_moments_array() -> None:
    """AC: judge prompt instructs model to emit a moments array per session."""
    prompt = _build_system_prompt()
    assert "moments" in prompt
    assert "at most one moment per" in prompt.lower() or "at most one" in prompt.lower()


def test_system_prompt_documents_all_required_moment_fields() -> None:
    """AC: each moment must include the 7 named fields with the right caps."""
    prompt = _build_system_prompt()
    for field in (
        "dim_key",
        "turn_index",
        "quoted_excerpt",
        "why_it_lost_score",
        "suggested_alternative",
        "coach_line",
        "severity",
    ):
        assert field in prompt, f"prompt missing field name {field!r}"
    assert "240" in prompt, "quoted_excerpt cap (240) not documented"
    assert "180" in prompt, "why_it_lost_score cap (180) not documented"
    assert "220" in prompt, "suggested_alternative cap (220) not documented"
    assert "second person" in prompt.lower(), "coach_line voice not documented"
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


def test_parse_moments_reads_coach_line_when_present() -> None:
    """The judge's second-person coach_line is parsed onto the Moment."""
    moments = _parse_moments([
        {
            "dim_key": "tools",
            "turn_index": 2,
            "quoted_excerpt": "here are the results",
            "why_it_lost_score": "Results were pasted, not run.",
            "suggested_alternative": "Ask the agent to run the checks.",
            "coach_line": "You pasted the results instead of having the agent run them.",
            "severity": "moderate",
        }
    ])
    assert len(moments) == 1
    assert moments[0].coach_line == (
        "You pasted the results instead of having the agent run them."
    )


def test_parse_moments_derives_coach_line_when_missing() -> None:
    """A moment that omits coach_line is kept, with a fallback derived from why."""
    moments = _parse_moments([
        {
            "dim_key": "planning",
            "turn_index": 0,
            "quoted_excerpt": "build it",
            "why_it_lost_score": "You never named which story to implement first.",
            "suggested_alternative": "Name the story.",
            "severity": "minor",
        }
    ])
    assert len(moments) == 1, "missing coach_line must not drop the moment"
    assert moments[0].coach_line == "You never named which story to implement first."


def test_parse_moments_caps_coach_line_length() -> None:
    long = "You " + "x" * 300
    moments = _parse_moments([
        {
            "dim_key": "context", "turn_index": 1, "quoted_excerpt": "q",
            "why_it_lost_score": "w", "suggested_alternative": "a",
            "coach_line": long, "severity": "minor",
        }
    ])
    assert len(moments[0].coach_line) <= 110


def test_derive_coach_line_trims_to_one_clean_clause() -> None:
    why = ("You documented the type error but did not ask the assistant to "
           "produce a fix, re-run the checks, or confirm the failure cleared.")
    line = derive_coach_line(why)
    assert len(line) <= 110
    assert line.endswith(".")
    assert "..." not in line
    assert line.startswith("You documented the type error")


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


# ---------------------------------------------------------------------------
# US-019: confidence (low/medium/high) + confidence_reason
# ---------------------------------------------------------------------------


def test_system_prompt_includes_confidence_calibration_section() -> None:
    """AC: spec §9.3 calibration instructions are present in the prompt.

    The prompt must instruct the model on what high/medium/low mean,
    and request a one-sentence confidence_reason.
    """
    prompt = _build_system_prompt()
    assert "confidence" in prompt.lower()
    assert "confidence_reason" in prompt
    for level in ("low", "medium", "high"):
        assert level in prompt.lower(), f"confidence level {level!r} missing from prompt"
    # The calibration anchors per spec §9.3.
    assert "default to" in prompt.lower() or "default when uncertain" in prompt.lower()
    assert "second" in prompt.lower(), "prompt should mention the 'second opinion' framing"


def test_system_prompt_documents_confidence_in_output_shape() -> None:
    """AC: the JSON output shape example shows confidence + confidence_reason."""
    prompt = _build_system_prompt()
    assert '"confidence"' in prompt
    assert '"confidence_reason"' in prompt
    assert "low|medium|high" in prompt or "low | medium | high" in prompt


def test_parse_response_extracts_high_confidence() -> None:
    text = _full_response_payload(
        confidence="high",
        confidence_reason="Six dims all had clear signal across 28 turns.",
    )
    result = _parse_response(text, model="claude-opus-4-7")
    assert isinstance(result, JudgeResult)
    assert result.confidence == "high"
    assert result.confidence_reason == "Six dims all had clear signal across 28 turns."


def test_parse_response_extracts_medium_confidence() -> None:
    text = _full_response_payload(
        confidence="medium",
        confidence_reason="Tools dim is weak; everything else is solid.",
    )
    result = _parse_response(text, model="claude-opus-4-7")
    assert result.confidence == "medium"
    assert result.confidence_reason == "Tools dim is weak; everything else is solid."


def test_parse_response_extracts_low_confidence() -> None:
    text = _full_response_payload(
        confidence="low",
        confidence_reason="Transcript was a 2-turn fragment; nothing to ground rationale on.",
    )
    result = _parse_response(text, model="claude-opus-4-7")
    assert result.confidence == "low"
    assert "fragment" in result.confidence_reason


def test_parse_response_missing_confidence_defaults_to_medium() -> None:
    """A judge that omits confidence should not crash; default to medium so
    a missing self-rating does not trigger pass 2 escalation."""
    text = _full_response_payload()  # no confidence keys at all
    result = _parse_response(text, model="claude-opus-4-7")
    assert result.confidence == "medium"
    assert result.confidence_reason == ""


def test_parse_response_invalid_confidence_value_defaults_to_medium() -> None:
    """Unknown enum value (e.g. 'very-high') falls back to medium."""
    text = _full_response_payload(
        confidence="very-high",
        confidence_reason="bogus",
    )
    result = _parse_response(text, model="claude-opus-4-7")
    assert result.confidence == "medium"
    # confidence_reason is still kept; it's free-text, not enum-validated.
    assert result.confidence_reason == "bogus"


def test_parse_response_non_string_confidence_defaults_to_medium() -> None:
    """A numeric or null confidence value should not crash."""
    text = _full_response_payload(
        confidence=3,
        confidence_reason=None,
    )
    result = _parse_response(text, model="claude-opus-4-7")
    assert result.confidence == "medium"
    assert result.confidence_reason == ""


def test_parse_confidence_unit() -> None:
    """The _parse_confidence helper coerces raw judge output to a typed pair."""
    assert _parse_confidence("low", "short transcript") == ("low", "short transcript")
    assert _parse_confidence("medium", "") == ("medium", "")
    assert _parse_confidence("high", "all clear") == ("high", "all clear")
    # Invalid enum -> medium default.
    assert _parse_confidence("MAYBE", "x") == ("medium", "x")
    # Missing keys (None) -> medium + empty.
    assert _parse_confidence(None, None) == ("medium", "")
    # Non-string reason -> empty.
    assert _parse_confidence("high", 42) == ("high", "")


def test_judge_result_default_confidence_is_medium() -> None:
    """A JudgeResult constructed without confidence defaults to medium / empty
    reason, mirroring the parser's missing-key behavior."""
    r = JudgeResult(
        dimension_scores={},
        rationale={},
        standout_moments=[],
        failure_modes=[],
        overall_note="",
        judge_model="m",
    )
    assert r.confidence == "medium"
    assert r.confidence_reason == ""


# ---------------------------------------------------------------------------
# US-027: pass 1 entrypoint (cheap-tier judge that runs on every session)
# ---------------------------------------------------------------------------


def test_pass1_constants_match_spec() -> None:
    """Spec §9.1: pass 1 uses haiku / gpt-5-mini; pass 2 uses opus / gpt-5."""
    assert CLAUDE_CHEAP_MODEL == "claude-haiku-4-5"
    assert OPENAI_CHEAP_MODEL == "gpt-5-mini"
    assert CLAUDE_FRONTIER_MODEL == "claude-opus-4-7"
    assert OPENAI_FRONTIER_MODEL == "gpt-5"


def test_pass1_returns_none_without_api_keys(monkeypatch) -> None:
    """US-027: pass 1 cannot fabricate scores; without keys it returns None.

    Returning None here means "this session is unjudged in this run", not
    "skip it on heuristic grounds". The orchestrator's caller already
    differentiates: a None pass-1 result is logged, never substituted.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    session = _make_session([(Role.USER, "anything")])
    assert score_session_pass1(session) is None


def test_pass1_uses_cheap_claude_model_when_preferred(monkeypatch) -> None:
    """Pass 1 calls Claude with CLAUDE_CHEAP_MODEL when anthropic is the preference."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: dict[str, object] = {}

    def _fake(session, model=CLAUDE_FRONTIER_MODEL, **kwargs):  # noqa: ARG001
        seen["model"] = model
        return JudgeResult(
            dimension_scores={}, rationale={}, standout_moments=[],
            failure_modes=[], overall_note="", judge_model=model,
        )

    monkeypatch.setattr("praxis.scoring.judge.score_with_claude", _fake)
    result = score_session_pass1(_make_session([(Role.USER, "x")]))
    assert result is not None
    assert seen["model"] == CLAUDE_CHEAP_MODEL


def test_pass1_uses_cheap_openai_model_when_preferred(monkeypatch) -> None:
    """When the openai provider is preferred, pass 1 uses OPENAI_CHEAP_MODEL."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    seen: dict[str, object] = {}

    def _fake(session, model=OPENAI_FRONTIER_MODEL, **kwargs):  # noqa: ARG001
        seen["model"] = model
        return JudgeResult(
            dimension_scores={}, rationale={}, standout_moments=[],
            failure_modes=[], overall_note="", judge_model=model,
        )

    monkeypatch.setattr("praxis.scoring.judge.score_with_openai", _fake)
    result = score_session_pass1(_make_session([(Role.USER, "x")]), prefer="openai")
    assert result is not None
    assert seen["model"] == OPENAI_CHEAP_MODEL


def test_pass1_falls_back_to_other_provider_on_error(monkeypatch) -> None:
    """If the preferred provider raises, pass 1 tries the other one (still cheap-tier)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    seen_openai: dict[str, object] = {}

    def _broken_claude(session, model=CLAUDE_FRONTIER_MODEL, **kwargs):  # noqa: ARG001
        raise RuntimeError("anthropic down")

    def _fake_openai(session, model=OPENAI_FRONTIER_MODEL, **kwargs):  # noqa: ARG001
        seen_openai["model"] = model
        return JudgeResult(
            dimension_scores={}, rationale={}, standout_moments=[],
            failure_modes=[], overall_note="", judge_model=model,
        )

    monkeypatch.setattr("praxis.scoring.judge.score_with_claude", _broken_claude)
    monkeypatch.setattr("praxis.scoring.judge.score_with_openai", _fake_openai)

    result = score_session_pass1(_make_session([(Role.USER, "x")]))
    assert result is not None
    assert seen_openai["model"] == OPENAI_CHEAP_MODEL


# ---------------------------------------------------------------------------
# US-028: pass 2 entrypoint (frontier judge for low-confidence sessions)
# ---------------------------------------------------------------------------


def test_pass2_returns_none_without_api_keys(monkeypatch) -> None:
    """Pass 2 cannot fabricate scores either; without keys it returns None.

    The orchestrator should then keep the pass-1 score for that session
    rather than substituting a fallback.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    session = _make_session([(Role.USER, "anything")])
    assert score_session_pass2(session) is None


def test_pass2_uses_frontier_claude_model_when_preferred(monkeypatch) -> None:
    """Pass 2 calls Claude with CLAUDE_FRONTIER_MODEL (opus), not the cheap tier."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: dict[str, object] = {}

    def _fake(session, model=CLAUDE_FRONTIER_MODEL):  # noqa: ARG001
        seen["model"] = model
        return JudgeResult(
            dimension_scores={}, rationale={}, standout_moments=[],
            failure_modes=[], overall_note="", judge_model=model,
        )

    monkeypatch.setattr("praxis.scoring.judge.score_with_claude", _fake)
    result = score_session_pass2(_make_session([(Role.USER, "x")]))
    assert result is not None
    assert seen["model"] == CLAUDE_FRONTIER_MODEL


def test_pass2_uses_frontier_openai_model_when_preferred(monkeypatch) -> None:
    """When openai is preferred, pass 2 uses OPENAI_FRONTIER_MODEL (gpt-5)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    seen: dict[str, object] = {}

    def _fake(session, model=OPENAI_FRONTIER_MODEL):  # noqa: ARG001
        seen["model"] = model
        return JudgeResult(
            dimension_scores={}, rationale={}, standout_moments=[],
            failure_modes=[], overall_note="", judge_model=model,
        )

    monkeypatch.setattr("praxis.scoring.judge.score_with_openai", _fake)
    result = score_session_pass2(_make_session([(Role.USER, "x")]), prefer="openai")
    assert result is not None
    assert seen["model"] == OPENAI_FRONTIER_MODEL


def test_pass2_receives_only_the_session_no_pass1_context(monkeypatch) -> None:
    """Spec §9.1 / AC: pass-2 prompts do not include pass-1 outputs.

    The pass-2 entrypoint must hand the provider client only the session
    (and a model id). It must not accept or forward any JudgeResult,
    score dict, rationale, or moments list from pass 1.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    captured_calls: list[tuple[tuple, dict]] = []

    def _fake(*args, **kwargs):
        captured_calls.append((args, kwargs))
        return JudgeResult(
            dimension_scores={}, rationale={}, standout_moments=[],
            failure_modes=[], overall_note="", judge_model=CLAUDE_FRONTIER_MODEL,
        )

    monkeypatch.setattr("praxis.scoring.judge.score_with_claude", _fake)
    session = _make_session([(Role.USER, "x")])
    score_session_pass2(session)
    assert len(captured_calls) == 1
    args, kwargs = captured_calls[0]
    # Only the session may be a positional, with at most the model id as a kwarg.
    assert len(args) == 1 and args[0] is session
    assert set(kwargs.keys()) <= {"model"}


# ---------------------------------------------------------------------------
# US-031: prompt calibration adjustments from rolling 4-week telemetry
# ---------------------------------------------------------------------------


def test_system_prompt_unchanged_when_no_calibration_flags() -> None:
    """Default prompt has no calibration-check addendum.

    The pre-US-031 prompt is the baseline that pass-1 emits when the
    rolling 4-week share is within bounds. The phrase only appears when
    the orchestrator opts in via a flag, so a stock prompt must omit it.
    """
    prompt = _build_system_prompt()
    assert "Calibration check" not in prompt


def test_system_prompt_sharpens_high_when_flagged() -> None:
    """AC: sharpen_calibration injects an over-confidence anchor into the prompt.

    The addendum must specifically address the "high" rating - the only
    knob that would correct an over-confidence pattern - and must direct
    the model to default to medium when in doubt.
    """
    prompt = _build_system_prompt(sharpen_calibration=True)
    assert "Calibration check" in prompt
    assert "90%" in prompt
    assert "over-confidence" in prompt.lower()
    # The instruction must reach the "high" rating, not just be a generic note.
    assert "\"high\"" in prompt or "high" in prompt.lower()


def test_system_prompt_tightens_low_when_flagged() -> None:
    """AC: stricter_low injects an over-flagging anchor into the prompt.

    The addendum must reference the 70% threshold and must constrain "low"
    to genuinely-ambiguous transcripts so the cheap model stops escalating
    everything.
    """
    prompt = _build_system_prompt(stricter_low=True)
    assert "Calibration check" in prompt
    assert "70%" in prompt
    assert "over-flagging" in prompt.lower()


def test_system_prompt_can_apply_both_flags_simultaneously() -> None:
    """Both auto-tunes can fire on the same run when the rolling shares
    happen to cross both thresholds at once. Each addendum must be present
    so the model sees both calibration anchors."""
    prompt = _build_system_prompt(sharpen_calibration=True, stricter_low=True)
    assert prompt.count("Calibration check") == 2
    assert "90%" in prompt
    assert "70%" in prompt


def test_score_session_pass1_forwards_calibration_flags(monkeypatch) -> None:
    """The flag must reach score_with_claude so the cheap-tier prompt is
    actually adjusted - not just consumed by score_session_pass1 and dropped."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured: dict[str, object] = {}

    def _fake(session, model=CLAUDE_FRONTIER_MODEL, **kwargs):  # noqa: ARG001
        captured["sharpen_calibration"] = kwargs.get("sharpen_calibration")
        captured["stricter_low"] = kwargs.get("stricter_low")
        return JudgeResult(
            dimension_scores={}, rationale={}, standout_moments=[],
            failure_modes=[], overall_note="", judge_model=model,
        )

    monkeypatch.setattr("praxis.scoring.judge.score_with_claude", _fake)
    score_session_pass1(
        _make_session([(Role.USER, "x")]),
        sharpen_calibration=True,
        stricter_low=True,
    )
    assert captured["sharpen_calibration"] is True
    assert captured["stricter_low"] is True
