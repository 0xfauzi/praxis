"""Tests for praxis.behavior.headline (US-046).

Acceptance criteria (PRD US-046, spec section 7.3):
  - One short LLM call takes label, slope numbers, and bucket count
    as inputs and returns a single sentence.
  - Output is truncated to 180 chars.
  - The label is not invented by the LLM: the prose is constrained
    to paraphrase the supplied label.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError

import pytest  # type: ignore[import-not-found]

from praxis.behavior import (
    HEADLINE_MAX_CHARS,
    WeeklyTrajectoryLabel,
    fallback_headline,
    generate_headline,
)
from praxis.behavior import headline as headline_mod
from praxis.behavior.headline import (
    _build_prompt,
    _HeadlineContext,
    _other_labels,
    _truncate,
    _violates_label_constraint,
)

# ---- constants -------------------------------------------------------------


def test_headline_max_chars_matches_spec():
    """Spec 7.3: 'Truncate to 180 chars.'"""
    assert HEADLINE_MAX_CHARS == 180


# ---- signature contract (AC #1) -------------------------------------------


def test_generate_headline_signature_takes_label_slopes_and_bucket_count():
    """AC #1: inputs are label, slope numbers, and bucket count."""
    sig = inspect.signature(generate_headline)
    param_names = list(sig.parameters.keys())
    assert param_names == [
        "label",
        "engagement_slope",
        "delegation_slope",
        "bucket_count",
    ]
    # `from __future__ import annotations` keeps annotations as strings.
    # Return type is a string sentence, not a label - the LLM does not
    # invent the label.
    assert sig.return_annotation == "str"


def test_generate_headline_does_not_return_label_type():
    """Sanity check: the function returns prose, never a label.

    The label decision lives in praxis.behavior.labels and is supplied
    as an input - it is never something this function can override.
    """
    out = generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.1, -0.1, 5)
    assert isinstance(out, str)
    assert not isinstance(out, WeeklyTrajectoryLabel)


# ---- truncation (AC #2) ----------------------------------------------------


def test_truncate_helper_keeps_short_strings_unchanged():
    assert _truncate("hello") == "hello"


def test_truncate_helper_strips_whitespace():
    assert _truncate("  hi  ") == "hi"


def test_truncate_helper_caps_at_limit():
    s = "x" * (HEADLINE_MAX_CHARS + 50)
    assert len(_truncate(s)) == HEADLINE_MAX_CHARS


def test_truncate_helper_caps_at_custom_limit():
    assert _truncate("abcdef", limit=3) == "abc"


def test_generate_headline_truncates_long_llm_output(monkeypatch, tmp_home):
    """If the LLM rambles, the output is clipped to 180 chars."""
    long_text = "Engagement is rising and delegation is falling. " * 20
    assert len(long_text) > HEADLINE_MAX_CHARS
    monkeypatch.setattr(headline_mod, "_call_llm", lambda system, user: long_text)
    out = generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert len(out) == HEADLINE_MAX_CHARS


@pytest.mark.parametrize("label", list(WeeklyTrajectoryLabel))
def test_fallback_headline_within_limit(label: WeeklyTrajectoryLabel):
    """Every per-label fallback stays at or under HEADLINE_MAX_CHARS."""
    out = fallback_headline(label, 0.12, -0.18, 8)
    assert len(out) <= HEADLINE_MAX_CHARS, (label, len(out), out)


@pytest.mark.parametrize("label", list(WeeklyTrajectoryLabel))
def test_fallback_headline_within_limit_at_large_bucket_count(
    label: WeeklyTrajectoryLabel,
):
    """Headroom check: even at the largest plausible window (~13 weeks) the fallback fits."""
    out = fallback_headline(label, 0.99, -0.99, 13)
    assert len(out) <= HEADLINE_MAX_CHARS, (label, len(out), out)


# ---- label-not-invented constraint (AC #3) --------------------------------


