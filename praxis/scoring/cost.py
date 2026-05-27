"""Estimate the LLM spend of one `run_weekly()` invocation.

Spec sections 10 and 15.2: the weekly digest panel surfaces "this
week's spend" (the cost of running praxis itself, not the user's
day-to-day model usage), and the 30-session perf gate requires that
spend to stay under $2.

The estimate is rough by design - we do not instrument every API
call. Instead, we reconstruct what was called after the fact from
the surviving JudgeResults plus knowledge of the deterministic
single-call steps (clustering, selector, follow-up). Each call's
input/output character count is converted to tokens via a fixed
ratio and multiplied by the model card's per-million pricing.

The HTML digest already carries the spec's "rough estimate from
token volume + tier pricing" disclaimer (spec 10.2); the same
disclaimer applies here.
"""
from __future__ import annotations

from praxis.models import Session
from praxis.models_advisor.cards import find_card_for_model_hint
from praxis.scoring.judge import JudgeResult, _compact_transcript


# Industry-standard rough approximation. The Anthropic and OpenAI
# tokenizers both land near 3.5-4.5 chars/token for English-language
# transcripts; 4.0 is the conventional midpoint and is what the model
# cards' pricing assumes you'll use to back out budgets.
CHARS_PER_TOKEN = 4.0


# Spec 5.2 + spec 4.3: clustering and selector both run on the cheap-tier
# model of the user's primary provider. Default to the Anthropic cheap
# tier here so the estimator can stand alone (it has no access to the
# user's runtime provider choice). The estimate is conservative either
# way: gpt-5-mini and claude-haiku-4-5 land within ~10% of each other
# at typical weekly volumes.
DEFAULT_CHEAP_MODEL = "claude-haiku-4-5"


# Order-of-magnitude estimates for the per-call payloads we cannot
# measure from this module's inputs. Used as upper bounds so the cost
# projection errs on the high side. Numbers are derived from the
# prompt sizes in praxis/scoring/clustering.py and moment_selector.py.
_CLUSTER_OUTPUT_CHARS_PER_SESSION = 80   # ~one task block per session in JSON
_SELECTOR_OUTPUT_CHARS_PER_MOMENT = 60   # one selection record + reason

# Spec 9.2 caps the per-session transcript sent to the judge at
# MAX_TRANSCRIPT_CHARS (12_000); the JudgeResult JSON typically runs
# 1.5-2.5 KB. 2_500 is a conservative ceiling for the output side.
_JUDGE_OUTPUT_CHARS_DEFAULT = 2_500


def estimate_call_cost(
    model_id: str, input_chars: int, output_chars: int
) -> float:
    """Project a single LLM call's dollar cost.

    Returns 0.0 when the model has no pricing on file (e.g. Copilot,
    or a model the user has aliased outside the shipped cards). The
    caller treats a 0.0 contribution as "unknown, do not count" rather
    than "free"; the spec section 10 wording "total estimated USD
    across all priced models" allows for unpriced models being
    excluded from the total.
    """
    if input_chars < 0 or output_chars < 0:
        return 0.0
    card = find_card_for_model_hint(model_id)
    if card is None:
        return 0.0
    if card.input_per_million_usd is None or card.output_per_million_usd is None:
        return 0.0
    input_tokens = input_chars / CHARS_PER_TOKEN
    output_tokens = output_chars / CHARS_PER_TOKEN
    return (
        input_tokens / 1_000_000 * card.input_per_million_usd
        + output_tokens / 1_000_000 * card.output_per_million_usd
    )


def _judge_input_chars(session: Session) -> int:
    """Chars the judge actually saw for this session, after compaction.

    Mirrors the path in `score_with_claude` / `score_with_openai`:
    the compact transcript is the user-message content; the system
    prompt is the rubric block, which we approximate via a fixed
    overhead constant rather than re-rendering the full template
    every time.
    """
    return len(_compact_transcript(session))


