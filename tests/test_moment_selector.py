"""Tests for praxis.scoring.moment_selector (US-029).

These tests exercise:
  - the candidate payload contains every field the selector prompt
    promises the model it will see
  - the system prompt declares the response shape and the "different
    dims" / "up to two supporting" guidance
  - the right cheap-tier model is picked for each primary_provider
  - JSON parsing tolerates markdown fences and preamble
  - supporting_moment_ids is capped at two regardless of what the
    model returns
  - empty candidates short-circuits to None with no LLM call
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from praxis.scoring.moment_selector import (
    PRIMARY_CHEAP_MODELS,
    InvalidMomentSelectionError,
    Moment,
    MomentCandidate,
    MomentSelection,
    _build_user_prompt,
    _candidate_payload,
    _fallback_selection,
    _invalid_ids,
    _parse_selection,
    _retry_user_prompt,
    _SYSTEM_PROMPT,
    cheap_model_for,
    select_moments,
    select_moments_with_fallback,
)


def _moment(
    moment_id: str = "m1",
    dim_key: str = "verification",
    severity: str = "moderate",
    dollar_impact_estimate: float | None = 1.25,
) -> Moment:
    return Moment(
        moment_id=moment_id,
        session_stable_id="abc123",
        dim_key=dim_key,
        turn_index=4,
        quoted_excerpt="ran the migration without checking what it touched",
        why_it_lost_score="accepted the SQL block without listing affected tables",
        suggested_alternative="ask: list every table this migration writes to",
        severity=severity,  # type: ignore[arg-type]
        created_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        dollar_impact_estimate=dollar_impact_estimate,
        minutes_impact_estimate=8,
    )


def _candidate(
    moment_id: str = "m1",
    dim_key: str = "verification",
    severity: str = "moderate",
    recurrence_count: int = 2,
    session_started_at: datetime | None = None,
    dollar_impact_estimate: float | None = 1.25,
) -> MomentCandidate:
    return MomentCandidate(
        moment=_moment(
            moment_id=moment_id,
            dim_key=dim_key,
            severity=severity,
            dollar_impact_estimate=dollar_impact_estimate,
        ),
        session_started_at=session_started_at
        or datetime(2026, 5, 20, 11, 30, tzinfo=timezone.utc),
        recurrence_count=recurrence_count,
    )


# ---------- prompt construction ----------


def test_candidate_payload_includes_all_required_fields():
    """Each candidate must include the 8 fields enumerated in the
    US-029 acceptance criteria."""
    payload = _candidate_payload(_candidate())
    required = {
        "dim_key",
        "quoted_excerpt",
        "why_it_lost_score",
        "suggested_alternative",
        "severity",
        "session_started_at",
        "dollar_impact_estimate",
        "recurrence_count",
    }
    assert required.issubset(set(payload.keys()))


def test_candidate_payload_carries_moment_id_for_reference():
    """The LLM needs to refer back to a moment by id when it picks
    a headline or supporting; moment_id must be in the payload."""
    payload = _candidate_payload(_candidate(moment_id="abcdef0123"))
    assert payload["moment_id"] == "abcdef0123"


def test_candidate_payload_serializes_session_started_at_as_iso():
    when = datetime(2026, 5, 18, 9, 15, tzinfo=timezone.utc)
    payload = _candidate_payload(_candidate(session_started_at=when))
    assert payload["session_started_at"] == when.isoformat()


def test_candidate_payload_passes_none_dollar_impact_through():
    payload = _candidate_payload(_candidate(dollar_impact_estimate=None))
    assert payload["dollar_impact_estimate"] is None


def test_user_prompt_serializes_all_candidates_as_json():
    cands = [
        _candidate(moment_id="m1", dim_key="verification"),
        _candidate(moment_id="m2", dim_key="planning", recurrence_count=0),
    ]
    prompt = _build_user_prompt(cands)
    assert "m1" in prompt
    assert "m2" in prompt
    assert "verification" in prompt
    assert "planning" in prompt
    # The JSON block round-trips to a list of 2 dicts.
    start = prompt.find("[")
    end = prompt.rfind("]")
    parsed = json.loads(prompt[start : end + 1])
    assert isinstance(parsed, list) and len(parsed) == 2


def test_system_prompt_declares_response_shape():
    """Prompt must name the three response fields explicitly so the
    model has the schema in hand."""
    assert "headline_moment_id" in _SYSTEM_PROMPT
    assert "headline_reason" in _SYSTEM_PROMPT
    assert "supporting_moment_ids" in _SYSTEM_PROMPT


def test_system_prompt_caps_supporting_at_two():
    """Acceptance: 'supporting_moment_ids (up to 2)'. The instruction
    must be in the prompt, not just the parser."""
    assert "up to two" in _SYSTEM_PROMPT.lower() or "up to 2" in _SYSTEM_PROMPT


def test_system_prompt_asks_one_sentence_headline_reason():
    """Acceptance: 'headline_reason (one sentence)'."""
    assert "one sentence" in _SYSTEM_PROMPT.lower()


def test_system_prompt_prefers_different_dims_for_supporting():
    """Acceptance: 'Supporting moments prefer different dims from
    the headline'. This is an LLM behavior, but the instruction
    must be present in the prompt for it to happen."""
    assert "different dim" in _SYSTEM_PROMPT.lower()


def test_system_prompt_names_every_candidate_field():
    """All 8 required candidate fields must be listed in the prompt
    so the model knows what each entry will contain."""
    for field in (
        "dim_key",
        "quoted_excerpt",
        "why_it_lost_score",
        "suggested_alternative",
        "severity",
        "session_started_at",
        "dollar_impact_estimate",
        "recurrence_count",
    ):
        assert field in _SYSTEM_PROMPT, f"system prompt missing field: {field}"


# ---------- model selection ----------


def test_cheap_model_for_anthropic_is_haiku():
    assert cheap_model_for("anthropic") == "claude-haiku-4-5"


def test_cheap_model_for_openai_is_gpt5_mini():
    assert cheap_model_for("openai") == "gpt-5-mini"


def test_cheap_model_defaults_to_anthropic_when_unknown():
    # Spec section 12.2: anthropic is the default primary provider.
    assert cheap_model_for("nope") == PRIMARY_CHEAP_MODELS["anthropic"]


def test_select_moments_passes_haiku_to_caller_for_anthropic():
    seen: dict[str, str] = {}

    def stub(system: str, user: str, model: str) -> str:
        seen["system"] = system
        seen["user"] = user
        seen["model"] = model
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "biggest recurrence and clear alternative.",
                "supporting_moment_ids": [],
            }
        )

    out = select_moments([_candidate()], primary_provider="anthropic", llm_caller=stub)
    assert out is not None
    assert seen["model"] == "claude-haiku-4-5"


def test_select_moments_passes_gpt5_mini_to_caller_for_openai():
    seen: dict[str, str] = {}

    def stub(system: str, user: str, model: str) -> str:
        seen["model"] = model
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "x.",
                "supporting_moment_ids": [],
            }
        )

    select_moments([_candidate()], primary_provider="openai", llm_caller=stub)
    assert seen["model"] == "gpt-5-mini"


# ---------- response parsing ----------


def test_parses_valid_json_response_to_selection():
    text = json.dumps(
        {
            "headline_moment_id": "m1",
            "headline_reason": "highest dollar impact and recurs for the third week.",
            "supporting_moment_ids": ["m2", "m3"],
        }
    )
    out = _parse_selection(text)
    assert out == MomentSelection(
        headline_moment_id="m1",
        headline_reason="highest dollar impact and recurs for the third week.",
        supporting_moment_ids=["m2", "m3"],
    )


def test_parses_response_wrapped_in_markdown_fences():
    text = (
        "```json\n"
        + json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "x.",
                "supporting_moment_ids": ["m2"],
            }
        )
        + "\n```"
    )
    out = _parse_selection(text)
    assert out.headline_moment_id == "m1"
    assert out.supporting_moment_ids == ["m2"]


def test_parses_response_with_preamble():
    text = (
        "Sure, here is the selection:\n"
        + json.dumps(
            {
                "headline_moment_id": "abc",
                "headline_reason": "y.",
                "supporting_moment_ids": [],
            }
        )
    )
    out = _parse_selection(text)
    assert out.headline_moment_id == "abc"


def test_parse_caps_supporting_at_two_even_if_model_returns_more():
    # Even if a chatty model returns three or four supporting ids,
    # the parser keeps only the first two -- spec section 4.3 caps
    # total digest moments at 3 (headline + at most 2 supporting).
    text = json.dumps(
        {
            "headline_moment_id": "m1",
            "headline_reason": "x.",
            "supporting_moment_ids": ["m2", "m3", "m4", "m5"],
        }
    )
    out = _parse_selection(text)
    assert out.supporting_moment_ids == ["m2", "m3"]


def test_parse_rejects_response_without_headline_moment_id():
    text = json.dumps(
        {
            "headline_reason": "missing headline",
            "supporting_moment_ids": [],
        }
    )
    with pytest.raises(ValueError):
        _parse_selection(text)


def test_parse_rejects_non_json():
    with pytest.raises(ValueError):
        _parse_selection("this is not JSON at all")


# ---------- end-to-end with stub caller ----------


def test_select_moments_returns_none_for_empty_candidates():
    calls: list[tuple[str, str, str]] = []

    def stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        return "{}"

    out = select_moments([], primary_provider="anthropic", llm_caller=stub)
    assert out is None
    # Critically: no LLM call was made.
    assert calls == []


def test_select_moments_round_trips_through_stub():
    captured: dict[str, str] = {}

    def stub(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return json.dumps(
            {
                "headline_moment_id": "mA",
                "headline_reason": "recurs across all 3 prior weeks and the alternative is concrete.",
                "supporting_moment_ids": ["mB"],
            }
        )

    cands = [
        _candidate(moment_id="mA", dim_key="verification", recurrence_count=3),
        _candidate(moment_id="mB", dim_key="planning", recurrence_count=0),
        _candidate(moment_id="mC", dim_key="iteration", recurrence_count=1),
    ]
    out = select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    assert out == MomentSelection(
        headline_moment_id="mA",
        headline_reason="recurs across all 3 prior weeks and the alternative is concrete.",
        supporting_moment_ids=["mB"],
    )
    # The user prompt the stub saw mentioned every candidate.
    for mid in ("mA", "mB", "mC"):
        assert mid in captured["user"]


def test_select_moments_serializes_recurrence_and_dollar_impact_in_prompt():
    captured: dict[str, str] = {}

    def stub(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "x.",
                "supporting_moment_ids": [],
            }
        )

    cand = _candidate(
        moment_id="m1", recurrence_count=2, dollar_impact_estimate=4.5
    )
    select_moments([cand], primary_provider="anthropic", llm_caller=stub)
    # Both numbers are in the JSON payload the LLM saw.
    assert '"recurrence_count": 2' in captured["user"]
    assert '"dollar_impact_estimate": 4.5' in captured["user"]


# ---------- US-030: validate IDs, re-prompt once on failure ----------


def test_invalid_ids_helper_empty_when_all_valid():
    sel = MomentSelection(
        headline_moment_id="m1",
        headline_reason="x.",
        supporting_moment_ids=["m2", "m3"],
    )
    assert _invalid_ids(sel, {"m1", "m2", "m3"}) == []


def test_invalid_ids_helper_returns_headline_then_supporting_in_order():
    sel = MomentSelection(
        headline_moment_id="BAD_H",
        headline_reason="x.",
        supporting_moment_ids=["m2", "BAD_S"],
    )
    assert _invalid_ids(sel, {"m2"}) == ["BAD_H", "BAD_S"]


def test_invalid_ids_helper_dedupes_repeats():
    sel = MomentSelection(
        headline_moment_id="BAD",
        headline_reason="x.",
        supporting_moment_ids=["BAD", "BAD"],
    )
    assert _invalid_ids(sel, set()) == ["BAD"]


def test_retry_user_prompt_names_each_invalid_id_and_candidate_set():
    out = _retry_user_prompt("ORIGINAL_PROMPT", ["BAD1", "BAD2"])
    assert "ORIGINAL_PROMPT" in out
    assert "BAD1" in out
    assert "BAD2" in out
    assert "candidate set" in out


def test_select_moments_does_not_reprompt_when_all_ids_valid():
    """If the first response references only valid ids, no second LLM
    call is made -- the retry budget is reserved for actual failures."""
    calls: list[tuple[str, str, str]] = []

    def stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "x.",
                "supporting_moment_ids": ["m2"],
            }
        )

    cands = [_candidate(moment_id="m1"), _candidate(moment_id="m2")]
    out = select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    assert out is not None
    assert out.headline_moment_id == "m1"
    assert len(calls) == 1


def test_select_moments_reprompts_when_headline_id_unknown():
    """First response's headline is not in the candidate set: the LLM
    is called a second time with the bad id named in the user prompt."""
    calls: list[tuple[str, str, str]] = []

    def stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        if len(calls) == 1:
            return json.dumps(
                {
                    "headline_moment_id": "BOGUS",
                    "headline_reason": "x.",
                    "supporting_moment_ids": [],
                }
            )
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "corrected.",
                "supporting_moment_ids": [],
            }
        )

    cands = [_candidate(moment_id="m1")]
    out = select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    assert out is not None
    assert out.headline_moment_id == "m1"
    assert out.headline_reason == "corrected."
    assert len(calls) == 2
    assert "BOGUS" in calls[1][1]


def test_select_moments_reprompts_when_supporting_id_unknown():
    """One supporting id is bogus: re-prompt, recover."""
    calls: list[tuple[str, str, str]] = []

    def stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        if len(calls) == 1:
            return json.dumps(
                {
                    "headline_moment_id": "m1",
                    "headline_reason": "x.",
                    "supporting_moment_ids": ["m2", "GHOST"],
                }
            )
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "x.",
                "supporting_moment_ids": ["m2"],
            }
        )

    cands = [_candidate(moment_id="m1"), _candidate(moment_id="m2")]
    out = select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    assert out is not None
    assert out.supporting_moment_ids == ["m2"]
    assert len(calls) == 2
    assert "GHOST" in calls[1][1]


def test_select_moments_retry_prompt_lists_every_invalid_id():
    """When several ids are wrong, the retry message names them all so
    the model knows the full set of corrections to make."""
    calls: list[tuple[str, str, str]] = []

    def stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        if len(calls) == 1:
            return json.dumps(
                {
                    "headline_moment_id": "BAD1",
                    "headline_reason": "x.",
                    "supporting_moment_ids": ["BAD2", "BAD3"],
                }
            )
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "x.",
                "supporting_moment_ids": [],
            }
        )

    cands = [_candidate(moment_id="m1")]
    select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    retry_user = calls[1][1]
    assert "BAD1" in retry_user
    assert "BAD2" in retry_user
    assert "BAD3" in retry_user


def test_select_moments_retry_includes_original_candidate_payload():
    """The retry must include the original candidate list -- otherwise
    the model is guessing without context. The retry user prompt is the
    original prompt + an error tail."""
    calls: list[tuple[str, str, str]] = []

    def stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        if len(calls) == 1:
            return json.dumps(
                {
                    "headline_moment_id": "WRONG",
                    "headline_reason": "x.",
                    "supporting_moment_ids": [],
                }
            )
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "x.",
                "supporting_moment_ids": [],
            }
        )

    cands = [
        _candidate(moment_id="m1", dim_key="verification"),
        _candidate(moment_id="m2", dim_key="planning"),
    ]
    select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    retry_user = calls[1][1]
    # Original candidates still present in the retry prompt.
    assert "m1" in retry_user
    assert "m2" in retry_user
    assert "verification" in retry_user
    assert "planning" in retry_user


def test_select_moments_raises_when_retry_response_still_invalid():
    """If the second attempt still references unknown ids, raise
    InvalidMomentSelectionError so the US-031 fallback path can catch
    it. The error message names the offending id(s)."""
    calls: list[tuple[str, str, str]] = []

    def stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        return json.dumps(
            {
                "headline_moment_id": "STILL_BAD",
                "headline_reason": "x.",
                "supporting_moment_ids": [],
            }
        )

    cands = [_candidate(moment_id="m1")]
    with pytest.raises(InvalidMomentSelectionError) as excinfo:
        select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    assert "STILL_BAD" in str(excinfo.value)


def test_select_moments_never_calls_llm_a_third_time():
    """The retry budget is exactly one. Two failures must NOT trigger a
    third attempt -- that would be a runaway cost and contradicts the
    US-030 acceptance criterion 're-prompted once'."""
    n = 0

    def stub(system: str, user: str, model: str) -> str:
        nonlocal n
        n += 1
        return json.dumps(
            {
                "headline_moment_id": "NOPE",
                "headline_reason": "x.",
                "supporting_moment_ids": [],
            }
        )

    cands = [_candidate(moment_id="m1")]
    with pytest.raises(InvalidMomentSelectionError):
        select_moments(cands, primary_provider="anthropic", llm_caller=stub)
    assert n == 2


def test_invalid_moment_selection_error_is_a_value_error():
    """InvalidMomentSelectionError must subclass ValueError so callers
    that catch the broad shape still work; US-031 can catch the narrow
    type for its fallback path."""
    assert issubclass(InvalidMomentSelectionError, ValueError)


# ---------- US-031: fallback on protocol failure ----------


def _bad_stub(system: str, user: str, model: str) -> str:
    """Stub that always returns an id not in any sane candidate set,
    forcing select_moments to exhaust its one retry and raise."""
    return json.dumps(
        {
            "headline_moment_id": "NOT_IN_SET",
            "headline_reason": "x.",
            "supporting_moment_ids": [],
        }
    )


def test_fallback_selection_picks_most_recent_major_by_session_time():
    """Most-recent here means largest session_started_at -- the
    conversation that happened most recently in wall time, not when
    the moment was emitted."""
    older_major = _candidate(
        moment_id="OLD",
        severity="major",
        session_started_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )
    newer_major = _candidate(
        moment_id="NEW",
        severity="major",
        session_started_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
    )
    moderate_more_recent = _candidate(
        moment_id="MODERATE_RECENT",
        severity="moderate",
        session_started_at=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
    )
    out = _fallback_selection([older_major, newer_major, moderate_more_recent])
    assert out is not None
    # Major precedence beats raw recency: the more-recent moderate
    # is still skipped because any major exists.
    assert out.headline_moment_id == "NEW"


def test_fallback_selection_falls_through_to_moderate_when_no_major():
    """When no major-severity candidate exists, the most-recent
    moderate becomes the headline."""
    older_moderate = _candidate(
        moment_id="OLD_MOD",
        severity="moderate",
        session_started_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )
    newer_moderate = _candidate(
        moment_id="NEW_MOD",
        severity="moderate",
        session_started_at=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
    )
    minor = _candidate(
        moment_id="MINOR",
        severity="minor",
        session_started_at=datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
    )
    out = _fallback_selection([older_moderate, newer_moderate, minor])
    assert out is not None
    assert out.headline_moment_id == "NEW_MOD"


def test_fallback_selection_returns_none_when_no_major_or_moderate():
    """If only minor candidates exist, the spec's fallback ordering
    has nothing to fall back to -- return None so the wrapper can
    re-raise the original protocol-failure error."""
    minor = _candidate(moment_id="MINOR", severity="minor")
    assert _fallback_selection([minor]) is None


def test_fallback_selection_has_empty_supporting_and_reason():
    """Acceptance: supporting_moment_ids is left empty in the
    fallback path. headline_reason is also empty -- the deterministic
    path has no LLM-generated coaching prose to attach."""
    major = _candidate(moment_id="M", severity="major")
    out = _fallback_selection([major])
    assert out is not None
    assert out.supporting_moment_ids == []
    assert out.headline_reason == ""


def test_select_moments_with_fallback_passes_through_on_success():
    """If the LLM succeeds (any path inside select_moments), the
    fallback wrapper just returns that selection unchanged."""
    def stub(system: str, user: str, model: str) -> str:
        return json.dumps(
            {
                "headline_moment_id": "m1",
                "headline_reason": "the LLM was happy.",
                "supporting_moment_ids": [],
            }
        )

    cands = [_candidate(moment_id="m1", severity="major")]
    out = select_moments_with_fallback(
        cands, primary_provider="anthropic", llm_caller=stub
    )
    assert out is not None
    assert out.headline_moment_id == "m1"
    assert out.headline_reason == "the LLM was happy."


def test_select_moments_with_fallback_recovers_when_llm_fails_twice():
    """When the LLM returns bad ids on both attempts, the wrapper
    catches InvalidMomentSelectionError and returns the most-recent
    major-severity candidate as the headline."""
    cands = [
        _candidate(
            moment_id="oldMajor",
            severity="major",
            session_started_at=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
        ),
        _candidate(
            moment_id="newMajor",
            severity="major",
            session_started_at=datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc),
        ),
        _candidate(
            moment_id="oldModerate",
            severity="moderate",
            session_started_at=datetime(2026, 5, 23, 9, 0, tzinfo=timezone.utc),
        ),
    ]
    out = select_moments_with_fallback(
        cands, primary_provider="anthropic", llm_caller=_bad_stub
    )
    assert out is not None
    assert out.headline_moment_id == "newMajor"
    assert out.supporting_moment_ids == []
    assert out.headline_reason == ""


def test_select_moments_with_fallback_uses_moderate_when_no_major():
    """If the LLM fails and no major-severity candidates exist, the
    most-recent moderate is the fallback headline."""
    cands = [
        _candidate(
            moment_id="modA",
            severity="moderate",
            session_started_at=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
        ),
        _candidate(
            moment_id="modB",
            severity="moderate",
            session_started_at=datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc),
        ),
    ]
    out = select_moments_with_fallback(
        cands, primary_provider="anthropic", llm_caller=_bad_stub
    )
    assert out is not None
    assert out.headline_moment_id == "modB"
    assert out.supporting_moment_ids == []


def test_select_moments_with_fallback_reraises_when_no_major_or_moderate():
    """If the LLM fails and only minor candidates exist, there is
    nothing the deterministic fallback can pick -- the original
    InvalidMomentSelectionError propagates so the digest pipeline
    knows the headline slot is empty."""
    cands = [_candidate(moment_id="minA", severity="minor")]
    with pytest.raises(InvalidMomentSelectionError):
        select_moments_with_fallback(
            cands, primary_provider="anthropic", llm_caller=_bad_stub
        )


def test_select_moments_with_fallback_makes_at_most_two_llm_calls():
    """The fallback wrapper does not retry beyond select_moments'
    one-retry budget; it must call the LLM exactly twice before
    giving up and using the deterministic path."""
    n = 0

    def counting_bad_stub(system: str, user: str, model: str) -> str:
        nonlocal n
        n += 1
        return _bad_stub(system, user, model)

    cands = [_candidate(moment_id="m1", severity="major")]
    out = select_moments_with_fallback(
        cands, primary_provider="anthropic", llm_caller=counting_bad_stub
    )
    assert out is not None
    assert out.headline_moment_id == "m1"
    assert n == 2


def test_select_moments_with_fallback_returns_none_for_empty_candidates():
    """Consistency with select_moments: empty input -> None, with no
    LLM call attempted and nothing to fall back to."""
    calls: list[tuple[str, str, str]] = []

    def tracking_stub(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        return "{}"

    out = select_moments_with_fallback(
        [], primary_provider="anthropic", llm_caller=tracking_stub
    )
    assert out is None
    assert calls == []


def test_select_moments_with_fallback_prefers_major_over_more_recent_moderate():
    """If a more-recent moderate sits next to an older major, the
    major still wins. Severity ordering is hard: the fallback is not
    'most recent moment' -- it's 'most recent of the highest severity
    that exists'."""
    older_major = _candidate(
        moment_id="OLDMAJ",
        severity="major",
        session_started_at=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
    )
    newer_moderate = _candidate(
        moment_id="NEWMOD",
        severity="moderate",
        session_started_at=datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
    )
    out = select_moments_with_fallback(
        [older_major, newer_moderate],
        primary_provider="anthropic",
        llm_caller=_bad_stub,
    )
    assert out is not None
    assert out.headline_moment_id == "OLDMAJ"