def test_other_labels_excludes_supplied_label():
    others = _other_labels(WeeklyTrajectoryLabel.LEARNING)
    assert WeeklyTrajectoryLabel.LEARNING.value not in others
    # Every other label is present.
    for l in WeeklyTrajectoryLabel:
        if l is WeeklyTrajectoryLabel.LEARNING:
            continue
        assert l.value in others


@pytest.mark.parametrize(
    "supplied,offender",
    [
        (WeeklyTrajectoryLabel.STEADY, WeeklyTrajectoryLabel.LEARNING),
        (WeeklyTrajectoryLabel.STEADY, WeeklyTrajectoryLabel.DRIFTING),
        (WeeklyTrajectoryLabel.STEADY, WeeklyTrajectoryLabel.ATROPHYING),
        (WeeklyTrajectoryLabel.LEARNING, WeeklyTrajectoryLabel.STEADY),
        (WeeklyTrajectoryLabel.LEARNING, WeeklyTrajectoryLabel.DRIFTING),
        (WeeklyTrajectoryLabel.LEARNING, WeeklyTrajectoryLabel.GROWING_AUTONOMY),
        (WeeklyTrajectoryLabel.DRIFTING, WeeklyTrajectoryLabel.LEARNING),
        (WeeklyTrajectoryLabel.GROWING_AUTONOMY, WeeklyTrajectoryLabel.STEADY),
        (WeeklyTrajectoryLabel.ATROPHYING, WeeklyTrajectoryLabel.READING),
        (WeeklyTrajectoryLabel.READING, WeeklyTrajectoryLabel.ATROPHYING),
    ],
)
def test_violates_label_constraint_catches_other_label_mentions(
    supplied: WeeklyTrajectoryLabel, offender: WeeklyTrajectoryLabel
):
    sentence = f"This week looks more like {offender.value} to me."
    assert _violates_label_constraint(sentence, supplied) is True


@pytest.mark.parametrize("label", list(WeeklyTrajectoryLabel))
def test_violates_label_constraint_allows_supplied_label(
    label: WeeklyTrajectoryLabel,
):
    """The supplied label is allowed to appear in the prose."""
    sentence = f"This week is clearly {label.value}."
    assert _violates_label_constraint(sentence, label) is False


def test_violates_label_constraint_is_case_insensitive():
    # Even with weird casing, an OTHER label is rejected.
    sentence = "your behaviour is LEARNING fast"
    assert _violates_label_constraint(sentence, WeeklyTrajectoryLabel.STEADY) is True


def test_violates_label_constraint_uses_word_boundaries():
    """'steadily' or 'unsteady' should not trigger STEADY rejection.

    Word boundary regex means only whole-word matches count - otherwise
    natural English prose with embedded label substrings would all be
    rejected.
    """
    sentence = "You are improving steadily and unsteady habits are gone."
    # Supplied label != STEADY: substring 'steady' appears inside
    # 'steadily' / 'unsteady', but the whole word is not present.
    assert _violates_label_constraint(sentence, WeeklyTrajectoryLabel.LEARNING) is False


def test_violates_label_constraint_catches_multiword_label():
    # 'Growing autonomy' is two words and is matched as a phrase.
    sentence = "Your trajectory shows growing autonomy this week."
    assert _violates_label_constraint(sentence, WeeklyTrajectoryLabel.STEADY) is True


def test_violates_label_constraint_clean_sentence_passes():
    sentence = "Engagement up, delegation down. You're learning the hard parts."
    # Supplied label is LEARNING - 'learning' is the supplied label
    # word, not a different label. Other label words absent.
    assert _violates_label_constraint(sentence, WeeklyTrajectoryLabel.LEARNING) is False


@pytest.mark.parametrize("label", list(WeeklyTrajectoryLabel))
def test_fallback_does_not_invent_other_labels(label: WeeklyTrajectoryLabel):
    """Each per-label fallback template itself passes the constraint check.

    If a fallback accidentally name-dropped another label, every reject
    path would loop forever (LLM rejected -> fallback also rejected).
    Lock the property in.
    """
    out = fallback_headline(label, 0.12, -0.18, 8)
    assert _violates_label_constraint(out, label) is False, (label, out)


