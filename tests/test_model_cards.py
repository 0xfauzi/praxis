"""Tests for the model card system.

Spec 11.3:
  - find_card_for_model_hint('claude-opus-4-7') returns the Opus card
  - prefix-matched dated suffixes resolve to the same card
  - unknown model returns None
  - user cards override built-ins
  - build_profiles([]) returns []
"""
from __future__ import annotations

import json

from praxis.models_advisor.advisor import build_profiles
from praxis.models_advisor.cards import (
    BUILTIN_CARDS,
    _normalize_model_string,
    _user_cards_dir,
    export_card,
    find_card_for_model_hint,
    load_all_cards,
)


def test_normalize_handles_spaces_and_dots():
    assert _normalize_model_string("Claude Opus 4.7") == "claude-opus-4-7"
    assert _normalize_model_string("GPT-5") == "gpt-5"


def test_direct_id_match(tmp_home):
    card = find_card_for_model_hint("claude-opus-4-7")
    assert card is not None
    assert card.id == "claude-opus-4-7"


def test_alias_match(tmp_home):
    # 'opus-4-7' is an alias of the Opus card per the built-in list.
    card = find_card_for_model_hint("opus-4-7")
    assert card is not None
    assert card.id == "claude-opus-4-7"


def test_prefix_match_with_dated_suffix(tmp_home):
    # Vendors often suffix the canonical id with a date for snapshotting.
    card = find_card_for_model_hint("claude-opus-4-7-20260315")
    assert card is not None
    assert card.id == "claude-opus-4-7"


def test_unknown_returns_none(tmp_home):
    assert find_card_for_model_hint("totally-made-up-model-zzz") is None
    assert find_card_for_model_hint("") is None
    assert find_card_for_model_hint(None) is None


def test_user_card_overrides_builtin(tmp_home):
    card_dir = _user_cards_dir()
    overridden = {
        "id": "claude-opus-4-7",
        "family": "claude",
        "display_name": "User Override Opus",
        "vendor": "Anthropic",
        "tier": "frontier",
        "context_window_tokens": 999_999,
        "strengths": ["one"],
        "weaknesses": ["two"],
        "prompting_quirks": ["three"],
        "best_for": ["four"],
        "avoid_for": ["five"],
        "notes": "user-supplied",
        "sources": ["local"],
        "aliases": ["claude-opus-4-7"],
    }
    (card_dir / "claude-opus-4-7.json").write_text(json.dumps(overridden), encoding="utf-8")
    loaded = load_all_cards()
    assert loaded["claude-opus-4-7"].display_name == "User Override Opus"


def test_all_eight_builtin_cards_present():
    expected = {
        "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5",
        "gpt-5", "gpt-5-mini", "gpt-4o", "gemini-2-5-pro", "copilot-default",
    }
    assert expected == {c.id for c in BUILTIN_CARDS}


def test_export_card_roundtrips_to_dict():
    card = BUILTIN_CARDS[0]
    d = export_card(card)
    assert d["id"] == card.id
    assert d["display_name"] == card.display_name


def test_build_profiles_empty_input_returns_empty():
    assert build_profiles([]) == []


# =========================================================================
# US-042: deterministic counterfactual overspend rule (advisor.py)
# =========================================================================

from datetime import datetime, timezone  # noqa: E402

from praxis.models import Provider, Role, Session, Turn  # noqa: E402
from praxis.models_advisor.advisor import (  # noqa: E402
    COUNTERFACTUAL_MAX_AVG_PROMPT_CHARS,
    COUNTERFACTUAL_MAX_USER_TURNS,
    CounterfactualOverspend,
    compute_counterfactual_overspend,
)


def _make_cf_session(
    model_hint: str | None,
    turn_texts: list[str],
    *,
    session_id_suffix: str = "",
) -> Session:
    """Build a session with the given model_hint and user-turn content."""
    turns = [Turn(role=Role.USER, content=t) for t in turn_texts]
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"s-{model_hint or 'none'}-{session_id_suffix or len(turn_texts)}",
        started_at=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
        turns=turns,
        source_path="/tmp/test-cf",
        model_hint=model_hint,
    )


def test_counterfactual_empty_sessions_returns_default():
    """No sessions => empty result; no priced session observed."""
    result = compute_counterfactual_overspend([])
    assert isinstance(result, CounterfactualOverspend)
    assert result.had_any_priced_session is False
    assert result.qualifying_session_count == 0
    assert result.overspend_usd == 0.0
    assert result.higher_tier_display == ""


def test_counterfactual_unknown_model_does_not_count():
    """A session whose model_hint resolves to no card contributes
    nothing - no priced session is recognised, no overspend, no
    qualifying count."""
    sessions = [_make_cf_session("totally-made-up-model-zzz", ["hello"])]
    result = compute_counterfactual_overspend(sessions)
    assert result.had_any_priced_session is False
    assert result.qualifying_session_count == 0
    assert result.overspend_usd == 0.0


