"""Constrained cheap-tier judge for the masthead's gap-summary prose.

Spec section 2 (coaching-reposition) / US-037: when the user's
self-report tally and the per-dim data disagree by at least the
documented noise band, the masthead's "Gap:" field renders an LLM-
written closing sentence (or two) that names the difference under
curiosity-only framing. This module is the single place that call
happens.

Public surface:
- ``GAP_SYSTEM_PROMPT`` - the constrained-voice system prompt.
- ``generate_gap_prose(rollup)`` - cheap-tier judge entry point. Returns
  a prose string when a provider responds, None on failure or when no
  API key is configured for either provider.
- ``apply_gap_prose(rollup)`` - convenience wrapper used by the
  orchestrator. Detects whether the rollup is in a disagree state
  (delegating to the same threshold ``digest_terminal._gap_summary_line``
  uses) and, when so, calls the judge and returns a new rollup with
  ``gap_prose`` attached. Returns the input rollup unchanged when no
  disagreement is detected or when the judge returns None.
- ``truncate_to_two_sentences(text)`` - defense-in-depth cap that bounds
  any judge response at <= 2 sentences before the renderer surfaces it.

The renderer falls back to the static neutral phrasing
(``_GAP_DISAGREE_LINE`` in ``praxis.reports.digest_terminal``) when:

* the judge call returns None for any reason (no API key, network error,
  empty response, ImportError on the optional SDK), or
* the truncation step yields an empty string.

The disagreement threshold lives in ``digest_terminal`` so the renderer
and the judge call never disagree about when to emit the gap line.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from praxis.reports.commitment_rollup import CommitmentRollup


# Spec section 2: mirror the cheap-tier hint names from
# ``praxis.scoring.judge`` so a future tier rename touches one source
# of truth per surface. We do not import them directly because the
# scoring module pulls in the rubric and other heavy state we do not
# need here; the constants are simple strings.
CLAUDE_CHEAP_MODEL = "claude-haiku-4-5"
OPENAI_CHEAP_MODEL = "gpt-5-mini"


# System prompt for the constrained-judge call. Forbids accusatory
# framing, names the curiosity-only voice, bounds the answer at <= 2
# sentences, and forbids the most common LLM tells (advice, exclamation
# marks, moralizing). The user prompt carries the structured data; this
# prompt carries the rules and the voice.
GAP_SYSTEM_PROMPT = """You write the closing one or two sentences of a weekly self-coaching report's "Gap" field.

The report shows: the user's commitment for the week, how many sessions they ran, what they self-reported (yes / partial / no / skip on whether they kept the commitment), and what the data-based judge measured on the same rubric dimension. The user has already seen the numbers above your line. Your job is to write one or two sentences that name the noticed difference and invite curiosity.

Rules:
- Maximum 2 sentences. Briefer is better. One sentence is fine when one is enough.
- Curiosity only, no accusatory framing. Do not blame, shame, or moralize.
- Do not write "but", "however", "actually" as a setup to a correction. The user is the one doing the work, not a defendant.
- Do not say the user lied, was wrong, missed, or failed. Note the gap, do not litigate it.
- Do not re-list the raw numbers. The reader has them above.
- Do not give advice or prescribe what to try next. Another section handles that.
- No exclamation points. No emojis. No "remember to" or "make sure to".
- Plain ASCII characters only.

Voice: warm, plain, direct. Like a senior colleague pointing at the chart and asking a question.

