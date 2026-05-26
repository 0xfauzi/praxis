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
