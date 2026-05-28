"""Tests for ``praxis commit`` (US-020, US-021).

Acceptance criteria exercised:

  US-020 AC #1: prints three numbered suggestions in priority order:
         headline drill first, then drills for the two weakest dims;
         the 'Keep last week' option appears only when a still-open
         prior commitment exists; 'Write your own' is always offered.

  US-020 AC #2: dedup -- when the headline drill is identical to the
         first drill from a weakest dim, that dim is skipped and the
         third slot is filled by the next-weakest dim instead.

  US-020 AC #3: empty-suggestion guarantee -- when there is no headline,
         no prior commitment, and no usable dim drills, the prompt
         still offers 'Write your own' and the CLI exits 0.

  US-021: 'Write your own' opens a single-line read; the input is
         trimmed and validated. Length > 280 re-prompts with
         'Keep it under 280 characters (current: <N>).'; empty (or
         whitespace-only) re-prompts with 'Cannot be empty.'.

The pure-function tests exercise ``build_commit_suggestions`` /
``prompt_free_text`` against deterministic inputs; the integration
tests drive the CLI entry point end-to-end (argparse + DB I/O).
"""
from __future__ import annotations

import builtins

import pytest

from praxis.cli.__main__ import main
from praxis.cli.commit import (
    MAX_COMMITMENT_CHARS,
    CommitContext,
    CommitSuggestion,
    build_commit_suggestions,
    format_commit_prompt,
    load_commit_context,
    prompt_free_text,
)
from praxis.follow_up import FollowUp
from praxis.models import Moment
from praxis.orchestrator import current_iso_week
from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.coach import FALLBACK_DRILLS, drills_for_dim
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore


# ---- build_commit_suggestions (pure) ---------------------------------------


def test_build_suggestions_in_priority_order_with_all_inputs():
    """Headline first, then drills for the two weakest dims, then keep-last, then free-text."""
    headline = "Before deploying, run a one-line check the migration is reversible."
    ctx = CommitContext(
        headline_text=headline,
        headline_dim_key="verification",
        weakest_dim_keys=["planning", "context", "iteration"],
        open_prior_commitment_text="Ask 'what would change your mind?' on every claim.",
    )
    result = build_commit_suggestions(ctx)

    assert [s.kind for s in result] == [
        "headline",
        "drill",
        "drill",
        "keep_last",
        "free_text",
    ]
    assert result[0].text == headline
    assert result[0].dim_key == "verification"
    # The drill slots pull the FIRST drill from each weakest dim.
    assert result[1].text == FALLBACK_DRILLS["planning"][0]
    assert result[1].dim_key == "planning"
    assert result[2].text == FALLBACK_DRILLS["context"][0]
    assert result[2].dim_key == "context"
    assert result[3].text == "Ask 'what would change your mind?' on every claim."
    assert result[3].dim_key is None
    assert result[4].text == "Write your own"


def test_build_suggestions_dedup_skips_dim_when_headline_matches_first_drill():
    """AC #2: headline duplicating a dim's top drill skips that dim entirely.

    The slot is filled by the next-weakest dim's first drill (not by the
    second drill within the same dim).
    """
    duplicate_headline = FALLBACK_DRILLS["planning"][0]
    ctx = CommitContext(
        headline_text=duplicate_headline,
        headline_dim_key="planning",
        weakest_dim_keys=["planning", "context", "iteration"],
        open_prior_commitment_text=None,
    )
    result = build_commit_suggestions(ctx)

    texts = [s.text for s in result if s.kind in ("headline", "drill")]
    # Exactly one copy of the headline drill, then two more drills from
    # the next-weakest dims -- never from "planning" again.
    assert texts.count(duplicate_headline) == 1
    drill_dims = [s.dim_key for s in result if s.kind == "drill"]
    assert "planning" not in drill_dims
    assert drill_dims == ["context", "iteration"]


def test_build_suggestions_no_keep_last_when_prior_outcome_not_pending():
    """The keep-last option is conditional on a still-open commitment."""
    ctx = CommitContext(
        headline_text="Pull-quote a counter-claim and respond to it.",
        headline_dim_key="iteration",
        weakest_dim_keys=["verification", "tools"],
        open_prior_commitment_text=None,
    )
    result = build_commit_suggestions(ctx)
    kinds = [s.kind for s in result]
    assert "keep_last" not in kinds
    assert kinds[-1] == "free_text"


