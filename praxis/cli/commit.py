"""Suggestion engine and prompt renderer for ``praxis commit``.

The Commit step of the weekly coaching loop (Commit -> Cue -> Reflect ->
Review) asks the user to pick a single, concrete commitment for the
current ISO week. This module builds the ordered list of suggestions
that the CLI prints and accepts a selection against.

Priority order (US-020 AC #1):

  1) Current-week headline-moment.suggested_alternative, if any.
  2-3) The first drill from each of the two weakest dimensions, pulled
       from ``praxis.scoring.coach.FALLBACK_DRILLS``.
  + Keep last week's commitment, only when a still-open prior
    commitment exists (i.e. ``outcome == 'pending'``).
  + Write your own, always last.

Dedup (US-020 AC #2): when the headline drill is identical to the first
drill of a weakest dim, that dim is skipped entirely and the third slot
is filled by the next-weakest dim. The dedup is by drill text, not by
dim key, so an identical sentence never appears twice.

Empty-suggestion guarantee (US-020 AC #3): even when there is no
headline, no prior commitment, and no usable drills, the returned list
still contains the 'Write your own' option, so the CLI prompt is never
empty.

Free-text path (US-021): :func:`prompt_free_text` reads a single-line
commitment from stdin, trims it, and re-prompts on empty or oversize
input. The 280-character cap matches Twitter's limit and the per-spec
budget for SessionStart hook payloads / Copilot instruction files.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Literal

from praxis.follow_up import FollowUp, target_metric_for
from praxis.scoring.coach import drills_for_dim
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore


_RUBRIC_KEYS: frozenset[str] = frozenset(d.key for d in RUBRIC)
_FREE_TEXT_SENTINEL = "free_text"


MAX_COMMITMENT_CHARS = 280


def _default_input_reader(prompt: str, /) -> str:
    """Default reader for :func:`prompt_free_text` -- wraps builtin :func:`input`."""
    return input(prompt)


def _default_stderr_writer(msg: str, /) -> None:
    """Default sink for :func:`prompt_free_text` validation messages."""
    print(msg, file=sys.stderr)


SuggestionKind = Literal["headline", "drill", "keep_last", "free_text"]


@dataclass(frozen=True)
class CommitSuggestion:
    """One row of the commit-prompt menu.

    ``kind`` lets the CLI render different prefixes (e.g. ``k)`` for
    keep-last, ``w)`` for free-text) and gives downstream stories
    (US-022) a stable shape to record into ``user_chosen`` /
    ``display_text``.

    ``dim_key`` is set for ``headline`` and ``drill`` suggestions so
    callers can record which rubric dim the commitment targets; it is
    ``None`` for ``keep_last`` and ``free_text`` whose dim is recovered
    from the prior follow-up or chosen by the user later.
    """

    kind: SuggestionKind
    text: str
    dim_key: str | None = None


@dataclass(frozen=True)
class CommitContext:
    """The DB-resolved inputs to :func:`build_commit_suggestions`.

    Decoupled from the CLI handler so the suggestion logic can be tested
    purely (no DB, no I/O). ``weakest_dim_keys`` is ordered ascending by
    score: ``[weakest, second-weakest, third-weakest, ...]``. Pass at
    least three dim keys when known so the dedup path has a fallback.
    """

    headline_text: str | None
    headline_dim_key: str | None
    weakest_dim_keys: list[str]
    open_prior_commitment_text: str | None


def build_commit_suggestions(ctx: CommitContext) -> list[CommitSuggestion]:
    """Return the ordered prompt entries for ``praxis commit``.

    See module docstring for the priority order, dedup rule, and empty
    guarantee. Pure function: no I/O, no global state.
    """
    suggestions: list[CommitSuggestion] = []
    seen_texts: set[str] = set()
    max_dim_drills = 2

    if ctx.headline_text:
        suggestions.append(
            CommitSuggestion(
                kind="headline",
                text=ctx.headline_text,
                dim_key=ctx.headline_dim_key,
            )
        )
        seen_texts.add(ctx.headline_text)

    drills_added = 0
    for dim_key in ctx.weakest_dim_keys:
        if drills_added >= max_dim_drills:
            break
        drills = drills_for_dim(dim_key)
        if not drills:
            continue
        first_drill = drills[0]
        if first_drill in seen_texts:
            # Dedup against the headline: skip this dim entirely so the
            # slot can be filled by the next-weakest dim (US-020 AC #2).
            continue
        suggestions.append(
            CommitSuggestion(kind="drill", text=first_drill, dim_key=dim_key)
        )
        seen_texts.add(first_drill)
        drills_added += 1

    if ctx.open_prior_commitment_text:
        suggestions.append(
            CommitSuggestion(
                kind="keep_last",
                text=ctx.open_prior_commitment_text,
            )
        )

    # Always last so the menu never collapses to zero choices when the
    # snapshot/headline/prior data are all empty (US-020 AC #3).
    suggestions.append(
        CommitSuggestion(kind="free_text", text="Write your own")
    )
    return suggestions


def load_commit_context(store: ProfileStore, *, week_iso: str) -> CommitContext:
    """Resolve the inputs to :func:`build_commit_suggestions` from storage.

    Best-effort: any missing data degrades gracefully.
      - No current-week digest: no headline moment.
      - No digest anywhere: no weakest-dim drills.
      - No prior follow-up or prior outcome already closed: no keep-last.

    ``weakest_dim_keys`` is sorted ascending by ``dimension_means`` so
    the first entry is the weakest dim. All dims with a numeric score
    are included (not just the first two) so the dedup path in
    :func:`build_commit_suggestions` can fall back to the next-weakest
    dim when the headline already covers the first one.
    """
    headline_text: str | None = None
    headline_dim_key: str | None = None
    weakest_dim_keys: list[str] = []

    digest = store.load_weekly_digest(week_iso)
    if digest and digest.get("headline_moment_id"):
        moment = store.load_moment_by_id(digest["headline_moment_id"])
        if moment is not None:
            alt = moment.get("suggested_alternative")
            if alt:
                headline_text = str(alt)
                dim = moment.get("dim_key")
                headline_dim_key = str(dim) if dim else None

    snapshot_source = digest.get("snapshot") if digest else None
    if snapshot_source is None:
        latest_week = store.latest_weekly_digest_week()
        if latest_week:
            fallback = store.load_weekly_digest(latest_week)
            if fallback:
                snapshot_source = fallback.get("snapshot")

    if isinstance(snapshot_source, dict):
        dim_means = snapshot_source.get("dimension_means")
        if isinstance(dim_means, dict) and dim_means:
            weakest_dim_keys = [
                key
                for key, _ in sorted(dim_means.items(), key=lambda kv: kv[1])
            ]

    open_prior_text: str | None = None
    prior = store.latest_follow_up()
    if prior is not None and prior.outcome == "pending":
        open_prior_text = prior.commitment_text

    return CommitContext(
        headline_text=headline_text,
        headline_dim_key=headline_dim_key,
        weakest_dim_keys=weakest_dim_keys,
        open_prior_commitment_text=open_prior_text,
    )


def format_commit_prompt(suggestions: list[CommitSuggestion]) -> str:
    """Render the user-facing prompt for the given suggestion list.

    Suggestions of kind ``headline`` and ``drill`` are numbered 1..N in
    the order they appear; ``keep_last`` is labelled ``k)``; the final
    ``free_text`` entry is labelled ``w)``. The returned string ends
    with a single newline so the caller can ``print()`` it directly.
    """
    lines: list[str] = ["", "Pick a commitment for this week:", ""]
    numbered = [
        s for s in suggestions if s.kind in ("headline", "drill")
    ]
    for idx, s in enumerate(numbered, start=1):
        lines.append(f"  {idx}) {s.text}")
    keep = next((s for s in suggestions if s.kind == "keep_last"), None)
    free = next((s for s in suggestions if s.kind == "free_text"), None)
    if keep is not None or free is not None:
        lines.append("")
        if keep is not None:
            lines.append(f"  k) Keep last week's: \"{keep.text}\"")
        if free is not None:
            lines.append(f"  w) {free.text}")
    choice_keys = [str(i) for i in range(1, len(numbered) + 1)]
    if keep is not None:
        choice_keys.append("k")
    if free is not None:
        choice_keys.append("w")
    lines.append("")
    lines.append(f"Your choice [{', '.join(choice_keys)}]:")
    return "\n".join(lines) + "\n"


def resolve_choice(
    raw: str, suggestions: list[CommitSuggestion]
) -> CommitSuggestion | None:
    """Map the user's typed choice to a :class:`CommitSuggestion`.

    Accepts (case-insensitive, whitespace-trimmed):
      - ``'1'``..``'9'`` -> the Nth headline/drill in priority order.
      - ``'k'`` -> the keep-last entry, if present.
      - ``'w'`` -> the free-text placeholder; the caller is responsible for
        opening :func:`prompt_free_text` to capture the actual text.

    Returns ``None`` for any unrecognized or out-of-range input so the
    caller can decide whether to re-prompt or exit silently.
    """
    normalised = raw.strip().lower()
    if normalised == "w":
        return next((s for s in suggestions if s.kind == "free_text"), None)
    if normalised == "k":
        return next((s for s in suggestions if s.kind == "keep_last"), None)
    if normalised.isdigit():
        idx = int(normalised)
        numbered = [s for s in suggestions if s.kind in ("headline", "drill")]
        if 1 <= idx <= len(numbered):
            return numbered[idx - 1]
    return None


def build_user_chosen_follow_up(
    *,
    week_iso: str,
    suggestion: CommitSuggestion,
    display_text: str,
    prior: FollowUp | None,
) -> FollowUp:
    """Construct the :class:`FollowUp` to persist for a user-chosen commitment.

    Always sets ``user_chosen=1``, ``outcome='pending'``, ``measured_value=None``,
    and ``display_text`` to the verbatim user-facing string. The remaining
    fields depend on the suggestion's kind:

      - ``headline`` / ``drill``: ``dim_key`` is the suggestion's rubric dim;
        ``target_metric`` is derived via :func:`target_metric_for`. Baseline
        defaults to ``0.0`` because :func:`compute_baseline_value` needs a
        snapshot + signals that ``cmd_commit`` does not load. The next-week
        close path can either refuse to score ``user_chosen`` rows or use
        the live signal value as both baseline and measured.
      - ``keep_last``: reuses the prior follow-up's ``dim_key`` /
        ``target_metric`` / ``baseline_value`` so the original baseline is
        preserved across the keep-replace pivot.
      - ``free_text`` (or any unknown dim): a ``free_text`` sentinel is
        written into ``dim_key`` / ``target_metric``. The columns are
        ``NOT NULL`` so we need a non-empty placeholder; the next-week close
        path inspects ``target_metric`` and skips sentinel rows.
    """
    if suggestion.kind == "keep_last" and prior is not None:
        return FollowUp(
            week_iso=week_iso,
            dim_key=prior.dim_key,
            commitment_text=display_text,
            target_metric=prior.target_metric,
            baseline_value=prior.baseline_value,
            measured_value=None,
            outcome="pending",
            user_chosen=1,
            display_text=display_text,
        )
    dim_key = suggestion.dim_key
    if (
        suggestion.kind in ("headline", "drill")
        and dim_key is not None
        and dim_key in _RUBRIC_KEYS
    ):
        return FollowUp(
            week_iso=week_iso,
            dim_key=dim_key,
            commitment_text=display_text,
            target_metric=target_metric_for(dim_key),
            baseline_value=0.0,
            measured_value=None,
            outcome="pending",
            user_chosen=1,
            display_text=display_text,
        )
    return FollowUp(
        week_iso=week_iso,
        dim_key=_FREE_TEXT_SENTINEL,
        commitment_text=display_text,
        target_metric=_FREE_TEXT_SENTINEL,
        baseline_value=0.0,
        measured_value=None,
        outcome="pending",
        user_chosen=1,
        display_text=display_text,
    )


def prompt_free_text(
    *,
    prompt_message: str = "> ",
    input_fn: Callable[[str], str] | None = None,
    error_writer: Callable[[str], None] | None = None,
) -> str:
    """Read a single-line free-text commitment, validated and re-prompted.

    Reads a line via ``input_fn`` (default :func:`input`), strips
    leading/trailing whitespace, and validates:

      - Whitespace-only / empty after trim -> ``error_writer`` is called
        with ``'Cannot be empty.'`` and the loop reads again.
      - Trimmed length above :data:`MAX_COMMITMENT_CHARS` (280) ->
        ``error_writer`` is called with
        ``'Keep it under 280 characters (current: <N>).'`` (where ``N``
        is the trimmed length) and the loop reads again.

    The loop continues until the user enters a valid line or aborts.
    :class:`KeyboardInterrupt` (Ctrl-C) and :class:`EOFError` (Ctrl-D /
    closed stdin) propagate to the caller so the CLI can decide how to
    react -- typically by exiting 0 without persisting anything.

    ``error_writer`` defaults to writing one line to ``sys.stderr``.
    Both ``input_fn`` and ``error_writer`` are injectable so unit tests
    can drive the loop deterministically without touching real
    stdin/stderr.
    """
    read: Callable[[str], str] = (
        input_fn if input_fn is not None else _default_input_reader
    )
    write_error: Callable[[str], None] = (
        error_writer if error_writer is not None else _default_stderr_writer
    )
    while True:
        raw = read(prompt_message)
        trimmed = raw.strip()
        if not trimmed:
            write_error("Cannot be empty.")
            continue
        if len(trimmed) > MAX_COMMITMENT_CHARS:
            write_error(
                f"Keep it under {MAX_COMMITMENT_CHARS} characters "
                f"(current: {len(trimmed)})."
            )
            continue
        return trimmed
