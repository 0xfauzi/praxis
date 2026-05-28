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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import mean

from praxis.behavior.signals import BehavioralSignals
from praxis.models import Session
from praxis.models_advisor.cards import (
    ModelCard,
    find_card_for_model_hint,
)


# --------------------------------------------------------------------
# US-042 counterfactual cost-effectiveness rule.
#
# The refined cost-effectiveness panel surfaces a deterministic
# "You spent $X on <higher-tier model> for tasks <lower-tier model> could
# have done = $Y overspend" line. The rule below is the canonical
# definition: a session counts as overspent iff
#   1. Its model resolves to a frontier-tier card with a fast-tier
#      sibling in the same family,
#   2. Its workload is small (<= COUNTERFACTUAL_MAX_USER_TURNS user
#      turns AND <= COUNTERFACTUAL_MAX_AVG_PROMPT_CHARS average prompt
#      length), which is the same shape the cost ledger's tier-fit
#      panel uses.
# The dollar-difference per session is computed under the same
# chars-per-token + output-multiplier assumptions as
# ``praxis.scoring.cost_ledger.estimate_session_cost_usd`` so the
# numbers on this panel and on the cost ledger never disagree.
#
# When multiple frontier models qualify, the panel reports the pair that
# accumulated the most overspend (deterministic tiebreak below). This is
# both legible for the user and consistent under reordering of inputs.

COUNTERFACTUAL_MAX_USER_TURNS = 3
COUNTERFACTUAL_MAX_AVG_PROMPT_CHARS = 200.0
COUNTERFACTUAL_CHARS_PER_TOKEN = 4.0
COUNTERFACTUAL_OUTPUT_TO_INPUT_RATIO = 1.5


@dataclass(frozen=True)
class CounterfactualOverspend:
    """Deterministic counterfactual overspend across the week's sessions.

    ``higher_tier_display`` / ``lower_tier_display`` are the resolved
    card display names of the dominant (frontier, fast) pair, or empty
    strings when no session qualified. ``spent_on_higher_tier_usd`` is
    the sum of frontier costs across qualifying sessions for that pair;
    ``overspend_usd`` is the sum of (frontier_cost - fast_cost) across
    qualifying sessions for that pair.

    ``had_any_priced_session`` is True iff at least one input session
    had a non-None frontier cost (i.e. a resolvable card with per-token
    pricing). The refined cost-effectiveness panel uses this flag to
    decide between "$Y overspend" copy and the explicit "No cost data
    this week." empty-state (US-042 AC: $0 overspend must not be
    rendered when no cost data exists, since 0 falsely implies
    optimality).
    """

    higher_tier_display: str = ""
    lower_tier_display: str = ""
    spent_on_higher_tier_usd: float = 0.0
    overspend_usd: float = 0.0
    qualifying_session_count: int = 0
    had_any_priced_session: bool = False


def _counterfactual_costs(
    card: ModelCard, total_input_chars: int,
) -> float | None:
    """Cost in USD for ``total_input_chars`` under ``card`` pricing.

    Mirrors the assumptions in
    ``praxis.scoring.cost_ledger.estimate_session_cost_usd`` so this
    panel and the weekly ledger agree on every session's spend. Returns
    None when the card lacks per-token pricing.
    """
    if card.input_per_million_usd is None or card.output_per_million_usd is None:
        return None
    if total_input_chars <= 0:
        return 0.0
    input_tokens = total_input_chars / COUNTERFACTUAL_CHARS_PER_TOKEN
    output_tokens = input_tokens * COUNTERFACTUAL_OUTPUT_TO_INPUT_RATIO
    return (
        input_tokens * card.input_per_million_usd / 1_000_000
        + output_tokens * card.output_per_million_usd / 1_000_000
    )


def _fast_tier_sibling(
    family: str, all_cards: dict[str, ModelCard],
) -> ModelCard | None:
    """Lowest-priced fast-tier card in ``family``, deterministic by id.

    When a family ships more than one fast-tier card the rule needs a
    stable choice so the panel cannot flip across runs. Ties are broken
    by ascending card id; that is the same order ``load_all_cards``
    populates and any future ordering is decoupled from JSON-load
    iteration order.
    """
    candidates = [
        c for c in all_cards.values() if c.family == family and c.tier == "fast"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c.id)
    return candidates[0]


