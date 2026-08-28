"""Tests for the augmentation-vs-automation session classifier.

These tests cover praxis.behavior.aug_auto.classify_session - parser
robustness, the augmentation/automation/mixed literal contract, and the
call-time provider selection. They do NOT hit a live model; the
Anthropic/OpenAI client codepaths are stubbed via monkeypatch.

The aggregate_for_week tests (US-011) seed real session_scores rows via
ProfileStore (no LLM call) and exercise the per-week roll-up directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from praxis.behavior.aug_auto import (
    AUG_AUTO_BUCKETS,
    CLAUDE_CLASSIFIER_MODEL,
    OPENAI_CLASSIFIER_MODEL,
    AugAutoParseError,
    AugAutoResult,
    AugAutoUnavailableError,
    AugAutoWeekSummary,
    _parse_response,
    aggregate_for_week,
    classify_session,
)
from praxis.scoring.aggregate import SessionScore
from praxis.scoring.features import SessionFeatures
from praxis.scoring.judge import JudgeResult
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


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
    bad = json.dumps({"classification": "augmentation", "confidence": "high", "rationale": "x"})
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
    bad = json.dumps({"classification": "automation", "confidence": 0.7, "rationale": 123})
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

    def _fake_openai(transcript_text: str) -> str:
        raise AssertionError("OpenAI path should not be called when ANTHROPIC_API_KEY is set")

    monkeypatch.setattr("praxis.behavior.aug_auto._classify_with_anthropic", _fake_anthropic)
    monkeypatch.setattr("praxis.behavior.aug_auto._classify_with_openai", _fake_openai)

    result = classify_session("user: do a thing\nassistant: ok\n")
    assert seen["called"] is True
    assert result.classification == "augmentation"


def test_classify_falls_back_to_openai_when_only_openai_key(monkeypatch) -> None:
    """No ANTHROPIC_API_KEY but OPENAI_API_KEY set -> OpenAI classifier path."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    seen: dict[str, object] = {"called": False}

    def _fake_anthropic(transcript_text: str) -> str:
        raise AssertionError("Anthropic path should not be called without ANTHROPIC_API_KEY")

    def _fake_openai(transcript_text: str) -> str:
        seen["called"] = True
        return _good_payload(classification="automation", confidence=0.4)

    monkeypatch.setattr("praxis.behavior.aug_auto._classify_with_anthropic", _fake_anthropic)
    monkeypatch.setattr("praxis.behavior.aug_auto._classify_with_openai", _fake_openai)

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
        lambda _t: json.dumps({"classification": "garbage", "confidence": 0.5, "rationale": "x"}),
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


# ---------------------------------------------------------------------------
# aggregate_for_week (US-011): per-week roll-up of classifier output.
# ---------------------------------------------------------------------------


def _seed_session_score(
    store: ProfileStore,
    *,
    stable_id: str,
    started_at: datetime,
    classification: str | None = None,
    confidence: float | None = None,
) -> None:
    """Seed one session_scores row (and optional aug_auto columns) for tests."""
    judge = JudgeResult(
        dimension_scores={d.key: 5.0 for d in RUBRIC},
        rationale={d.key: "x" for d in RUBRIC},
        standout_moments=[],
        failure_modes=[],
        overall_note="x",
        judge_model="x",
    )
    score = SessionScore(
        session_stable_id=stable_id,
        provider="claude",
        started_at=started_at,
        dimension_scores={d.key: 5.0 for d in RUBRIC},
        overall=5.0,
        judge_result=judge,
        features=SessionFeatures(turn_count=2, avg_prompt_chars=40.0),
        source_path=f"/tmp/{stable_id}.jsonl",
        judge_pass=1,
    )
    store.save_session_score(score)
    if classification is not None and confidence is not None:
        store.save_session_aug_auto(stable_id, classification, confidence)