# ---- LLM-vs-fallback dispatch ---------------------------------------------


def test_generate_headline_no_api_uses_fallback(tmp_home):
    """With no API keys (tmp_home clears them), fallback is used."""
    out = generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    expected = fallback_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert out == expected


def test_generate_headline_uses_llm_response_when_available(monkeypatch, tmp_home):
    """When _call_llm returns valid prose, generate_headline uses it as-is (after truncate)."""
    response = "Engagement up sharply; delegation down. Hands on the keyboard."
    monkeypatch.setattr(headline_mod, "_call_llm", lambda system, user: response)
    out = generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert out == response


def test_generate_headline_strips_whitespace_from_llm_output(monkeypatch, tmp_home):
    monkeypatch.setattr(
        headline_mod,
        "_call_llm",
        lambda system, user: "   Hands on the keyboard, eyes on the why.   ",
    )
    out = generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert out == "Hands on the keyboard, eyes on the why."


def test_generate_headline_empty_llm_falls_back(monkeypatch, tmp_home):
    monkeypatch.setattr(headline_mod, "_call_llm", lambda system, user: "")
    out = generate_headline(WeeklyTrajectoryLabel.STEADY, 0.0, 0.0, 5)
    expected = fallback_headline(WeeklyTrajectoryLabel.STEADY, 0.0, 0.0, 5)
    assert out == expected


def test_generate_headline_whitespace_only_llm_falls_back(monkeypatch, tmp_home):
    monkeypatch.setattr(headline_mod, "_call_llm", lambda system, user: "   \n\t  ")
    out = generate_headline(WeeklyTrajectoryLabel.STEADY, 0.0, 0.0, 5)
    expected = fallback_headline(WeeklyTrajectoryLabel.STEADY, 0.0, 0.0, 5)
    assert out == expected


def test_generate_headline_rejects_other_label_mention(monkeypatch, tmp_home):
    """If the LLM names a different label, fall back to the deterministic template."""
    monkeypatch.setattr(
        headline_mod,
        "_call_llm",
        lambda system, user: "Looks like Learning to me, frankly.",
    )
    out = generate_headline(WeeklyTrajectoryLabel.DRIFTING, 0.0, 0.14, 6)
    expected = fallback_headline(WeeklyTrajectoryLabel.DRIFTING, 0.0, 0.14, 6)
    assert out == expected


def test_generate_headline_accepts_supplied_label_mention(monkeypatch, tmp_home):
    """If the LLM uses the supplied label word, that's allowed."""
    response = "Steady week: habits are locked in, for better or worse."
    monkeypatch.setattr(headline_mod, "_call_llm", lambda system, user: response)
    out = generate_headline(WeeklyTrajectoryLabel.STEADY, 0.0, 0.0, 5)
    assert out == response


def test_generate_headline_llm_call_receives_label_in_prompt(monkeypatch, tmp_home):
    """The LLM call must receive the supplied label so it can paraphrase it."""
    seen: dict[str, str] = {}

    def fake(system: str, user: str) -> str:
        seen["system"] = system
        seen["user"] = user
        return "Engagement up, delegation down - more thought per session."

    monkeypatch.setattr(headline_mod, "_call_llm", fake)
    generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert WeeklyTrajectoryLabel.LEARNING.value in seen["user"]
    assert WeeklyTrajectoryLabel.LEARNING.value in seen["system"]


def test_generate_headline_llm_call_receives_slope_numbers_in_prompt(monkeypatch, tmp_home):
    """Slopes are passed to the LLM so the sentence can name the magnitudes."""
    seen: dict[str, str] = {}

    def fake(system: str, user: str) -> str:
        seen["user"] = user
        return "ok"

    monkeypatch.setattr(headline_mod, "_call_llm", fake)
    generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.1800, -0.1100, 8)
    assert "0.1800" in seen["user"]
    assert "-0.1100" in seen["user"]
    assert "8" in seen["user"]


