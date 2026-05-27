"""Moment selector.

After the judge has emitted all candidate Moments for a week, this
module makes ONE cheap-tier LLM call to choose:
  - the headline_moment_id (the moment the digest opens with)
  - up to two supporting_moment_ids (other moments shown in the
    "weakest dim" panel; ideally on dims other than the headline's)
  - a one-sentence headline_reason

Spec section 4.3. The selection is a coaching judgment; a formula
would be a heuristic substitute. The total moments rendered in the
digest is capped at three (headline + at most two supporting).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Severity = Literal["minor", "moderate", "major"]


# Cheap-tier models per primary provider. Spec section 4.3: "Cheap-tier
# model from the user's primary provider (Haiku if Anthropic,
# gpt-5-mini if OpenAI)."
PRIMARY_CHEAP_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5-mini",
}


@dataclass(frozen=True)
class Moment:
    """A structured pointer to a transcript span tied to one rubric dim.

    Spec section 4.1. The selector does not produce Moments; it
    chooses among Moments the judge already produced.
    """

    moment_id: str
    session_stable_id: str
    dim_key: str
    turn_index: int
    quoted_excerpt: str
    why_it_lost_score: str
    suggested_alternative: str
    severity: Severity
    created_at: datetime
    dollar_impact_estimate: float | None = None
    minutes_impact_estimate: int | None = None


@dataclass(frozen=True)
class MomentCandidate:
    """A Moment plus the recency + recurrence context the selector
    needs but the bare Moment doesn't carry. recurrence_count counts
    how often this same (dim_key, suggested_alternative) pair has
    been flagged in the previous 3 weeks (spec section 4.3)."""

    moment: Moment
    session_started_at: datetime
    recurrence_count: int


@dataclass(frozen=True)
class MomentSelection:
    """Output of one selector call. supporting_moment_ids has at most two."""

    headline_moment_id: str
    headline_reason: str
    supporting_moment_ids: list[str] = field(default_factory=list)


class InvalidMomentSelectionError(ValueError):
    """Raised when the selector LLM's response references moment_ids
    that are not in the input candidate set, and the single re-prompt
    permitted by spec section 4.3 / US-030 also failed to produce a
    valid response. US-031's fallback path catches this."""


# (system_prompt, user_prompt, model) -> response text. Tests pass a
# stub here so the pipeline can be exercised without hitting any API.
LLMCaller = Callable[[str, str, str], str]


_SYSTEM_PROMPT = """You are picking the single most coachable moment from a person's AI usage this week. You will see N moment candidates, each with:
  - dim_key
  - quoted_excerpt
  - why_it_lost_score
  - suggested_alternative
  - severity (minor / moderate / major)
  - session_started_at (so you know recency)
  - dollar_impact_estimate (may be null)
  - recurrence_count - times this same suggested_alternative was flagged in any of the previous 3 weeks

Choose:
  1. one headline_moment_id - the single moment most worth opening the week's digest with. Weigh severity, how concrete the alternative is, recurrence (a pattern that keeps happening is more worth coaching than a one-off), and dollar impact.
  2. up to two supporting_moment_ids - other moments worth showing in the "weakest dim" panel. Prefer moments on different dims from the headline.
  3. a one-sentence headline_reason explaining why you picked the headline moment.

Return JSON only, no preamble, no markdown fences:
{
  "headline_moment_id": "...",
  "headline_reason": "<one sentence>",
  "supporting_moment_ids": ["...", "..."]
}
"""


def _candidate_payload(candidate: MomentCandidate) -> dict[str, Any]:
    """Compact dict the LLM sees for one candidate. Includes every
    field listed in the system prompt, plus moment_id (so the LLM
    can refer back to it in headline_moment_id / supporting_moment_ids)."""
    m = candidate.moment
    return {
        "moment_id": m.moment_id,
        "dim_key": m.dim_key,
        "quoted_excerpt": m.quoted_excerpt,
        "why_it_lost_score": m.why_it_lost_score,
        "suggested_alternative": m.suggested_alternative,
        "severity": m.severity,
        "session_started_at": candidate.session_started_at.isoformat(),
        "dollar_impact_estimate": m.dollar_impact_estimate,
        "recurrence_count": candidate.recurrence_count,
    }


def _build_user_prompt(candidates: list[MomentCandidate]) -> str:
    payload = [_candidate_payload(c) for c in candidates]
    return (
        f"Here are the {len(payload)} candidate moments from this week:\n\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "Return JSON only, matching the schema in the system prompt."
    )