def _wed_in_week(week_iso: str) -> datetime:
    """Return a UTC datetime at noon Wednesday of the given ISO week tag."""
    year_str, week_str = week_iso.split("-W")
    monday = datetime.fromisocalendar(int(year_str), int(week_str), 1)
    return datetime(
        monday.year,
        monday.month,
        monday.day,
        12,
        0,
        0,
        tzinfo=UTC,
    ) + timedelta(days=2)


def test_aggregate_for_week_empty_store_returns_zero_summary(tmp_home) -> None:
    """No session_scores in the store -> all-zero counts, classifier_unavailable=False.

    AC: 'Calling aggregate_for_week with a week_iso that has zero
    session_scores returns a summary with all zeros and
    classifier_unavailable=False (not True).'
    """
    store = ProfileStore(home=resolve_home())
    summary = aggregate_for_week(store, "2026-W22")
    assert isinstance(summary, AugAutoWeekSummary)
    assert summary.week_iso == "2026-W22"
    assert summary.total == 0
    assert summary.counts == {b: 0 for b in AUG_AUTO_BUCKETS}
    assert summary.shares == {b: 0.0 for b in AUG_AUTO_BUCKETS}
    assert summary.classifier_unavailable is False


def test_aggregate_for_week_target_week_has_no_rows(tmp_home) -> None:
    """Store has rows in other weeks but none in the target week.

    Same expectation as the truly-empty store: classifier_unavailable=False
    because there is no classifier work to do.
    """
    store = ProfileStore(home=resolve_home())
    _seed_session_score(
        store,
        stable_id="other-week",
        started_at=_wed_in_week("2026-W20"),
        classification="augmentation",
        confidence=0.7,
    )
    summary = aggregate_for_week(store, "2026-W22")
    assert summary.total == 0
    assert summary.classifier_unavailable is False


def test_aggregate_for_week_all_null_sets_classifier_unavailable(tmp_home) -> None:
    """Every row in the week is NULL -> classifier_unavailable=True.

    AC: '"classifier_unavailable" flag when every row in the week is NULL.'
    """
    store = ProfileStore(home=resolve_home())
    for i in range(3):
        _seed_session_score(
            store,
            stable_id=f"null-{i}",
            started_at=_wed_in_week("2026-W22"),
        )
    summary = aggregate_for_week(store, "2026-W22")
    assert summary.total == 3
    assert summary.counts["unclassified"] == 3
    assert summary.counts["augmentation"] == 0
    assert summary.counts["automation"] == 0
    assert summary.counts["mixed"] == 0
    assert summary.classifier_unavailable is True
    assert summary.shares["unclassified"] == pytest.approx(1.0)


def test_aggregate_for_week_null_rows_go_to_unclassified_bucket(tmp_home) -> None:
    """NULL rows are counted under 'unclassified', not dropped.

    AC: 'aggregate_for_week treats NULL rows as unclassified (separate
    bucket) rather than dropping them, so totals always equal the count
    of session_scores in the week.'
    """
    store = ProfileStore(home=resolve_home())
    _seed_session_score(
        store,
        stable_id="aug-1",
        started_at=_wed_in_week("2026-W22"),
        classification="augmentation",
        confidence=0.9,
    )
    _seed_session_score(
        store,
        stable_id="null-1",
        started_at=_wed_in_week("2026-W22"),
    )
    _seed_session_score(
        store,
        stable_id="null-2",
        started_at=_wed_in_week("2026-W22"),
    )
    summary = aggregate_for_week(store, "2026-W22")
    assert summary.total == 3
    assert summary.counts["augmentation"] == 1
    assert summary.counts["unclassified"] == 2
    assert summary.classifier_unavailable is False