def test_generate_headline_llm_call_forbids_other_labels_in_prompt(monkeypatch, tmp_home):
    """The system prompt must explicitly list the other labels as forbidden."""
    seen: dict[str, str] = {}

    def fake(system: str, user: str) -> str:
        seen["system"] = system
        return "ok"

    monkeypatch.setattr(headline_mod, "_call_llm", fake)
    generate_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    for other in _other_labels(WeeklyTrajectoryLabel.LEARNING):
        assert other in seen["system"], other


# ---- _call_llm provider selection (no real network) -----------------------


def test_call_llm_no_keys_returns_none(tmp_home):
    """tmp_home clears both API keys -> _call_llm returns None."""
    assert headline_mod._call_llm("sys", "user") is None


def test_call_llm_prefers_anthropic_when_both_present(monkeypatch, tmp_home):
    """When both keys are set, Anthropic wins (matches the rest of v0.2)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    calls: list[str] = []
    monkeypatch.setattr(
        headline_mod,
        "_call_anthropic",
        lambda s, u: calls.append("anthropic") or "ok",
    )
    monkeypatch.setattr(
        headline_mod,
        "_call_openai",
        lambda s, u: calls.append("openai") or "ok",
    )
    headline_mod._call_llm("sys", "user")
    assert calls == ["anthropic"]


def test_call_llm_uses_openai_when_only_openai_set(monkeypatch, tmp_home):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    calls: list[str] = []
    monkeypatch.setattr(
        headline_mod,
        "_call_anthropic",
        lambda s, u: calls.append("anthropic") or "ok",
    )
    monkeypatch.setattr(
        headline_mod,
        "_call_openai",
        lambda s, u: calls.append("openai") or "ok",
    )
    headline_mod._call_llm("sys", "user")
    assert calls == ["openai"]


# ---- per-label fallback shape ---------------------------------------------


def test_fallback_learning_mentions_both_slopes_and_bucket_count():
    out = fallback_headline(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert "0.18" in out
    assert "0.11" in out
    assert "8" in out


def test_fallback_drifting_mentions_delegation_slope_and_bucket_count():
    out = fallback_headline(WeeklyTrajectoryLabel.DRIFTING, 0.0, 0.14, 6)
    assert "0.14" in out
    assert "6" in out


def test_fallback_steady_mentions_bucket_count_only():
    out = fallback_headline(WeeklyTrajectoryLabel.STEADY, 0.0, 0.0, 5)
    assert "5" in out
    assert "No significant movement" in out


def test_fallback_growing_autonomy_mentions_delegation_slope():
    out = fallback_headline(WeeklyTrajectoryLabel.GROWING_AUTONOMY, 0.0, -0.11, 7)
    assert "0.11" in out
    assert "7" in out


def test_fallback_atrophying_mentions_both_slopes():
    out = fallback_headline(WeeklyTrajectoryLabel.ATROPHYING, -0.18, 0.14, 6)
    assert "0.18" in out
    assert "0.14" in out
    assert "6" in out


def test_fallback_reading_mentions_bucket_count():
    out = fallback_headline(WeeklyTrajectoryLabel.READING, 0.0, 0.0, 2)
    assert "2" in out


# ---- _HeadlineContext dataclass -------------------------------------------


def test_headline_context_is_frozen():
    ctx = _HeadlineContext(engagement_slope=0.1, delegation_slope=-0.1, bucket_count=8)
    with pytest.raises(FrozenInstanceError):
        ctx.engagement_slope = 0.5  # type: ignore[misc]


# ---- prompt-building shape ------------------------------------------------


def test_build_prompt_returns_system_and_user():
    system, user = _build_prompt(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert isinstance(system, str) and system
    assert isinstance(user, str) and user


def test_build_prompt_user_contains_all_inputs():
    _system, user = _build_prompt(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert WeeklyTrajectoryLabel.LEARNING.value in user
    assert "0.1800" in user
    assert "-0.1100" in user
    assert "8" in user


def test_build_prompt_system_states_180_char_limit():
    system, _user = _build_prompt(WeeklyTrajectoryLabel.LEARNING, 0.18, -0.11, 8)
    assert str(HEADLINE_MAX_CHARS) in system