def test_build_suggestions_no_headline_uses_two_dim_drills():
    """Without a headline, slots 1+2 are dim drills (the two weakest dims)."""
    ctx = CommitContext(
        headline_text=None,
        headline_dim_key=None,
        weakest_dim_keys=["verification", "tools", "fit"],
        open_prior_commitment_text=None,
    )
    result = build_commit_suggestions(ctx)
    kinds = [s.kind for s in result]
    assert kinds == ["drill", "drill", "free_text"]
    assert [s.dim_key for s in result if s.kind == "drill"] == [
        "verification",
        "tools",
    ]


def test_build_suggestions_empty_context_offers_only_free_text():
    """AC #3: even with no inputs at all, 'Write your own' is offered.

    No headline, no prior commitment, no dim drills -- the menu still has
    one entry so the CLI never crashes on an empty list.
    """
    ctx = CommitContext(
        headline_text=None,
        headline_dim_key=None,
        weakest_dim_keys=[],
        open_prior_commitment_text=None,
    )
    result = build_commit_suggestions(ctx)
    assert len(result) == 1
    assert result[0].kind == "free_text"
    assert result[0].text == "Write your own"


def test_build_suggestions_skips_dims_with_no_drills_in_bank():
    """Dims absent from FALLBACK_DRILLS are silently skipped (defensive)."""
    ctx = CommitContext(
        headline_text=None,
        headline_dim_key=None,
        # 'imaginary' is not a real rubric key and has no drills.
        weakest_dim_keys=["imaginary", "planning"],
        open_prior_commitment_text=None,
    )
    result = build_commit_suggestions(ctx)
    drills = [s for s in result if s.kind == "drill"]
    assert len(drills) == 1
    assert drills[0].dim_key == "planning"


def test_build_suggestions_caps_total_drill_slots_at_two_when_headline_present():
    """The combined headline+drills total is capped at 3 entries (AC #1)."""
    ctx = CommitContext(
        headline_text="Ask the assistant to rebut its own answer.",
        headline_dim_key="iteration",
        weakest_dim_keys=["planning", "context", "iteration", "tools"],
        open_prior_commitment_text=None,
    )
    result = build_commit_suggestions(ctx)
    numbered = [s for s in result if s.kind in ("headline", "drill")]
    assert len(numbered) == 3


# ---- drills_for_dim --------------------------------------------------------


def test_drills_for_dim_returns_copy_of_fallback_bank():
    """The accessor returns a fresh list so callers can mutate safely."""
    result = drills_for_dim("planning")
    assert result == FALLBACK_DRILLS["planning"]
    result.append("mutate-me")
    assert "mutate-me" not in FALLBACK_DRILLS["planning"]


def test_drills_for_dim_unknown_key_returns_empty_list():
    assert drills_for_dim("not-a-real-dim") == []


# ---- format_commit_prompt --------------------------------------------------


def test_format_prompt_numbers_first_drills_and_labels_keep_and_free():
    """Numbered entries get 1..N; keep-last uses 'k)', free-text uses 'w)'."""
    suggestions = [
        CommitSuggestion(kind="headline", text="alpha", dim_key="planning"),
        CommitSuggestion(kind="drill", text="beta", dim_key="context"),
        CommitSuggestion(kind="keep_last", text="gamma"),
        CommitSuggestion(kind="free_text", text="Write your own"),
    ]
    rendered = format_commit_prompt(suggestions)
    assert "1) alpha" in rendered
    assert "2) beta" in rendered
    assert 'k) Keep last week\'s: "gamma"' in rendered
    assert "w) Write your own" in rendered
    assert "[1, 2, k, w]" in rendered


def test_format_prompt_with_only_free_text_still_renders_w_choice():
    suggestions = [CommitSuggestion(kind="free_text", text="Write your own")]
    rendered = format_commit_prompt(suggestions)
    assert "w) Write your own" in rendered
    assert "[w]" in rendered


# ---- load_commit_context ---------------------------------------------------


def _snapshot_with_means(means: dict[str, float]) -> ProfileSnapshot:
    base = {d.key: 5.0 for d in RUBRIC}
    base.update(means)
    return ProfileSnapshot(
        overall=5.0,
        dimension_means=base,
        session_count=3,
        provider_breakdown={"claude": 3},
        strongest_dimension=max(base, key=lambda k: base[k]),
        weakest_dimension=min(base, key=lambda k: base[k]),
    )


