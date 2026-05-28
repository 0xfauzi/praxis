"""Tests for the augmentation-vs-automation session classifier.

These tests cover praxis.behavior.aug_auto.classify_session - parser
robustness, the augmentation/automation/mixed literal contract, and the
call-time provider selection. They do NOT hit a live model; the
Anthropic/OpenAI client codepaths are stubbed via monkeypatch.
"""
from __future__ import annotations

import json

import pytest

from praxis.behavior.aug_auto import (
    CLAUDE_CLASSIFIER_MODEL,
    OPENAI_CLASSIFIER_MODEL,
    AugAutoParseError,
    AugAutoResult,
    AugAutoUnavailableError,
    _parse_response,
    classify_session,
)


def _good_payload(
    classification: str = "augmentation",
    confidence: float = 0.82,
    rationale: str = "User stated the goal and asked follow-up questions.",
) -> str:
    return json.dumps(
        {
            "classification": classification,
            "confidence": confidence,
            "rationale": rationale,
        }
    )


def test_parse_response_returns_dataclass() -> None:
    """Happy path: well-formed JSON parses into an AugAutoResult."""
    result = _parse_response(_good_payload())
    assert isinstance(result, AugAutoResult)
    assert result.classification == "augmentation"
    assert result.confidence == pytest.approx(0.82)
    assert "goal" in result.rationale


def test_parse_response_accepts_all_three_literals() -> None:
    """augmentation, automation, mixed must all be accepted."""
    for label in ("augmentation", "automation", "mixed"):
        result = _parse_response(_good_payload(classification=label))
        assert result.classification == label


def test_parse_response_strips_markdown_fences() -> None:
    """The LLM sometimes wraps JSON in ```json ... ``` fences."""
    fenced = "```json\n" + _good_payload() + "\n```"
    result = _parse_response(fenced)
    assert result.classification == "augmentation"


def test_parse_response_handles_preamble_postamble() -> None:
    """Extra prose around the JSON object should not break parsing."""
    text = "Sure! Here is the result:\n" + _good_payload() + "\nLet me know."
    result = _parse_response(text)
    assert result.classification == "augmentation"


def test_parse_response_raises_on_non_json_body() -> None:
    """A response with no JSON object raises AugAutoParseError."""
    with pytest.raises(AugAutoParseError):
        _parse_response("totally not json")


def test_parse_response_raises_on_malformed_json() -> None:
    """A truncated/corrupt JSON object raises AugAutoParseError."""
    with pytest.raises(AugAutoParseError):
        _parse_response('{"classification": "augmentation", "confidence":')


def test_parse_response_raises_on_invalid_classification_literal() -> None:
    """Any classification outside the three literals raises AugAutoParseError."""
    bad = _good_payload(classification="hybrid")
    with pytest.raises(AugAutoParseError) as exc_info:
        _parse_response(bad)
    assert "hybrid" in str(exc_info.value)


def test_parse_response_raises_on_missing_classification() -> None:
    bad = json.dumps({"confidence": 0.5, "rationale": "x"})
    with pytest.raises(AugAutoParseError):
        _parse_response(bad)


def test_parse_response_raises_on_non_numeric_confidence() -> None:
    bad = json.dumps(
        {"classification": "augmentation", "confidence": "high", "rationale": "x"}
    )
    with pytest.raises(AugAutoParseError):
        _parse_response(bad)


def test_parse_response_raises_on_out_of_range_confidence() -> None:
    """Confidence must be in [0.0, 1.0]; 1.5 must raise."""
    bad = _good_payload(confidence=1.5)
    with pytest.raises(AugAutoParseError):
        _parse_response(bad)


def test_parse_response_raises_on_negative_confidence() -> None:
    bad = _good_payload(confidence=-0.1)
    with pytest.raises(AugAutoParseError):
        _parse_response(bad)


def test_parse_response_raises_on_non_string_rationale() -> None:
    bad = json.dumps(
        {"classification": "automation", "confidence": 0.7, "rationale": 123}
    )
    with pytest.raises(AugAutoParseError):
        _parse_response(bad)


