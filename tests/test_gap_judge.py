"""Tests for the constrained cheap-tier gap-summary judge (US-037).

The renderer side (digest_terminal, digest_html) is exercised in
``test_digest_terminal.py`` and ``test_digest_html.py``; these tests
cover the gap-judge module itself: the truncation helper, the prompt
shape, the API call wrappers (under monkeypatched SDKs), the
disagreement detector, and the ``apply_gap_prose`` orchestrator hook.

No real LLM calls are made. Each provider's SDK is monkeypatched to
either return a canned response, raise a specific exception, or be
absent (ImportError), so the offline fallbacks are tested
deterministically. The ``tmp_home`` conftest fixture already deletes
``ANTHROPIC_API_KEY`` and ``OPENAI_API_KEY`` from the env so tests
that opt into a provider must re-set the matching key.
"""
from __future__ import annotations

import sys

from praxis.reports.commitment_rollup import CommitmentRollup
from praxis.reports.gap_judge import (
    CLAUDE_CHEAP_MODEL,
    GAP_FALLBACK_LINE,
    GAP_SYSTEM_PROMPT,
    OPENAI_CHEAP_MODEL,
    _build_user_prompt,
    _is_disagreement,
    apply_gap_prose,
    generate_gap_prose,
    truncate_to_two_sentences,
)


def _rollup(
    *,
    display_text: str = "Verify before destructive ops.",
    target_dim_key: str = "verification",
    sessions_this_week: int = 6,
    sessions_prior_week: int = 4,
    self_report_tally: dict[str, int] | None = None,
    dim_before: dict[str, float] | None = None,
    dim_after: dict[str, float] | None = None,
    gap_prose: str | None = None,
) -> CommitmentRollup:
    """Fixture: a disagree-shaped rollup (yes-heavy self-report, dim down).

    Defaults match the canonical disagreement scenario from spec §2 so
    each test only overrides the specific field it cares about. Override
    self_report_tally / dim_before / dim_after to flip the scenario.
    """
    return CommitmentRollup(
        display_text=display_text,
        target_dim_key=target_dim_key,
        sessions_this_week=sessions_this_week,
        sessions_prior_week=sessions_prior_week,
        self_report_tally=(
            self_report_tally
            if self_report_tally is not None
            else {"yes": 4, "no": 0, "partial": 0, "skip": 1}
        ),
        dim_before=dim_before if dim_before is not None else {"verification": 6.5},
        dim_after=dim_after if dim_after is not None else {"verification": 4.8},
        gap_prose=gap_prose,
    )


# --------------------------------------------------------- truncation


def test_truncate_passthrough_for_one_sentence():
    """A single-sentence input is returned unchanged.

    The two-sentence cap is the ceiling, not a floor; under-budget
    input must not be padded or truncated. This guards against an
    overzealous regex that would emit "..." even on compliant input.
    """
    assert (
        truncate_to_two_sentences("Numbers point one way, words another.")
        == "Numbers point one way, words another."
    )


def test_truncate_passthrough_for_two_sentences():
    """Exactly two sentences sit at the cap and render verbatim."""
    text = "Numbers point one way. Words point another."
    assert truncate_to_two_sentences(text) == text


def test_truncate_caps_three_sentences_with_ellipsis():
    """Three sentences keep the first two and append "..." for truncation.

    The terminal punctuation of the second sentence is replaced by
    the ellipsis marker so the reader sees "...,. Two..." rather than
    "...,. Two...." (four-dot tail).
    """
    out = truncate_to_two_sentences(
        "One sentence. Two sentence. Three sentence."
    )
    assert out == "One sentence. Two sentence..."


def test_truncate_caps_many_sentences():
    """A long blob is cut at sentence 2 regardless of how many follow.

    Defense in depth against a model that ignores the <= 2-sentence
    prompt rule.
    """
    out = truncate_to_two_sentences(
        "A. B. C. D. E. F. G. H."
    )
    assert out == "A. B..."


def test_truncate_handles_mixed_terminal_punctuation():
    """!, ?, . all count as sentence boundaries.

    A question or exclamation closes a sentence the same way a
    period does.
    """
    out = truncate_to_two_sentences(
        "Did you notice the gap? It is worth a moment of curiosity. Try this next."
    )
    assert out == "Did you notice the gap? It is worth a moment of curiosity..."