def test_load_commit_context_returns_empty_when_db_is_empty(tmp_home):
    store = ProfileStore()
    ctx = load_commit_context(store, week_iso=current_iso_week())
    assert ctx.headline_text is None
    assert ctx.headline_dim_key is None
    assert ctx.weakest_dim_keys == []
    assert ctx.open_prior_commitment_text is None


def test_load_commit_context_pulls_headline_from_current_week_digest(tmp_home):
    """When a current-week digest exists with a moment, the headline is loaded."""
    store = ProfileStore()
    session_stable_id = "session-headline-test"
    moments = store.save_moments(
        session_stable_id,
        [
            Moment(
                dim_key="verification",
                turn_index=2,
                quoted_excerpt="Looks right.",
                why_it_lost_score="Did not verify before deploying.",
                suggested_alternative="Run the migration on a copy first.",
                severity="moderate",
            )
        ],
    )
    headline_moment_id = moments[0].moment_id
    week = current_iso_week()
    store.save_weekly_digest(
        week_iso=week,
        trajectory_label="learning",
        trajectory_headline="learning week",
        snapshot=_snapshot_with_means({"verification": 3.0, "context": 4.0}),
        headline_moment_id=headline_moment_id,
    )

    ctx = load_commit_context(store, week_iso=week)
    assert ctx.headline_text == "Run the migration on a copy first."
    assert ctx.headline_dim_key == "verification"
    # weakest_dim_keys is sorted ascending by dim_means.
    assert ctx.weakest_dim_keys[0] == "verification"
    assert ctx.weakest_dim_keys[1] == "context"


def test_load_commit_context_falls_back_to_latest_digest_for_dim_means(tmp_home):
    """No current-week digest -> use the latest digest for the dim ranking.

    Headline remains None (AC #1: 'current-week' headline only).
    """
    store = ProfileStore()
    store.save_weekly_digest(
        week_iso="2026-W19",
        trajectory_label="learning",
        trajectory_headline="prior week",
        snapshot=_snapshot_with_means({"planning": 2.0, "tools": 3.0}),
    )

    ctx = load_commit_context(store, week_iso="2026-W21")
    assert ctx.headline_text is None
    assert ctx.weakest_dim_keys[0] == "planning"
    assert ctx.weakest_dim_keys[1] == "tools"


def test_load_commit_context_keeps_last_only_when_prior_is_pending(tmp_home):
    """Closed follow-ups (improved/unchanged/worse) do not surface keep-last."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W20",
            dim_key="verification",
            commitment_text="ask 'what would change your mind?' on every claim",
            target_metric="verification_rate",
            baseline_value=0.30,
            measured_value=0.55,
            outcome="improved",
        )
    )
    ctx = load_commit_context(store, week_iso="2026-W21")
    assert ctx.open_prior_commitment_text is None


def test_load_commit_context_surfaces_pending_prior_commitment(tmp_home):
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W20",
            dim_key="verification",
            commitment_text="ask 'list every table this writes' before each migration",
            target_metric="verification_rate",
            baseline_value=0.30,
            outcome="pending",
        )
    )
    ctx = load_commit_context(store, week_iso="2026-W21")
    assert ctx.open_prior_commitment_text == (
        "ask 'list every table this writes' before each migration"
    )


# ---- end-to-end CLI integration --------------------------------------------


def test_cmd_commit_with_empty_db_prints_only_free_text(tmp_home, capsys):
    """AC #3: a fresh user with nothing in the DB still gets the prompt."""
    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Pick a commitment for this week" in out
    assert "w) Write your own" in out
    # No numbered drills, no keep-last.
    assert "1)" not in out
    assert "k)" not in out


def test_cmd_commit_prints_numbered_drills_when_digest_present(tmp_home, capsys):
    """When a current-week digest exists, the drills for its two weakest dims appear."""
    store = ProfileStore()
    store.save_weekly_digest(
        week_iso=current_iso_week(),
        trajectory_label="learning",
        trajectory_headline="learning week",
        snapshot=_snapshot_with_means({"planning": 2.0, "context": 3.0}),
    )

    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    assert "1) " + FALLBACK_DRILLS["planning"][0] in out
    assert "2) " + FALLBACK_DRILLS["context"][0] in out
    assert "w) Write your own" in out