def compute_counterfactual_overspend(
    sessions: list[Session],
) -> CounterfactualOverspend:
    """Apply the deterministic counterfactual rule to a session list.

    A session ``s`` qualifies for overspend attribution iff every clause
    below holds:

      1. ``s.model_hint`` resolves to a frontier-tier card with
         per-token pricing.
      2. The family ships a fast-tier sibling with per-token pricing
         (looked up via ``_fast_tier_sibling``; deterministic by card id
         tiebreak).
      3. ``len(s.user_turns) <= COUNTERFACTUAL_MAX_USER_TURNS``.
      4. The session's average prompt char volume is at most
         ``COUNTERFACTUAL_MAX_AVG_PROMPT_CHARS``.

    For each qualifying session, the overspend is
    ``frontier_cost - fast_cost``; the spent figure attributed to the
    higher tier is ``frontier_cost``. The returned panel reports the
    (higher, lower) display pair with the largest overspend; ties break
    by the higher-tier card id ascending so the result is reproducible.

    ``had_any_priced_session`` flips True the moment any input session
    resolves to a card with per-token pricing AND a positive frontier
    cost; this is the gate the panel uses to distinguish "$0 overspend
    because nothing qualified" from "no cost data at all this week"
    (US-042 AC).
    """
    if not sessions:
        return CounterfactualOverspend()

    all_cards = _load_all_cards_cached()

    # Per-(higher_id, lower_id) pair: sum of frontier spend + overspend
    # across qualifying sessions, plus display names + qualifying counts.
    pair_higher_spend: dict[tuple[str, str], float] = defaultdict(float)
    pair_overspend: dict[tuple[str, str], float] = defaultdict(float)
    pair_session_count: dict[tuple[str, str], int] = defaultdict(int)
    pair_display: dict[tuple[str, str], tuple[str, str]] = {}

    had_any_priced_session = False

    for session in sessions:
        card = find_card_for_model_hint(getattr(session, "model_hint", None))
        if card is None:
            continue
        # Compute frontier cost for this session against ITS card; this
        # is how we detect "had cost data" - any session whose card has
        # pricing and a positive cost counts.
        user_turns = session.user_turns
        total_chars = sum(len(t.content) for t in user_turns)
        cost = _counterfactual_costs(card, total_chars)
        if cost is None:
            continue
        # A session with a priced card AND a non-trivial spend counts as
        # "we have cost data this week"; a card-only session whose char
        # count is zero is borderline (the user opened the chat but did
        # not write). We treat any priced session with cost > 0 as
        # evidence of priced activity so the empty-state copy fires only
        # on truly cost-free weeks.
        if cost > 0:
            had_any_priced_session = True

        # Only frontier-tier sessions can be overspent.
        if card.tier != "frontier":
            continue

        # Workload threshold gate. Compute the average prompt char count
        # against the user-turn list; a session with no user turns
        # technically has avg_chars = 0 which is <= the threshold, but
        # it also has total_chars = 0 and contributes nothing to either
        # spend or overspend, so the qualifying-count includes it
        # honestly without inflating the dollar figures.
        n_turns = len(user_turns)
        avg_chars = (total_chars / n_turns) if n_turns else 0.0
        if n_turns > COUNTERFACTUAL_MAX_USER_TURNS:
            continue
        if avg_chars > COUNTERFACTUAL_MAX_AVG_PROMPT_CHARS:
            continue

        # The session qualifies; resolve the fast-tier sibling.
        fast = _fast_tier_sibling(card.family, all_cards)
        if fast is None:
            continue
        fast_cost = _counterfactual_costs(fast, total_chars)
        if fast_cost is None:
            continue

        pair_key = (card.id, fast.id)
        pair_higher_spend[pair_key] += cost
        pair_overspend[pair_key] += cost - fast_cost
        pair_session_count[pair_key] += 1
        # Display names are stable across sessions for a given (higher,
        # lower) pair; setdefault preserves the first observed pair
        # without rewriting.
        pair_display.setdefault(
            pair_key, (card.display_name, fast.display_name)
        )

    if not pair_overspend:
        return CounterfactualOverspend(
            had_any_priced_session=had_any_priced_session,
        )

    # Deterministic tiebreak: most overspend wins; ties broken by
    # ascending higher-tier card id, then ascending fast-tier card id,
    # so reordering inputs cannot flip the report.
    winner = min(
        pair_overspend,
        key=lambda k: (-pair_overspend[k], k[0], k[1]),
    )
    higher_display, lower_display = pair_display[winner]
    return CounterfactualOverspend(
        higher_tier_display=higher_display,
        lower_tier_display=lower_display,
        spent_on_higher_tier_usd=round(pair_higher_spend[winner], 4),
        overspend_usd=round(pair_overspend[winner], 4),
        qualifying_session_count=pair_session_count[winner],
        had_any_priced_session=had_any_priced_session,
    )


def _load_all_cards_cached() -> dict[str, ModelCard]:
    """One-shot wrapper around ``load_all_cards`` so test fixtures can
    monkey-patch the lookup if they need to inject custom cards. Kept as
    a function (not a cached module-level dict) because user cards live
    on disk and may change between tests.
    """
    from praxis.models_advisor.cards import load_all_cards
    return load_all_cards()


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
            client = OpenAI()  # type: ignore[assignment]
            resp = client.chat.completions.create(  # type: ignore[attr-defined]
                model="gpt-5",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""  # type: ignore[attr-defined]
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