def test_truncate_strips_surrounding_whitespace():
    """Leading/trailing whitespace is stripped before counting sentences."""
    out = truncate_to_two_sentences("   First. Second. Third.   ")
    assert out == "First. Second..."


def test_truncate_returns_empty_on_empty_input():
    """Empty input returns empty so the renderer's fallback path can fire."""
    assert truncate_to_two_sentences("") == ""
    assert truncate_to_two_sentences("   ") == ""


def test_truncate_returns_input_when_no_terminal_punctuation():
    """Single fragment without a period is one sentence -> no truncation.

    A model that violates the prompt by emitting an unterminated
    sentence still gets surfaced rather than dropped, since the bound
    is on sentence COUNT, not character count.
    """
    out = truncate_to_two_sentences("an unterminated fragment with no period")
    assert out == "an unterminated fragment with no period"


def test_truncate_does_not_split_on_abbreviation_followed_by_no_space():
    """Punctuation NOT followed by whitespace is treated as inline.

    Decimals (e.g. "4.8/10") and run-on punctuation should not count
    as sentence boundaries; this protects against a false split that
    would chop a single-sentence reply in half.
    """
    out = truncate_to_two_sentences("Verification dropped to 4.8/10 this week.")
    assert out == "Verification dropped to 4.8/10 this week."


# --------------------------------------------------------- system prompt


def test_system_prompt_caps_at_two_sentences():
    """The constrained-judge prompt names the <= 2-sentence cap.

    The cap is the renderer's contract; if a future copy edit drops
    the explicit budget the model would start emitting paragraphs.
    """
    assert "2 sentences" in GAP_SYSTEM_PROMPT or "two sentences" in GAP_SYSTEM_PROMPT.lower()


def test_system_prompt_names_curiosity_voice():
    """Curiosity-only framing is the load-bearing voice instruction."""
    assert "curiosity" in GAP_SYSTEM_PROMPT.lower()


def test_system_prompt_forbids_blame_and_shame():
    """Rule: no blame, no shame, no moralize.

    The voice is the whole point of the constrained call; verifying
    these forbidden modes are explicitly off-limits stops a future
    "tighten the tone" edit from quietly relaxing the contract.
    """
    lowered = GAP_SYSTEM_PROMPT.lower()
    assert "blame" in lowered
    assert "shame" in lowered or "shaming" in lowered


def test_system_prompt_forbids_advice():
    """Another section handles "what to try"; this judge writes the gap."""
    lowered = GAP_SYSTEM_PROMPT.lower()
    assert "advice" in lowered or "prescribe" in lowered


def test_system_prompt_forbids_accusatory_setups():
    """No "but" / "however" / "actually" pivots.

    Those words frame the user as someone needing correction. The
    constrained voice notes the gap without litigating it.
    """
    lowered = GAP_SYSTEM_PROMPT.lower()
    assert '"but"' in lowered or 'do not write "but"' in lowered


# --------------------------------------------------------- user prompt


def test_user_prompt_includes_commitment_text():
    """The model sees what the commitment for the week actually was.

    Without this the prose would be generic; the spec section 2
    masthead is anchored on the commitment, so the closing prose
    needs the same context.
    """
    rollup = _rollup(display_text="Paste the error before debugging.")
    prompt = _build_user_prompt(rollup)
    assert "Paste the error before debugging." in prompt


def test_user_prompt_includes_target_dim_key():
    """The model knows WHICH dim the commitment targets."""
    rollup = _rollup(target_dim_key="verification")
    prompt = _build_user_prompt(rollup)
    assert "verification" in prompt


def test_user_prompt_includes_self_report_counts():
    """All four buckets land in the prompt so the model sees the tally shape.

    Even zero-count buckets help the model interpret the signal (a
    "0 partial" line is different evidence than a missing "partial"
    field). The structured key-value format also makes it less
    likely the model regurgitates the count verbatim into the prose.
    """
    rollup = _rollup(
        self_report_tally={"yes": 3, "no": 1, "partial": 2, "skip": 1}
    )
    prompt = _build_user_prompt(rollup)
    assert "3 yes" in prompt
    assert "1 no" in prompt
    assert "2 partial" in prompt
    assert "1 skip" in prompt