def test_cmd_commit_renders_full_menu_with_headline_and_keep_last(tmp_home, capsys):
    """End-to-end: headline + 2 drills + keep-last + free-text."""
    store = ProfileStore()
    session_stable_id = "session-cli-commit"
    moments = store.save_moments(
        session_stable_id,
        [
            Moment(
                dim_key="iteration",
                turn_index=4,
                quoted_excerpt="OK, sounds good.",
                why_it_lost_score="Accepted a generic answer without pushback.",
                suggested_alternative="Reply: 'too generic - give me three concrete options'.",
                severity="moderate",
            )
        ],
    )
    week = current_iso_week()
    store.save_weekly_digest(
        week_iso=week,
        trajectory_label="learning",
        trajectory_headline="learning week",
        snapshot=_snapshot_with_means({"planning": 2.0, "context": 3.0}),
        headline_moment_id=moments[0].moment_id,
    )
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W20",
            dim_key="iteration",
            commitment_text="ask for three alternatives whenever the first reply is generic",
            target_metric="iteration_dim_mean",
            baseline_value=4.0,
            outcome="pending",
        )
    )

    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    assert "1) Reply: 'too generic" in out
    assert "2) " + FALLBACK_DRILLS["planning"][0] in out
    assert "3) " + FALLBACK_DRILLS["context"][0] in out
    assert (
        'k) Keep last week\'s: "ask for three alternatives whenever the first reply is generic"'
        in out
    )
    assert "w) Write your own" in out
    assert "[1, 2, 3, k, w]" in out


def test_cmd_commit_dedups_headline_against_weakest_dim_drill(tmp_home, capsys):
    """When the headline drill matches the weakest dim's first drill, the next-weakest dim fills the slot."""
    store = ProfileStore()
    duplicate = FALLBACK_DRILLS["planning"][0]
    session_stable_id = "session-dedup"
    moments = store.save_moments(
        session_stable_id,
        [
            Moment(
                dim_key="planning",
                turn_index=1,
                quoted_excerpt="Start coding.",
                why_it_lost_score="No plan; skipped scoping.",
                suggested_alternative=duplicate,
                severity="moderate",
            )
        ],
    )
    week = current_iso_week()
    store.save_weekly_digest(
        week_iso=week,
        trajectory_label="learning",
        trajectory_headline="learning week",
        snapshot=_snapshot_with_means(
            {"planning": 2.0, "context": 3.0, "iteration": 4.0}
        ),
        headline_moment_id=moments[0].moment_id,
    )

    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    # The headline drill appears exactly once (line 1).
    assert out.count(duplicate) == 1
    # Slot 2 is the next-weakest dim ('context'), NOT planning's second drill.
    assert "2) " + FALLBACK_DRILLS["context"][0] in out
    # Slot 3 is the next-after-that ('iteration').
    assert "3) " + FALLBACK_DRILLS["iteration"][0] in out


def test_cmd_commit_exit_code_is_zero(tmp_home, capsys):
    """The prompt-rendering verb always exits 0; capture is read to silence pytest warnings."""
    code = main(["commit"])
    capsys.readouterr()
    assert code == 0


def test_load_commit_context_handles_missing_moment_row(tmp_home):
    """A digest pointing at a missing moment_id degrades to no headline (defensive)."""
    store = ProfileStore()
    week = current_iso_week()
    store.save_weekly_digest(
        week_iso=week,
        trajectory_label="learning",
        trajectory_headline="learning week",
        snapshot=_snapshot_with_means({"planning": 2.0, "context": 3.0}),
        headline_moment_id="not-a-real-moment-id",
    )
    ctx = load_commit_context(store, week_iso=week)
    assert ctx.headline_text is None
    assert ctx.headline_dim_key is None
    # Dim ranking is still pulled from this digest's snapshot.
    assert ctx.weakest_dim_keys[0] == "planning"


def test_cmd_commit_uses_current_iso_week(monkeypatch, tmp_home, capsys):
    """The CLI hands the current ISO week to load_commit_context.

    A digest stored under the stubbed current week surfaces its drills;
    a digest stored under a different week would not (this assertion
    confirms the week selector is wired correctly).
    """
    from praxis.cli import __main__ as cli_main

    fixed = "2026-W21"
    monkeypatch.setattr(cli_main, "current_iso_week", lambda: fixed)
    store = ProfileStore()
    store.save_weekly_digest(
        week_iso=fixed,
        trajectory_label="learning",
        trajectory_headline="learning week",
        snapshot=_snapshot_with_means({"tools": 2.0, "fit": 3.0}),
    )

    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    assert "1) " + FALLBACK_DRILLS["tools"][0] in out