def _parse_selection(text: str) -> MomentSelection:
    """Parse the model's JSON response into a MomentSelection.

    Tolerant of markdown fences, preamble/postamble, and missing
    optional fields. The "up to two supporting" UX ceiling from
    spec section 4.3 is enforced here so a chatty model can't
    push more than two into the digest.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in selector response: {text[:200]}")
    payload = json.loads(cleaned[start : end + 1])

    headline = payload.get("headline_moment_id")
    if not isinstance(headline, str) or not headline:
        raise ValueError("selector response missing headline_moment_id")
    reason = str(payload.get("headline_reason", "") or "")
    raw_supporting = payload.get("supporting_moment_ids") or []
    if not isinstance(raw_supporting, list):
        raw_supporting = []
    supporting = [str(s) for s in raw_supporting[:2]]
    return MomentSelection(
        headline_moment_id=headline,
        headline_reason=reason,
        supporting_moment_ids=supporting,
    )


def _call_claude(system: str, user: str, model: str) -> str:
    from anthropic import Anthropic  # type: ignore
    from anthropic.types import TextBlock  # type: ignore

    client = Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in response.content if isinstance(block, TextBlock)
    )


def _call_openai(system: str, user: str, model: str) -> str:
    from openai import OpenAI  # type: ignore

    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


def _invalid_ids(selection: MomentSelection, valid_ids: set[str]) -> list[str]:
    """Return moment_ids referenced by the selection that are NOT in
    valid_ids. Order is preserved (headline first, then supporting in
    the order the model returned them) so the re-prompt error message
    reads naturally; duplicates are de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for mid in (selection.headline_moment_id, *selection.supporting_moment_ids):
        if mid not in valid_ids and mid not in seen:
            out.append(mid)
            seen.add(mid)
    return out


def _retry_user_prompt(original: str, invalid: list[str]) -> str:
    """Build the user-turn prompt for the single permitted re-prompt:
    repeat the candidate list, then name the offending ids and ask the
    model to retry using only ids from the candidate set. Naming the
    bad ids verbatim is what the spec means by 'the specific error'."""
    return (
        f"{original}\n\n"
        "Your previous response referenced moment_ids that are not in the "
        f"candidate set: {', '.join(invalid)}. Retry, returning only "
        "moment_ids from the candidate set above."
    )


def cheap_model_for(primary_provider: str) -> str:
    """Return the cheap-tier model id for the given primary provider.

    Defaults to anthropic's cheap tier when the provider name is
    unknown, matching spec section 12.2 where anthropic is the
    config default.
    """
    return PRIMARY_CHEAP_MODELS.get(primary_provider, PRIMARY_CHEAP_MODELS["anthropic"])


def select_moments(
    candidates: list[MomentCandidate],
    primary_provider: str = "anthropic",
    *,
    llm_caller: LLMCaller | None = None,
) -> MomentSelection | None:
    """Pick a headline + up to two supporting moments via one LLM call.

    Args:
        candidates: the week's emitted moments, already validated and
            redacted. Caller is responsible for filtering.
        primary_provider: "anthropic" or "openai". Determines which
            cheap-tier model is used (see PRIMARY_CHEAP_MODELS).
        llm_caller: optional injection for tests. When None, uses the
            real provider SDK matching primary_provider.

    Returns None when candidates is empty (no selector call is made).
    """
    if not candidates:
        return None

    model = cheap_model_for(primary_provider)
    system_prompt = _SYSTEM_PROMPT
    user_prompt = _build_user_prompt(candidates)
    valid_ids = {c.moment.moment_id for c in candidates}

    if llm_caller is None:
        if primary_provider == "openai":
            llm_caller = _call_openai
        else:
            llm_caller = _call_claude

    text = llm_caller(system_prompt, user_prompt, model)
    selection = _parse_selection(text)
    invalid = _invalid_ids(selection, valid_ids)
    if not invalid:
        return selection

    retry_user = _retry_user_prompt(user_prompt, invalid)
    retry_text = llm_caller(system_prompt, retry_user, model)
    retry_selection = _parse_selection(retry_text)
    retry_invalid = _invalid_ids(retry_selection, valid_ids)
    if retry_invalid:
        raise InvalidMomentSelectionError(
            "Selector returned moment_ids not in candidate set after retry: "
            f"{', '.join(retry_invalid)}"
        )
    return retry_selection


def _fallback_selection(candidates: list[MomentCandidate]) -> MomentSelection | None:
    """Pick the most-recent major moment, or the most-recent moderate
    if no major exists. Returns None when neither severity is present
    (caller should treat that as an unrecoverable protocol failure).

    Spec section 4.3.1: graceful degradation when the selector LLM
    fails to return ids in the candidate set after one retry. Recency
    is measured by session_started_at (when the conversation
    happened, not when the moment was emitted), matching the user's
    mental model of "the slip I most recently made". Supporting
    moment list is empty because the deterministic fallback has no
    coaching judgment to spend on a second moment.
    """
    for severity in ("major", "moderate"):
        bucket = [c for c in candidates if c.moment.severity == severity]
        if bucket:
            chosen = max(bucket, key=lambda c: c.session_started_at)
            return MomentSelection(
                headline_moment_id=chosen.moment.moment_id,
                headline_reason="",
                supporting_moment_ids=[],
            )
    return None


def select_moments_with_fallback(
    candidates: list[MomentCandidate],
    primary_provider: str = "anthropic",
    *,
    llm_caller: LLMCaller | None = None,
) -> MomentSelection | None:
    """Wrap select_moments with the spec section 4.3.1 fallback path.

    On InvalidMomentSelectionError (LLM returned bad ids twice in a
    row), pick the most-recent major-severity moment, or the
    most-recent moderate if no major exists. supporting_moment_ids is
    left empty. If neither major nor moderate exists in the candidate
    set, the original InvalidMomentSelectionError is re-raised so the
    caller knows the digest is missing a headline.
    """
    try:
        return select_moments(
            candidates, primary_provider, llm_caller=llm_caller
        )
    except InvalidMomentSelectionError:
        fallback = _fallback_selection(candidates)
        if fallback is None:
            raise
        return fallback