def test_user_prompt_includes_before_after_per_dim_values():
    """The targeted dim's before/after pair is the load-bearing data.

    Without these the model cannot judge the size of the gap; the
    prompt format puts them in a single line so the model parses the
    "-> this week" arrow as the comparison axis.
    """
    rollup = _rollup(
        dim_before={"verification": 6.5}, dim_after={"verification": 4.8}
    )
    prompt = _build_user_prompt(rollup)
    assert "6.5" in prompt
    assert "4.8" in prompt


def test_user_prompt_omits_non_targeted_dims():
    """Other dim values are excluded so the model focuses on the targeted one.

    Five additional dim columns would be noise; the commitment is
    about ONE dimension, and the prose is about THAT difference.
    """
    rollup = _rollup(
        target_dim_key="verification",
        dim_before={"verification": 6.5, "planning": 7.2, "context": 5.5},
        dim_after={"verification": 4.8, "planning": 7.5, "context": 5.7},
    )
    prompt = _build_user_prompt(rollup)
    # The targeted dim's values appear; the other dims' values do not.
    assert "6.5" in prompt
    assert "4.8" in prompt
    assert "7.2" not in prompt
    assert "5.5" not in prompt


def test_user_prompt_handles_missing_prior_week_value():
    """When there is no prior week's dim value the prompt says so.

    Disagreement requires both signals, so this branch is rarely
    exercised by `apply_gap_prose`; the helper is defensive in case
    a future caller invokes the prompt builder directly with a
    first-week rollup.
    """
    rollup = _rollup(dim_before={}, dim_after={"verification": 4.8})
    prompt = _build_user_prompt(rollup)
    assert "no prior week" in prompt


# --------------------------------------------------------- disagreement gate


def test_is_disagreement_true_when_yes_heavy_and_dim_worse():
    """The canonical disagreement scenario: yes-claims but data fell."""
    rollup = _rollup(
        self_report_tally={"yes": 5, "no": 0, "partial": 0},
        dim_before={"verification": 6.5},
        dim_after={"verification": 4.8},
    )
    assert _is_disagreement(rollup) is True


def test_is_disagreement_true_when_no_heavy_and_dim_improved():
    """Less common but still a real divergence."""
    rollup = _rollup(
        self_report_tally={"yes": 0, "no": 4, "partial": 1},
        dim_before={"verification": 4.0},
        dim_after={"verification": 6.5},
    )
    assert _is_disagreement(rollup) is True


def test_is_disagreement_false_when_signals_agree():
    """Both point the same way: no judge call worth making."""
    rollup = _rollup(
        self_report_tally={"yes": 4, "no": 1, "partial": 0},
        dim_before={"verification": 4.5},
        dim_after={"verification": 6.2},
    )
    assert _is_disagreement(rollup) is False


def test_is_disagreement_false_when_tally_empty():
    """Empty tally => no self signal => no disagreement to surface."""
    rollup = _rollup(
        self_report_tally={"yes": 0, "no": 0, "partial": 0, "skip": 0},
        dim_before={"verification": 6.0},
        dim_after={"verification": 4.5},
    )
    assert _is_disagreement(rollup) is False


def test_is_disagreement_false_without_baseline():
    """First weekly run has no prior dim mean => no data signal."""
    rollup = _rollup(
        self_report_tally={"yes": 5, "no": 0},
        dim_before={},
        dim_after={"verification": 4.5},
    )
    assert _is_disagreement(rollup) is False


def test_is_disagreement_false_when_sessions_zero():
    """No sessions logged this week => no progress to compare."""
    rollup = _rollup(
        sessions_this_week=0,
        self_report_tally={"yes": 5, "no": 0},
        dim_before={"verification": 6.5},
        dim_after={"verification": 4.5},
    )
    assert _is_disagreement(rollup) is False


def test_is_disagreement_false_when_dim_delta_within_noise_band():
    """A < 0.3 magnitude delta is "no change" not a regression.

    The noise band is the documented threshold (spec section 8.3);
    a sub-band move is treated as "no signal" rather than a real
    direction, matching the renderer's gap-line decision.
    """
    rollup = _rollup(
        self_report_tally={"yes": 5, "no": 0},
        dim_before={"verification": 5.0},
        dim_after={"verification": 5.1},
    )
    assert _is_disagreement(rollup) is False