# ---- prompt_free_text (US-021) ---------------------------------------------


def _scripted_input(lines: list[str]):
    """Build a fake input() that returns successive ``lines`` per call.

    Calling more times than there are scripted lines raises EOFError,
    which mirrors how a closed stdin behaves and prevents an infinite
    loop on an unexpected re-prompt.
    """
    queue = list(lines)

    def fake_input(_prompt: str = "") -> str:
        if not queue:
            raise EOFError("scripted input exhausted")
        return queue.pop(0)

    return fake_input


def test_prompt_free_text_returns_trimmed_input_on_first_valid_entry():
    errors: list[str] = []
    result = prompt_free_text(
        input_fn=_scripted_input(["  Ask 'what would change your mind?'  "]),
        error_writer=errors.append,
    )
    assert result == "Ask 'what would change your mind?'"
    assert errors == []


def test_prompt_free_text_rejects_empty_input_with_cannot_be_empty():
    errors: list[str] = []
    result = prompt_free_text(
        input_fn=_scripted_input(["", "Pick a non-empty commitment."]),
        error_writer=errors.append,
    )
    assert errors == ["Cannot be empty."]
    assert result == "Pick a non-empty commitment."


def test_prompt_free_text_treats_whitespace_only_as_empty():
    """Tabs and spaces trip the empty check (whitespace-only is rejected)."""
    errors: list[str] = []
    result = prompt_free_text(
        input_fn=_scripted_input(["   \t  ", "Real commitment text."]),
        error_writer=errors.append,
    )
    assert errors == ["Cannot be empty."]
    assert result == "Real commitment text."


def test_prompt_free_text_rejects_over_280_chars_with_current_length():
    """The re-prompt message reports the trimmed length (the cap target)."""
    too_long = "x" * (MAX_COMMITMENT_CHARS + 5)  # 285 chars
    errors: list[str] = []
    result = prompt_free_text(
        input_fn=_scripted_input([too_long, "Short enough commitment."]),
        error_writer=errors.append,
    )
    assert errors == [
        f"Keep it under {MAX_COMMITMENT_CHARS} characters "
        f"(current: {MAX_COMMITMENT_CHARS + 5})."
    ]
    assert result == "Short enough commitment."


def test_prompt_free_text_accepts_input_at_exactly_280_chars():
    """The cap is INCLUSIVE: 280 chars exactly is valid, 281+ is not."""
    at_cap = "y" * MAX_COMMITMENT_CHARS
    errors: list[str] = []
    result = prompt_free_text(
        input_fn=_scripted_input([at_cap]),
        error_writer=errors.append,
    )
    assert result == at_cap
    assert errors == []


def test_prompt_free_text_loops_until_valid_input_arrives():
    """Multiple invalid attempts in a row are all re-prompted before accepting."""
    errors: list[str] = []
    too_long = "z" * (MAX_COMMITMENT_CHARS + 1)
    result = prompt_free_text(
        input_fn=_scripted_input([
            "",
            "   ",
            too_long,
            "Finally a good commitment.",
        ]),
        error_writer=errors.append,
    )
    assert errors == [
        "Cannot be empty.",
        "Cannot be empty.",
        f"Keep it under {MAX_COMMITMENT_CHARS} characters "
        f"(current: {MAX_COMMITMENT_CHARS + 1}).",
    ]
    assert result == "Finally a good commitment."


def test_prompt_free_text_propagates_keyboard_interrupt():
    """Ctrl-C aborts the loop -- the caller decides how to recover."""
    def raising_input(_prompt: str = "") -> str:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        prompt_free_text(input_fn=raising_input, error_writer=lambda _m: None)


def test_prompt_free_text_propagates_eof_error():
    """A closed stdin (Ctrl-D) bubbles up so the CLI can exit 0 cleanly."""
    def eof_input(_prompt: str = "") -> str:
        raise EOFError()

    with pytest.raises(EOFError):
        prompt_free_text(input_fn=eof_input, error_writer=lambda _m: None)


def test_prompt_free_text_default_error_writer_goes_to_stderr(capsys):
    """The default writer sends validation messages to stderr (not stdout)."""
    result = prompt_free_text(
        input_fn=_scripted_input(["", "valid commitment text"]),
    )
    captured = capsys.readouterr()
    assert result == "valid commitment text"
    assert "Cannot be empty." in captured.err
    assert "Cannot be empty." not in captured.out