Output: just the prose. No preamble, no quotes, no markdown."""


# Static fallback emitted when the judge call returns None for any
# reason (no API key, network error, empty response, ImportError on the
# optional SDK). The renderer also carries this constant as
# ``_GAP_DISAGREE_LINE`` so both surfaces share the documented phrasing
# without one importing the other across that boundary.
GAP_FALLBACK_LINE = (
    "Self-report and data differ this week. Worth a moment of curiosity."
)


# Token budget for the cheap-tier call. Two sentences fit comfortably
# inside 240 tokens (under ~960 characters at 4 chars/token); the
# truncation step caps anything that overshoots regardless. We deliberately
# pick a tight budget so a runaway response is cut off at the API layer.
_MAX_OUTPUT_TOKENS = 240


def truncate_to_two_sentences(text: str) -> str:
    """Cap ``text`` at <= 2 sentences with a visible truncation marker.

    Splits on sentence-end punctuation (``.``, ``!``, ``?``) followed by
    whitespace or end-of-string. When three or more sentence ends are
    detected, keeps the first two sentences, strips the trailing
    sentence-end punctuation, and appends ``"..."`` so the reader sees
    the truncation rather than a silent cut.

    Inputs without sentence-end punctuation are returned unchanged: the
    AC bound is on sentence count, not character count, and a
    single-sentence answer (even an unterminated one) is within budget.
    The judge prompt explicitly forbids exceeding two sentences, so this
    helper is defense-in-depth, not the primary guard.
    """
    text = text.strip()
    if not text:
        return ""
    # Find each position just after a sentence-end punctuation followed
    # by whitespace or EOF. Mid-sentence punctuation (abbreviations,
    # decimals) typically lacks the trailing whitespace so it stays out
    # of the count.
    ends: list[int] = []
    for i, ch in enumerate(text):
        if ch in ".!?" and (i + 1 >= len(text) or text[i + 1].isspace()):
            ends.append(i + 1)
    if len(ends) <= 2:
        return text
    cut = ends[1]
    head = text[:cut].rstrip()
    while head and head[-1] in ".!?":
        head = head[:-1]
    return head + "..."


def _format_value_or_placeholder(value: float | None) -> str:
    if value is None:
        return "(no prior week)"
    return f"{float(value):.1f}/10"


def _build_user_prompt(rollup: "CommitmentRollup") -> str:
    """Render the rollup's load-bearing data as the user prompt.

    Only the targeted dimension's before/after pair is included. Other
    dim values are deliberately omitted so the model focuses on the
    dimension the commitment is actually about. The self-report tally
    is rendered as a structured count line (not a sentence) so the model
    does not echo it back into the prose verbatim.
    """
    target_key = rollup.target_dim_key
    dim_after_raw = rollup.dim_after.get(target_key)
    dim_before_raw = rollup.dim_before.get(target_key)
    dim_after = _format_value_or_placeholder(dim_after_raw)
    dim_before = _format_value_or_placeholder(dim_before_raw)
    tally = rollup.self_report_tally
    yes = int(tally.get("yes", 0))
    partial = int(tally.get("partial", 0))
    no = int(tally.get("no", 0))
    skip = int(tally.get("skip", 0))
    return (
        f"Commitment this week: {rollup.display_text}\n"
        f"Targeted rubric dimension: {target_key}\n"
        f"Self-report tally: {yes} yes, {partial} partial, {no} no, {skip} skip "
        f"(over {rollup.sessions_this_week} session"
        f"{'s' if rollup.sessions_this_week != 1 else ''} this week)\n"
        f"Measured on the targeted dimension: "
        f"{dim_before} last week -> {dim_after} this week\n\n"
        "Write the gap-summary closing sentence or two. Maximum 2 sentences."
    )


def _call_claude(user_prompt: str) -> str | None:
    """Cheap-tier Claude call. Returns the text content or None on failure."""
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError:
        return None
    try:
        client = Anthropic()
        response = client.messages.create(
            model=CLAUDE_CHEAP_MODEL,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=GAP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        print(f"[gap_judge] claude call failed: {exc!r}", file=sys.stderr)
        return None


def _call_openai(user_prompt: str) -> str | None:
    """Cheap-tier OpenAI call. Returns the message content or None on failure."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        return None
    try:
        client = OpenAI()
        response = client.chat.completions.create(
            model=OPENAI_CHEAP_MODEL,
            messages=[
                {"role": "system", "content": GAP_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        print(f"[gap_judge] openai call failed: {exc!r}", file=sys.stderr)
        return None


def generate_gap_prose(
    rollup: "CommitmentRollup",
    *,
    prefer: str = "claude",
) -> str | None:
    """Return the cheap-judge prose for the masthead's gap line, or None.

    Returns None whenever the renderer should fall back to the static
    documented phrasing. That covers four cases that all read the same
    from the renderer's side: no API key for either provider, all
    configured providers raised, the response came back empty, or the
    SDK module was not installed.

    ``prefer`` mirrors the contract on ``praxis.scoring.judge.score_session``
    so a future config flip can point both calls at the same provider.
    """
    have_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if not (have_claude or have_openai):
        return None
    user_prompt = _build_user_prompt(rollup)
    order: list[str] = (
        ["claude", "openai"] if prefer == "claude" else ["openai", "claude"]
    )
    for choice in order:
        if choice == "claude" and have_claude:
            result = _call_claude(user_prompt)
            if result:
                return result
        elif choice == "openai" and have_openai:
            result = _call_openai(user_prompt)
            if result:
                return result
    return None


def _is_disagreement(rollup: "CommitmentRollup") -> bool:
    """True iff the self-report and dim data disagree past the noise band.

    Mirrors the agreement check inside ``digest_terminal._gap_summary_line``
    so the orchestrator's "call the judge?" decision uses the same gate
    as the renderer's "render which line?" decision. The threshold lives
    on ``_GAP_DIM_DELTA_THRESHOLD`` in the renderer, the single source
    of truth.
    """
    from praxis.reports.digest_terminal import (
        _dim_movement_signal,
        _self_report_signals_progress,
    )

    if rollup.sessions_this_week <= 0:
        return False
    target_key = rollup.target_dim_key
    dim_after_raw = rollup.dim_after.get(target_key)
    if dim_after_raw is None:
        return False
    dim_after = float(dim_after_raw)
    dim_before_raw = rollup.dim_before.get(target_key)
    dim_before = float(dim_before_raw) if dim_before_raw is not None else None
    self_sig = _self_report_signals_progress(rollup.self_report_tally)
    data_sig = _dim_movement_signal(dim_before, dim_after)
    if self_sig is None or data_sig is None:
        return False
    return self_sig != data_sig


def apply_gap_prose(
    rollup: "CommitmentRollup | None",
) -> "CommitmentRollup | None":
    """Attach cheap-judge prose to ``rollup`` when a disagreement is detected.

    No-op (returns the input unchanged) when:
      - ``rollup`` is None,
      - ``rollup`` is not in a disagree state (see ``_is_disagreement``),
      - the judge call returns None (no API key, all providers raised,
        empty response, or SDK missing).

    Otherwise returns ``dataclasses.replace(rollup, gap_prose=prose)``
    so the renderer's commitment block reads the prose from the rollup
    rather than the static fallback.
    """
    if rollup is None:
        return None
    if not _is_disagreement(rollup):
        return rollup
    prose = generate_gap_prose(rollup)
    if not prose:
        return rollup
    return dataclasses.replace(rollup, gap_prose=prose)