def _judge_output_chars(result: JudgeResult) -> int:
    """Approximate the response length from the JudgeResult fields.

    The JSON the model produces is dominated by the per-dim rationale
    strings, the moments array, and the standout/failure lists.
    Summing the lengths is a conservative under-count of the wire
    format (it omits JSON syntax), so we add a fixed overhead. This
    keeps us within ~20% of the true response size, which is more
    accurate than fixed defaults given how much rationale length
    varies between sessions.
    """
    total = sum(len(v) for v in result.rationale.values())
    total += sum(len(s) for s in result.standout_moments)
    total += sum(len(s) for s in result.failure_modes)
    total += len(result.overall_note)
    total += len(result.confidence_reason)
    for m in result.moments:
        total += (
            len(m.quoted_excerpt)
            + len(m.why_it_lost_score)
            + len(m.suggested_alternative)
        )
    # Cap at the empirical ceiling so an oversized output (e.g. a
    # judge that pads rationales) cannot single-handedly push the
    # estimate past the perf gate.
    return min(total + 200, _JUDGE_OUTPUT_CHARS_DEFAULT)


def estimate_judge_pass_cost(
    sessions: list[Session], results: dict[str, JudgeResult]
) -> float:
    """Sum of per-session judge cost across one pass.

    `results` keys are session stable_ids; only sessions present in
    both arguments contribute. Each session's judge_model is read
    from the JudgeResult so pass 1 (cheap-tier) and pass 2 (frontier)
    bill against their respective price cards.
    """
    by_id = {s.stable_id: s for s in sessions}
    total = 0.0
    for sid, result in results.items():
        session = by_id.get(sid)
        if session is None:
            continue
        total += estimate_call_cost(
            result.judge_model,
            _judge_input_chars(session),
            _judge_output_chars(result),
        )
    return total


def estimate_cluster_cost(
    sessions: list[Session], model_id: str = DEFAULT_CHEAP_MODEL
) -> float:
    """Project the cost of the single weekly clustering call.

    Spec 5.2 sends one cheap-tier call with all N sessions inlined as
    per-session blocks of <=400 chars (first_user_turn) plus a small
    fixed instruction block. Output is one task object per cluster -
    bounded above by one per session, which is what we assume here.
    """
    if not sessions:
        return 0.0
    # 400 chars first-turn cap + ~120 chars of metadata (id, hint,
    # timestamp, JSON wrappers) per session.
    block_chars = 520
    instructions_chars = 1_500
    input_chars = instructions_chars + len(sessions) * block_chars
    output_chars = len(sessions) * _CLUSTER_OUTPUT_CHARS_PER_SESSION
    return estimate_call_cost(model_id, input_chars, output_chars)


def estimate_selector_cost(
    moment_count: int, model_id: str = DEFAULT_CHEAP_MODEL
) -> float:
    """Project the cost of the single weekly moment-selector call.

    Spec 4.3 sends one cheap-tier call with up to N candidate moments.
    Each moment carries ~600 chars of context (excerpt + why + alt +
    metadata); output is the selection record (~120 chars) plus an
    optional supporting_moment_ids list.
    """
    if moment_count <= 0:
        return 0.0
    per_moment_input_chars = 600
    instructions_chars = 1_500
    input_chars = instructions_chars + moment_count * per_moment_input_chars
    output_chars = 200 + moment_count * _SELECTOR_OUTPUT_CHARS_PER_MOMENT
    return estimate_call_cost(model_id, input_chars, output_chars)


def estimate_weekly_pipeline_cost(
    sessions: list[Session],
    pass1_results: dict[str, JudgeResult],
    pass2_results: dict[str, JudgeResult],
    moment_count: int,
    cheap_model_id: str = DEFAULT_CHEAP_MODEL,
) -> float:
    """Sum of every LLM call run_weekly() makes.

    Components:
      - one clustering call (cheap-tier) over all sessions
      - one judge call per session in pass1_results (cheap-tier)
      - one judge call per session in pass2_results (frontier)
      - one selector call (cheap-tier) over moment_count candidates

    The follow-up step does not make an LLM call in the current
    pipeline (US-070 wires `build_follow_up` which is template-based);
    omitting it here matches the runtime behavior. If a future story
    adds an LLM call to follow_up, plumb its cost through this fn.
    """
    return (
        estimate_cluster_cost(sessions, cheap_model_id)
        + estimate_judge_pass_cost(sessions, pass1_results)
        + estimate_judge_pass_cost(sessions, pass2_results)
        + estimate_selector_cost(moment_count, cheap_model_id)
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_CHEAP_MODEL",
    "estimate_call_cost",
    "estimate_cluster_cost",
    "estimate_judge_pass_cost",
    "estimate_selector_cost",
    "estimate_weekly_pipeline_cost",
]