# ---- cmd_commit free-text integration (US-021) ------------------------------


def _force_tty(monkeypatch) -> None:
    """Pretend stdin is a TTY so cmd_commit enters the interactive read."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


def test_cmd_commit_w_choice_reads_validated_free_text(monkeypatch, tmp_home, capsys):
    """End-to-end: typing 'w' then a valid commitment echoes the chosen text."""
    _force_tty(monkeypatch)
    inputs = iter(["w", "Ask 'list every table this writes' before each migration."])
    monkeypatch.setattr(builtins, "input", lambda *_a, **_kw: next(inputs))

    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    assert (
        '"Ask \'list every table this writes\' before each migration."' in out
    )


def test_cmd_commit_w_choice_reprompts_on_oversize_then_accepts(
    monkeypatch, tmp_home, capsys
):
    """Free-text > 280 chars triggers the cap message then re-reads."""
    _force_tty(monkeypatch)
    too_long = "a" * (MAX_COMMITMENT_CHARS + 1)
    inputs = iter(["w", too_long, "Short commitment."])
    monkeypatch.setattr(builtins, "input", lambda *_a, **_kw: next(inputs))

    code = main(["commit"])
    captured = capsys.readouterr()
    assert code == 0
    assert (
        f"Keep it under {MAX_COMMITMENT_CHARS} characters "
        f"(current: {MAX_COMMITMENT_CHARS + 1})." in captured.err
    )
    assert '"Short commitment."' in captured.out


def test_cmd_commit_w_choice_reprompts_on_empty_then_accepts(
    monkeypatch, tmp_home, capsys
):
    """Free-text empty/whitespace-only triggers 'Cannot be empty.' then re-reads."""
    _force_tty(monkeypatch)
    inputs = iter(["w", "   ", "Real commitment."])
    monkeypatch.setattr(builtins, "input", lambda *_a, **_kw: next(inputs))

    code = main(["commit"])
    captured = capsys.readouterr()
    assert code == 0
    assert "Cannot be empty." in captured.err
    assert '"Real commitment."' in captured.out


def test_cmd_commit_non_tty_skips_interactive_read(tmp_home, capsys):
    """Without a TTY, cmd_commit prints the prompt and returns 0 (no input call).

    This is the default path during pytest (stdin is not a TTY). The
    existing US-020 tests rely on it; this assertion pins the contract.
    """
    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    # The prompt rendered fully, including the choice line, but no
    # 'Write your own commitment' lead-in (which is only printed when
    # the user actually selects 'w').
    assert "Pick a commitment for this week" in out
    assert "Write your own commitment for this week." not in out


def test_cmd_commit_w_choice_eof_exits_zero_cleanly(monkeypatch, tmp_home, capsys):
    """Ctrl-D during the free-text read aborts without crashing."""
    _force_tty(monkeypatch)
    calls = iter(["w"])

    def eof_after_w(*_a, **_kw):
        # First call returns 'w'; subsequent calls (the free-text read)
        # raise EOFError to simulate Ctrl-D.
        try:
            return next(calls)
        except StopIteration:
            raise EOFError()

    monkeypatch.setattr(builtins, "input", eof_after_w)
    code = main(["commit"])
    capsys.readouterr()
    assert code == 0


def test_cmd_commit_w_choice_keyboard_interrupt_exits_zero(
    monkeypatch, tmp_home, capsys
):
    """Ctrl-C during the free-text read aborts without crashing."""
    _force_tty(monkeypatch)
    calls = iter(["w"])

    def ctrl_c_after_w(*_a, **_kw):
        try:
            return next(calls)
        except StopIteration:
            raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "input", ctrl_c_after_w)
    code = main(["commit"])
    capsys.readouterr()
    assert code == 0


def test_cmd_commit_non_w_choice_does_not_open_free_text(
    monkeypatch, tmp_home, capsys
):
    """Selecting '1' or 'k' does not trigger the free-text reader (US-022 will wire those)."""
    _force_tty(monkeypatch)
    inputs = iter(["1"])
    monkeypatch.setattr(builtins, "input", lambda *_a, **_kw: next(inputs))

    code = main(["commit"])
    out = capsys.readouterr().out
    assert code == 0
    # No free-text branch was entered.
    assert "Write your own commitment for this week." not in out
