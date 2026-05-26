"""Per-model usage analysis and advice.

For each model the user actually uses, we:
  1. Collect the sessions where it was used
  2. Summarize how they're using it (task types, prompt patterns)
  3. Compare that usage to the model card's strengths and weaknesses
  4. Generate specific advice: "you're using Opus for trivial classifications;
     here's why Haiku is the right tool for that," etc.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean

from praxis.behavior.signals import BehavioralSignals
from praxis.models import Session
from praxis.models_advisor.cards import (
    ModelCard,
    find_card_for_model_hint,
)


@dataclass
class ModelUsageProfile:
    """How the user uses one specific model."""

    model_hint: str                       # what we saw in session metadata
    card: ModelCard | None                # resolved card, if any
    session_count: int
    total_user_turns: int
    avg_prompt_chars: float
    avg_engagement_rate: float
    avg_delegation_rate: float
    avg_overall_score: float | None       # from scoring pipeline
    common_tasks: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    fit_assessment: str = ""              # "well-matched" / "over-using" / "under-using" / "unknown"

    # Cost estimates. Both None if the card lacks pricing data.
    estimated_cost_usd: float | None = None        # estimated total spend across scanned sessions
    estimated_cost_per_session_usd: float | None = None
    cost_basis_note: str = ""              # explains the assumptions used


def group_sessions_by_model(
    enriched: list[tuple[Session, BehavioralSignals, float | None]],
) -> dict[str, list[tuple[Session, BehavioralSignals, float | None]]]:
    """Bucket sessions by model_hint. Sessions with no hint go to 'unknown'."""
    buckets: dict[str, list] = {}
    for item in enriched:
        session = item[0]
        key = session.model_hint or "unknown"
        buckets.setdefault(key, []).append(item)
    return buckets


def _summarize_tasks(sessions: list[Session]) -> list[str]:
    """Crude task classification from the first user prompt of each session."""
    tasks: Counter[str] = Counter()
    for session in sessions:
        if not session.user_turns:
            continue
        first = session.user_turns[0].content.lower()[:300]
        if any(k in first for k in ["bug", "error", "fail", "broken", "debug", "trace"]):
            tasks["debugging"] += 1
        elif any(k in first for k in ["refactor", "rewrite", "clean up", "simplify"]):
            tasks["refactoring"] += 1
        elif any(k in first for k in ["write", "implement", "build", "create", "add"]):
            tasks["code generation"] += 1
        elif any(k in first for k in ["explain", "what is", "how does", "why does"]):
            tasks["learning / explanation"] += 1
        elif any(k in first for k in ["review", "feedback", "thoughts on"]):
            tasks["review / critique"] += 1
        elif any(k in first for k in ["test", "unit test", "integration"]):
            tasks["testing"] += 1
        elif any(k in first for k in ["architecture", "design", "approach"]):
            tasks["architecture / design"] += 1
        elif any(k in first for k in ["classify", "extract", "parse", "format"]):
            tasks["classification / extraction"] += 1
        else:
            tasks["other"] += 1
    return [f"{name} ({count})" for name, count in tasks.most_common(5)]


def _heuristic_fit(profile_data: dict) -> tuple[str, list[str]]:
    """Cheap fit assessment without an LLM call."""
    card = profile_data["card"]
    if card is None:
        return "unknown", [
            f"No model card on file for '{profile_data['model_hint']}'. "
            f"Drop a JSON card in ~/.praxis/model_cards/ to get tailored advice."
        ]

    avg_chars = profile_data["avg_prompt_chars"]
    tier = card.tier
    advice: list[str] = []
    fit = "well-matched"

    # Frontier model used for short, simple prompts = over-using
    if tier == "frontier" and avg_chars < 200:
        fit = "over-using"
        fast_card = _find_fast_tier_card_in_family(card.family)
        fast_name = fast_card.display_name if fast_card else "a faster tier in the same family"
        advice.append(
            f"Your average prompt to {card.display_name} is {int(avg_chars)} characters — "
            f"very short for a frontier model. If these are quick lookups or "
            f"simple classifications, {fast_name} would be cheaper and probably as good."
        )

    # Fast model used for tasks the card says to avoid
    if tier == "fast" and avg_chars > 1500:
        fit = "under-using"
        advice.append(
            f"Your prompts to {card.display_name} are long ({int(avg_chars)} chars avg). "
            f"Long prompts often mean complex tasks — and complex tasks "
            f"are where this tier struggles. Try a higher tier on the same vendor "
            f"for tasks like architecture, deep refactors, or hard reasoning."
        )

    # Surface 1-2 most relevant prompting quirks
    advice.extend(card.prompting_quirks[:2])

    # If their engagement on this model is low, flag the model-card best_for that might explain why
    if profile_data["avg_engagement_rate"] < 0.1 and card.best_for:
        advice.append(
            f"You're not asking many 'why' or 'how' questions when using "
            f"{card.display_name}. Given its strengths in "
            f"{card.best_for[0].lower()}, you'd get more out of it by engaging "
            f"with the reasoning, not just the output."
        )

    return fit, advice


def _llm_fit(profile_data: dict, recent_prompts: list[str]) -> tuple[str, list[str]] | None:
    """LLM-generated fit assessment with specific advice."""
    have_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if not (have_anthropic or have_openai):
        return None

    card = profile_data["card"]
    if card is None:
        return None

    system_prompt = """You advise a person on how well they're using one specific AI model, given that model's documented strengths and weaknesses.