def test_counterfactual_priced_session_sets_had_any_priced(tmp_home):
    """Any priced session (resolves to a card with pricing AND
    non-trivial char volume) flips ``had_any_priced_session`` True so
    the panel knows cost data exists, even when no session qualifies
    for overspend attribution."""
    # A long-prompt Opus session (does NOT qualify for overspend
    # because avg_chars > threshold) still flips the priced flag.
    long_prompt = "x" * 5000
    sessions = [
        _make_cf_session("claude-opus-4-7", [long_prompt]),
    ]
    result = compute_counterfactual_overspend(sessions)
    assert result.had_any_priced_session is True
    assert result.qualifying_session_count == 0
    assert result.overspend_usd == 0.0


def test_counterfactual_short_opus_session_overspends(tmp_home):
    """A short-prompt Opus session qualifies and produces positive
    overspend against Haiku. Use a prompt at the upper end of the
    threshold so the 4-decimal rounding doesn't collapse the (spent,
    overspend) numbers into the same value."""
    sessions = [
        # 200 chars = threshold ceiling; produces distinguishable
        # frontier vs fast costs after the rule's 4-decimal rounding.
        _make_cf_session("claude-opus-4-7", ["x" * 200]),
    ]
    result = compute_counterfactual_overspend(sessions)
    assert result.had_any_priced_session is True
    assert result.qualifying_session_count == 1
    assert result.overspend_usd > 0.0
    assert result.higher_tier_display == "Claude Opus 4.7"
    assert result.lower_tier_display == "Claude Haiku 4.5"
    # The frontier cost is the full $X spent on Opus this session; the
    # overspend is (frontier_cost - haiku_cost), strictly less than
    # frontier_cost since both costs are positive.
    assert result.overspend_usd < result.spent_on_higher_tier_usd


def test_counterfactual_haiku_session_does_not_qualify(tmp_home):
    """A fast-tier session is never overspent: the qualifying rule is
    frontier-tier only."""
    sessions = [_make_cf_session("claude-haiku-4-5", ["quick lookup"])]
    result = compute_counterfactual_overspend(sessions)
    assert result.had_any_priced_session is True
    assert result.qualifying_session_count == 0
    assert result.overspend_usd == 0.0


def test_counterfactual_long_prompt_opus_session_does_not_qualify(tmp_home):
    """A session whose average prompt exceeds the threshold does NOT
    qualify; large prompts are plausibly the right use of a frontier
    model."""
    long = "x" * int(COUNTERFACTUAL_MAX_AVG_PROMPT_CHARS + 50)
    sessions = [_make_cf_session("claude-opus-4-7", [long])]
    result = compute_counterfactual_overspend(sessions)
    assert result.had_any_priced_session is True
    assert result.qualifying_session_count == 0
    assert result.overspend_usd == 0.0


def test_counterfactual_too_many_turns_does_not_qualify(tmp_home):
    """A session with more user turns than the threshold does NOT
    qualify, even when each prompt is short. Many short turns is a
    real chat workload that benefits from a frontier model's context."""
    short_turns = ["hi"] * (COUNTERFACTUAL_MAX_USER_TURNS + 1)
    sessions = [_make_cf_session("claude-opus-4-7", short_turns)]
    result = compute_counterfactual_overspend(sessions)
    assert result.had_any_priced_session is True
    assert result.qualifying_session_count == 0


def test_counterfactual_multiple_qualifying_sessions_sum(tmp_home):
    """Across several qualifying Opus sessions, the overspend and
    spend totals are the sums per (frontier, fast) pair."""
    sessions = [
        _make_cf_session(
            "claude-opus-4-7",
            ["lookup A"],
            session_id_suffix="a",
        ),
        _make_cf_session(
            "claude-opus-4-7",
            ["lookup B"],
            session_id_suffix="b",
        ),
        _make_cf_session(
            "claude-opus-4-7",
            ["lookup C"],
            session_id_suffix="c",
        ),
    ]
    result = compute_counterfactual_overspend(sessions)
    assert result.qualifying_session_count == 3
    assert result.overspend_usd > 0.0


def test_counterfactual_deterministic_under_reordering(tmp_home):
    """The result is identical regardless of input order: the rule's
    tiebreaks are deterministic by card id, not by input position."""
    a = _make_cf_session(
        "claude-opus-4-7",
        ["alpha"],
        session_id_suffix="aa",
    )
    b = _make_cf_session(
        "claude-opus-4-7",
        ["beta"],
        session_id_suffix="bb",
    )
    forward = compute_counterfactual_overspend([a, b])
    reverse = compute_counterfactual_overspend([b, a])
    assert forward == reverse


def test_counterfactual_overspend_dataclass_is_frozen():
    """CounterfactualOverspend is immutable so callers cannot mutate
    the result."""
    import dataclasses

    result = CounterfactualOverspend()
    try:
        result.overspend_usd = 99.0  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("CounterfactualOverspend must be frozen")