# --------------------------------------------------------- generate_gap_prose


class _FakeAnthropicMessage:
    """Minimal stand-in for the anthropic SDK's response object."""

    def __init__(self, text: str) -> None:
        self.content = [type("Block", (), {"type": "text", "text": text})()]


def _install_fake_anthropic(monkeypatch, *, returns: str = "", raises: Exception | None = None) -> list[dict]:
    """Install a fake `anthropic` module and return the call-args log.

    The fake captures each call's args so tests can assert on the
    model, system prompt, and max_tokens. ``returns`` is the canned
    text content; if ``raises`` is set the call raises that exception
    instead.
    """
    calls: list[dict] = []

    class _FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            if raises is not None:
                raise raises
            return _FakeAnthropicMessage(returns)

    class _FakeAnthropicClient:
        def __init__(self, *args, **kwargs):
            self.messages = _FakeMessages()

    fake_module = type(sys)("anthropic")
    fake_module.Anthropic = _FakeAnthropicClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    return calls


def _install_fake_openai(monkeypatch, *, returns: str = "", raises: Exception | None = None) -> list[dict]:
    """Install a fake `openai` module and return the call-args log."""
    calls: list[dict] = []

    class _FakeChoices:
        def __init__(self, text: str) -> None:
            self.message = type("M", (), {"content": text})

    class _FakeCompletionsResponse:
        def __init__(self, text: str) -> None:
            self.choices = [_FakeChoices(text)]

    class _FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if raises is not None:
                raise raises
            return _FakeCompletionsResponse(returns)

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeOpenAIClient:
        def __init__(self, *args, **kwargs):
            self.chat = _FakeChat()

    fake_module = type(sys)("openai")
    fake_module.OpenAI = _FakeOpenAIClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    return calls


def test_generate_returns_none_without_api_keys(tmp_home):
    """Spec AC #3: no API key => the renderer falls back to the static line.

    tmp_home already deletes both API key env vars; this test just
    confirms the helper short-circuits to None instead of attempting
    a call that would fail at the SDK boundary.
    """
    assert generate_gap_prose(_rollup()) is None


def test_generate_calls_claude_when_anthropic_key_present(tmp_home, monkeypatch):
    """Happy path: Claude succeeds, the text content is returned verbatim."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _install_fake_anthropic(
        monkeypatch,
        returns="The numbers and your check-ins disagree this week. Worth a closer look.",
    )
    out = generate_gap_prose(_rollup())
    assert out == (
        "The numbers and your check-ins disagree this week. Worth a closer look."
    )


def test_generate_passes_cheap_model_and_system_prompt(tmp_home, monkeypatch):
    """The call must hit the cheap-tier model with the constrained prompt.

    A future provider-tier rename or a stray copy edit on the system
    prompt is caught here rather than after a real bill arrives.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = _install_fake_anthropic(
        monkeypatch, returns="Two sentences are fine. Curiosity intact."
    )
    generate_gap_prose(_rollup())
    assert len(calls) == 1
    assert calls[0]["model"] == CLAUDE_CHEAP_MODEL
    assert calls[0]["system"] == GAP_SYSTEM_PROMPT


def test_generate_falls_back_to_openai_when_claude_raises(tmp_home, monkeypatch):
    """Claude raises => the OpenAI provider gets a turn.

    Mirrors the multi-provider fallback in ``praxis.scoring.judge.score_session``;
    a transient outage on one provider must not block the prose call.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anth")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    _install_fake_anthropic(monkeypatch, raises=RuntimeError("503"))
    openai_calls = _install_fake_openai(
        monkeypatch, returns="OpenAI wrote this one."
    )
    out = generate_gap_prose(_rollup())
    assert out == "OpenAI wrote this one."
    assert len(openai_calls) == 1
    assert openai_calls[0]["model"] == OPENAI_CHEAP_MODEL


def test_generate_returns_none_when_both_providers_raise(tmp_home, monkeypatch):
    """Spec AC #3: judge failure => static fallback (None to the renderer)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anth")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    _install_fake_anthropic(monkeypatch, raises=RuntimeError("anth 503"))
    _install_fake_openai(monkeypatch, raises=RuntimeError("oai 503"))
    assert generate_gap_prose(_rollup()) is None