The person is using multiple AI models. They want a separate read for each one, because Opus and Haiku want different prompting, and GPT-5 and Gemini have different sweet spots. Generic prompting advice is useless to them.

# Inputs you'll receive

  1. A model card for ONE model: its documented strengths, weaknesses, best uses, prompting quirks.
  2. Stats on how this person actually uses that model: how often, prompt lengths, engagement patterns, task types.
  3. Sample opening prompts from their recent sessions with this model.

# What to produce

A fit assessment plus specific advice. Definitions:

  - "well-matched"  — tasks align with model strengths, prompting style suits the model.
  - "over-using"    — using a frontier/expensive model for tasks a faster/cheaper one would handle as well.
  - "under-using"   — using a fast/cheap model for tasks that would benefit from a bigger one.
  - "mixed"         — some tasks well-matched, others not.

Advice should be 2-4 items. Each one specific and actionable. Reference what this person ACTUALLY does, not generic best practice.

  BAD:  "Use clear prompts."
  GOOD: "Your average prompt to Opus is 90 characters — far below what frontier models reward. Either lengthen your context (this model excels with files-pasted-in style) or move these queries to Haiku."

# Hard constraints

- Never invent capabilities. If something isn't in the model card, don't claim it.
- Never compare to other models unless the card mentions them.
- Don't recommend models the user isn't using.
- If the model card is sparse and the usage data is thin, say so rather than padding.

# Voice

Direct, practical, warm. Senior engineer telling a colleague what you noticed, not a tool generating advice slop. No corporate jargon. Active voice. Numbers with context.

# Output

Return ONLY valid JSON, no preamble:

{
  "fit_assessment": "<well-matched|over-using|under-using|mixed>",
  "advice": [
    "<specific, actionable, references this user's actual usage>",
    "..."
  ]
}