# ---------------------------------------------------------------------------
# Provider selection: Haiku 4.5 when ANTHROPIC_API_KEY is set, gpt-5-mini else.
# Chosen at call time, not import time, so tests flip the env per call.
# ---------------------------------------------------------------------------


def test_classify_uses_haiku_when_anthropic_key_present(monkeypatch) -> None:
    """ANTHROPIC_API_KEY wins; the call must hit the Claude classifier path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: dict[str, object] = {"called": False}

    def _fake_anthropic(transcript_text: str) -> str:
        seen["called"] = True
        seen["transcript"] = transcript_text
        return _good_payload(classification="augmentation")

    def _fake_openai(transcript_text: str) -> str:  # noqa: ARG001
        raise AssertionError("OpenAI path should not be called when ANTHROPIC_API_KEY is set")

    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_anthropic", _fake_anthropic
    )
    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_openai", _fake_openai
    )

    result = classify_session("user: do a thing\nassistant: ok\n")
    assert seen["called"] is True
    assert result.classification == "augmentation"


def test_classify_falls_back_to_openai_when_only_openai_key(monkeypatch) -> None:
    """No ANTHROPIC_API_KEY but OPENAI_API_KEY set -> OpenAI classifier path."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    seen: dict[str, object] = {"called": False}

    def _fake_anthropic(transcript_text: str) -> str:  # noqa: ARG001
        raise AssertionError("Anthropic path should not be called without ANTHROPIC_API_KEY")

    def _fake_openai(transcript_text: str) -> str:
        seen["called"] = True
        return _good_payload(classification="automation", confidence=0.4)

    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_anthropic", _fake_anthropic
    )
    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_openai", _fake_openai
    )

    result = classify_session("short session")
    assert seen["called"] is True
    assert result.classification == "automation"
    assert result.confidence == pytest.approx(0.4)


def test_classify_raises_when_no_api_key(monkeypatch) -> None:
    """Neither key set: classify_session raises AugAutoUnavailableError.

    Distinct from AugAutoParseError so the orchestrator can log "classifier
    unavailable" once per run instead of treating it as a parse failure.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AugAutoUnavailableError):
        classify_session("anything")


def test_classify_propagates_parse_error_from_anthropic(monkeypatch) -> None:
    """A malformed Anthropic body must surface as AugAutoParseError to the caller."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_anthropic",
        lambda _t: "not json at all",
    )
    with pytest.raises(AugAutoParseError):
        classify_session("session text")


def test_classify_propagates_parse_error_from_openai(monkeypatch) -> None:
    """A malformed OpenAI body must surface as AugAutoParseError to the caller."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_openai",
        lambda _t: json.dumps(
            {"classification": "garbage", "confidence": 0.5, "rationale": "x"}
        ),
    )
    with pytest.raises(AugAutoParseError):
        classify_session("session text")


def test_classify_provider_choice_is_recomputed_per_call(monkeypatch) -> None:
    """Provider is chosen at call time; flipping the env between calls flips paths.

    Guards the AC: "provider is chosen at call time, not at import time."
    A previous import-time capture (e.g. caching a client at module load)
    would make this test fail because the second call would still use the
    Anthropic path.
    """
    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_anthropic",
        lambda _t: _good_payload(rationale="from anthropic"),
    )
    monkeypatch.setattr(
        "praxis.behavior.aug_auto._classify_with_openai",
        lambda _t: _good_payload(rationale="from openai"),
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    first = classify_session("call one")
    assert first.rationale == "from anthropic"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    second = classify_session("call two")
    assert second.rationale == "from openai"


def test_model_constants_match_spec() -> None:
    """Pin the model strings the PRD calls out."""
    assert CLAUDE_CLASSIFIER_MODEL == "claude-haiku-4-5"
    assert OPENAI_CLASSIFIER_MODEL == "gpt-5-mini"