def test_aggregate_for_week_mixed_classifications_count_and_share(tmp_home) -> None:
    """Counts and shares are computed per bucket; shares sum to 1.0."""
    store = ProfileStore(home=resolve_home())
    seeds = [
        ("a1", "augmentation"),
        ("a2", "augmentation"),
        ("b1", "automation"),
        ("m1", "mixed"),
    ]
    for stable_id, classification in seeds:
        _seed_session_score(
            store,
            stable_id=stable_id,
            started_at=_wed_in_week("2026-W22"),
            classification=classification,
            confidence=0.6,
        )
    summary = aggregate_for_week(store, "2026-W22")
    assert summary.total == 4
    assert summary.counts == {
        "augmentation": 2,
        "automation": 1,
        "mixed": 1,
        "unclassified": 0,
    }
    assert summary.shares["augmentation"] == pytest.approx(0.5)
    assert summary.shares["automation"] == pytest.approx(0.25)
    assert summary.shares["mixed"] == pytest.approx(0.25)
    assert summary.shares["unclassified"] == pytest.approx(0.0)
    assert sum(summary.shares.values()) == pytest.approx(1.0)
    assert summary.classifier_unavailable is False


def test_aggregate_for_week_excludes_rows_from_other_weeks(tmp_home) -> None:
    """Rows from weeks other than the target must not be counted."""
    store = ProfileStore(home=resolve_home())
    _seed_session_score(
        store,
        stable_id="prev-week",
        started_at=_wed_in_week("2026-W21"),
        classification="augmentation",
        confidence=0.9,
    )
    _seed_session_score(
        store,
        stable_id="next-week",
        started_at=_wed_in_week("2026-W23"),
        classification="automation",
        confidence=0.8,
    )
    _seed_session_score(
        store,
        stable_id="this-week",
        started_at=_wed_in_week("2026-W22"),
        classification="mixed",
        confidence=0.7,
    )
    summary = aggregate_for_week(store, "2026-W22")
    assert summary.total == 1
    assert summary.counts["mixed"] == 1
    assert summary.counts["augmentation"] == 0
    assert summary.counts["automation"] == 0


def test_aggregate_for_week_total_equals_row_count_for_week(tmp_home) -> None:
    """Sanity check the invariant: total == sum of session_scores rows for the week.

    This is the testable form of the AC 'totals always equal the count of
    session_scores in the week.'
    """
    store = ProfileStore(home=resolve_home())
    classifications: list[str | None] = [
        "augmentation",
        "augmentation",
        "automation",
        "mixed",
        None,
        None,
    ]
    for i, classification in enumerate(classifications):
        _seed_session_score(
            store,
            stable_id=f"row-{i}",
            started_at=_wed_in_week("2026-W22"),
            classification=classification,
            confidence=0.55 if classification else None,
        )
    summary = aggregate_for_week(store, "2026-W22")
    assert summary.total == len(classifications)
    assert sum(summary.counts.values()) == len(classifications)


def test_aggregate_for_week_classifier_unavailable_false_when_any_row_classified(
    tmp_home,
) -> None:
    """classifier_unavailable is False as soon as ONE row in the week is classified."""
    store = ProfileStore(home=resolve_home())
    _seed_session_score(
        store,
        stable_id="classified",
        started_at=_wed_in_week("2026-W22"),
        classification="augmentation",
        confidence=0.9,
    )
    for i in range(4):
        _seed_session_score(
            store,
            stable_id=f"unclassified-{i}",
            started_at=_wed_in_week("2026-W22"),
        )
    summary = aggregate_for_week(store, "2026-W22")
    assert summary.total == 5
    assert summary.counts["unclassified"] == 4
    assert summary.counts["augmentation"] == 1
    assert summary.classifier_unavailable is False


def test_aggregate_for_week_buckets_are_exactly_four(tmp_home) -> None:
    """The four-bucket shape (aug, auto, mixed, unclassified) is contract.

    Pinned to guard against silent expansion of AUG_AUTO_BUCKETS, which
    would also require widening callers / display code.
    """
    assert AUG_AUTO_BUCKETS == ("augmentation", "automation", "mixed", "unclassified")
    store = ProfileStore(home=resolve_home())
    summary = aggregate_for_week(store, "2026-W22")
    assert set(summary.counts.keys()) == set(AUG_AUTO_BUCKETS)
    assert set(summary.shares.keys()) == set(AUG_AUTO_BUCKETS)
