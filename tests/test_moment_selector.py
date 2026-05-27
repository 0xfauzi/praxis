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
    _invalid_ids,
    _parse_selection,
    _retry_user_prompt,
    _SYSTEM_PROMPT,
    cheap_model_for,
    select_moments,
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