def test_generate_returns_none_when_response_is_empty(tmp_home, monkeypatch):
    """Empty response => treat as failure so the static fallback fires.

    An empty-string response from the cheap tier means we have nothing
    to render; substituting an empty Gap field would visually drop
    the line altogether, which is the failure mode the static
    fallback exists to prevent.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _install_fake_anthropic(monkeypatch, returns="   ")
    assert generate_gap_prose(_rollup()) is None


def test_generate_returns_none_when_anthropic_sdk_missing(tmp_home, monkeypatch):
    """Missing optional SDK => None.

    The user installed praxis without the anthropic extra but DID set
    ANTHROPIC_API_KEY; we treat that as "no judge available" and
    fall through to OpenAI (or the static fallback) rather than
    crashing.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    real_import = __import__

    def _fail_anthropic_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no anthropic installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fail_anthropic_import)
    assert generate_gap_prose(_rollup()) is None


def test_generate_prefer_openai_uses_openai_first(tmp_home, monkeypatch):
    """`prefer="openai"` flips the dispatch order without changing fallbacks."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anth")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    anth_calls = _install_fake_anthropic(
        monkeypatch, returns="Claude prose."
    )
    openai_calls = _install_fake_openai(
        monkeypatch, returns="OpenAI prose."
    )
    out = generate_gap_prose(_rollup(), prefer="openai")
    assert out == "OpenAI prose."
    assert len(openai_calls) == 1
    # Claude must not be called when OpenAI succeeds first.
    assert len(anth_calls) == 0


# --------------------------------------------------------- apply_gap_prose


def test_apply_returns_none_when_input_is_none():
    """Defensive: the orchestrator passes None when no commitment exists."""
    assert apply_gap_prose(None) is None


def test_apply_returns_input_unchanged_when_no_disagreement(tmp_home):
    """No disagreement => no judge call => identity (same object).

    Returning the SAME object (not a copy) is a small but real cost
    saver for the orchestrator's downstream code; the rollup is
    frozen so a no-op replace would still be a fresh instance with
    different identity.
    """
    rollup = _rollup(
        self_report_tally={"yes": 5, "no": 0},
        dim_before={"verification": 4.0},
        dim_after={"verification": 6.0},
    )
    assert apply_gap_prose(rollup) is rollup


def test_apply_returns_input_unchanged_when_no_api_key(tmp_home):
    """Disagreement but no API key => static fallback (no prose attached)."""
    rollup = _rollup()  # disagree-shaped by default
    out = apply_gap_prose(rollup)
    assert out is rollup
    assert rollup.gap_prose is None


def test_apply_attaches_prose_when_disagreement_and_judge_succeeds(
    tmp_home, monkeypatch
):
    """Happy path: disagreement + judge succeeds => prose attached."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _install_fake_anthropic(
        monkeypatch,
        returns="The numbers and your reflections diverged this week.",
    )
    rollup = _rollup()  # disagree-shaped by default
    out = apply_gap_prose(rollup)
    assert out is not None
    assert out.gap_prose == (
        "The numbers and your reflections diverged this week."
    )
    # The other fields are preserved.
    assert out.display_text == rollup.display_text
    assert out.target_dim_key == rollup.target_dim_key
    assert out.sessions_this_week == rollup.sessions_this_week


def test_apply_returns_input_when_judge_returns_none(tmp_home, monkeypatch):
    """Judge returned None => no prose attached => static fallback fires."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _install_fake_anthropic(monkeypatch, returns="")
    rollup = _rollup()
    out = apply_gap_prose(rollup)
    assert out is rollup
    assert rollup.gap_prose is None


# --------------------------------------------------------- fallback constant


def test_gap_fallback_line_matches_renderer():
    """The exported fallback constant matches the renderer's static line.

    The renderer ships its own `_GAP_DISAGREE_LINE` so neither file
    has to import the other; this test pins both surfaces to the
    same string so a future copy edit on one would surface the drift.
    """
    from praxis.reports.digest_terminal import _GAP_DISAGREE_LINE

    assert GAP_FALLBACK_LINE == _GAP_DISAGREE_LINE