2-4 advice items."""

    user_msg = (
        f"MODEL CARD:\n"
        f"  Name: {card.display_name} ({card.vendor})\n"
        f"  Tier: {card.tier}\n"
        f"  Strengths: {card.strengths}\n"
        f"  Weaknesses: {card.weaknesses}\n"
        f"  Best for: {card.best_for}\n"
        f"  Avoid for: {card.avoid_for}\n"
        f"  Prompting quirks: {card.prompting_quirks}\n\n"
        f"USER'S USAGE OF THIS MODEL:\n"
        f"  Sessions: {profile_data['session_count']}\n"
        f"  Avg prompt length: {int(profile_data['avg_prompt_chars'])} chars\n"
        f"  Engagement rate: {profile_data['avg_engagement_rate']:.2f}\n"
        f"  Delegation rate: {profile_data['avg_delegation_rate']:.2f}\n"
        f"  Common task types: {profile_data['common_tasks']}\n\n"
        f"SAMPLE OPENING PROMPTS:\n"
        + "\n".join(f"  - {p[:200]}" for p in recent_prompts[:5])
    )

    text: str = ""
    try:
        if have_anthropic:
            from anthropic import Anthropic  # type: ignore
            client = Anthropic()
            resp = client.messages.create(
                model="claude-opus-4-7",
                max_tokens=1200,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        else:
            from openai import OpenAI  # type: ignore
            client = OpenAI()
            resp = client.chat.completions.create(
                model="gpt-5",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        print(f"[model_advisor] LLM call failed: {exc!r}", file=sys.stderr)
        return None

    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[-1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
            cleaned = cleaned.rsplit("```", 1)[0]
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        payload = json.loads(cleaned[start : end + 1])
        return (
            payload.get("fit_assessment", "well-matched"),
            list(payload.get("advice", [])),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[model_advisor] failed to parse LLM response: {exc!r}", file=sys.stderr)
        return None


def _estimate_cost(
    card: ModelCard | None,
    total_input_chars: int,
    session_count: int,
    avg_output_multiplier: float = 1.5,
    chars_per_token: float = 4.0,
) -> tuple[float | None, float | None, str]:
    """Rough cost estimate from prompt character volume and card pricing.

    Assumptions (deliberately conservative — chat output is often 1-2x input
    on coding workloads):

      - 4 chars per token (English text)
      - Output tokens ≈ avg_output_multiplier × input tokens
      - No prompt caching or batch discounts applied

    Returns (total_usd, per_session_usd, basis_note). All None when the
    card has no pricing data.
    """
    if card is None or card.input_per_million_usd is None or card.output_per_million_usd is None:
        return None, None, ""

    input_tokens = total_input_chars / chars_per_token
    output_tokens = input_tokens * avg_output_multiplier
    total_usd = (
        input_tokens * card.input_per_million_usd / 1_000_000
        + output_tokens * card.output_per_million_usd / 1_000_000
    )
    per_session = total_usd / session_count if session_count else 0.0
    note = (
        f"Estimate assumes ~{chars_per_token:.0f} chars/token and output ≈ "
        f"{avg_output_multiplier:.1f}× input. No caching/batch discounts. "
        f"Rates as of {card.pricing_last_verified or '?'}."
    )
    return total_usd, per_session, note


def _find_fast_tier_card_in_family(family: str) -> ModelCard | None:
    """Look up the fast-tier card in the same family, if one exists."""
    from praxis.models_advisor.cards import load_all_cards
    for card in load_all_cards().values():
        if card.family == family and card.tier == "fast":
            return card
    return None


def _cost_advice(
    card: ModelCard,
    avg_prompt_chars: float,
    estimated_cost: float | None,
    fit: str,
) -> list[str]:
    """Append cost-aware advice when the data warrants it."""
    out: list[str] = []
    if card.input_per_million_usd is None:
        return out

    if fit == "over-using" and avg_prompt_chars < 200 and estimated_cost and estimated_cost > 0:
        # Compute the cheaper-tier savings multiplier from card pricing
        # rather than hardcoding (the previous "~15×" string drifted whenever
        # vendor pricing changed). Mention the cheaper tier by name when we
        # know it, otherwise stay generic.
        fast_card = _find_fast_tier_card_in_family(card.family)
        savings_hint = ""
        if fast_card and fast_card.input_per_million_usd:
            mult = card.input_per_million_usd / fast_card.input_per_million_usd
            if mult >= 2.0:
                savings_hint = (
                    f"{fast_card.display_name} is ~{mult:.0f}× cheaper per input token."
                )
        out.append(
            f"Estimated spend on {card.display_name} in this window: "
            f"~${estimated_cost:.2f}. For short prompts like yours, moving to a "
            f"faster tier would cut that materially. {savings_hint}".rstrip()
        )

    if card.pricing_notes:
        # One-line caveat about the pricing model.
        out.append(card.pricing_notes)
    return out


def build_profiles(
    enriched: list[tuple[Session, BehavioralSignals, float | None]],
    use_llm: bool = True,
) -> list[ModelUsageProfile]:
    """Produce one ModelUsageProfile per model the user actually used."""
    buckets = group_sessions_by_model(enriched)
    profiles: list[ModelUsageProfile] = []

    for model_hint, items in buckets.items():
        if model_hint == "unknown":
            continue  # Don't generate advice without a model identity
        sessions = [item[0] for item in items]
        signals = [item[1] for item in items]
        overall_scores = [item[2] for item in items if item[2] is not None]

        all_prompt_chars: list[int] = []
        recent_prompts: list[str] = []
        for s in sessions:
            for t in s.user_turns:
                all_prompt_chars.append(len(t.content))
            if s.user_turns:
                recent_prompts.append(s.user_turns[0].content)

        avg_prompt_chars = mean(all_prompt_chars) if all_prompt_chars else 0.0
        avg_engagement = mean(sig.engagement_rate for sig in signals) if signals else 0.0
        avg_delegation = mean(sig.delegation_rate for sig in signals) if signals else 0.0
        avg_overall = mean(overall_scores) if overall_scores else None
        total_user_turns = sum(sig.user_turn_count for sig in signals)
        common_tasks = _summarize_tasks(sessions)
        total_input_chars = sum(all_prompt_chars)

        card = find_card_for_model_hint(model_hint)

        profile_data = {
            "model_hint": model_hint,
            "card": card,
            "session_count": len(sessions),
            "total_user_turns": total_user_turns,
            "avg_prompt_chars": avg_prompt_chars,
            "avg_engagement_rate": avg_engagement,
            "avg_delegation_rate": avg_delegation,
            "common_tasks": common_tasks,
        }

        fit = "unknown"
        advice: list[str] = []
        if use_llm:
            llm_result = _llm_fit(profile_data, recent_prompts)
            if llm_result is not None:
                fit, advice = llm_result

        if not advice:
            fit, advice = _heuristic_fit(profile_data)

        # Cost estimate + cost-aware advice (always heuristic, not LLM).
        total_cost, per_session_cost, cost_note = _estimate_cost(
            card, total_input_chars, len(sessions),
        )
        if card is not None:
            advice = list(advice) + _cost_advice(card, avg_prompt_chars, total_cost, fit)

        profiles.append(ModelUsageProfile(
            model_hint=model_hint,
            card=card,
            session_count=len(sessions),
            total_user_turns=total_user_turns,
            avg_prompt_chars=avg_prompt_chars,
            avg_engagement_rate=avg_engagement,
            avg_delegation_rate=avg_delegation,
            avg_overall_score=avg_overall,
            common_tasks=common_tasks,
            advice=advice,
            fit_assessment=fit,
            estimated_cost_usd=total_cost,
            estimated_cost_per_session_usd=per_session_cost,
            cost_basis_note=cost_note,
        ))

    # Sort by session count, most-used first
    profiles.sort(key=lambda p: p.session_count, reverse=True)
    return profiles
