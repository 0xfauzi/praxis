"""Tests for the follow-up engine (US-045, US-046).

US-045 (already covered):
  - target_metric_for branches (verification_rate / delegation_rate / <dim>_dim_mean)
  - build_follow_up assembles the row correctly from a HeadlineMoment + snapshot
  - baseline_value reflects the right metric per branch
  - outcome defaults to 'pending' and measured_value defaults to None
  - ProfileStore.save_follow_up + load_follow_up roundtrip
  - one row per week_iso (re-saving the same week replaces, not duplicates)
  - the outcome CHECK constraint rejects unknown values

US-046 (closing the loop with data, not LLM):
  - compute_outcome thresholds, edge cases, and delegation_rate inversion
  - compute_outcome is a pure deterministic function (no LLM in the path)
  - close_follow_up populates measured_value + outcome without mutating input
  - ProfileStore.prior_follow_up returns the most recent earlier row
  - End-to-end: prior week's row is updated in place on the next weekly run
"""
from __future__ import annotations

import inspect
import sqlite3
from dataclasses import replace

import pytest

import praxis.follow_up as follow_up_module
from praxis.follow_up import (
    FollowUp,
    HeadlineMoment,
    build_follow_up,
    close_follow_up,
    compute_baseline_value,
    compute_outcome,
    target_metric_for,
)
from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore


def _empty_snapshot(dim_means: dict[str, float] | None = None) -> ProfileSnapshot:
    means = {d.key: 5.0 for d in RUBRIC}
    if dim_means:
        means.update(dim_means)
    return ProfileSnapshot(
        overall=5.0,
        dimension_means=means,
        session_count=4,
        provider_breakdown={"claude": 4},
        strongest_dimension="planning",
        weakest_dimension="verification",
    )


# ---- target_metric_for ---------------------------------------------------


def test_target_metric_for_verification_dim():
    assert target_metric_for("verification") == "verification_rate"


def test_target_metric_for_iteration_dim():
    assert target_metric_for("iteration") == "delegation_rate"


@pytest.mark.parametrize("dim_key", ["planning", "context", "tools", "fit"])
def test_target_metric_for_remaining_dims_falls_back_to_dim_mean(dim_key):
    assert target_metric_for(dim_key) == f"{dim_key}_dim_mean"


def test_target_metric_for_unknown_dim_raises():
    with pytest.raises(ValueError):
        target_metric_for("not_a_dim")


# ---- compute_baseline_value ---------------------------------------------


def test_compute_baseline_verification_rate_pulls_from_signals():
    snap = _empty_snapshot()
    assert (
        compute_baseline_value(
            "verification_rate", snap, verification_rate=0.42, delegation_rate=0.1
        )
        == 0.42
    )


def test_compute_baseline_delegation_rate_pulls_from_signals():
    snap = _empty_snapshot()
    assert (
        compute_baseline_value(
            "delegation_rate", snap, verification_rate=0.42, delegation_rate=0.31
        )
        == 0.31
    )


def test_compute_baseline_dim_mean_pulls_from_snapshot():
    snap = _empty_snapshot({"planning": 7.4})
    assert (
        compute_baseline_value(
            "planning_dim_mean", snap, verification_rate=0.0, delegation_rate=0.0
        )
        == 7.4
    )


def test_compute_baseline_unknown_metric_raises():
    snap = _empty_snapshot()
    with pytest.raises(ValueError):
        compute_baseline_value("mystery_metric", snap, 0.0, 0.0)


def test_compute_baseline_unknown_dim_in_dim_mean_raises():
    snap = _empty_snapshot()
    with pytest.raises(ValueError):
        compute_baseline_value("nope_dim_mean", snap, 0.0, 0.0)


# ---- build_follow_up ----------------------------------------------------


def test_build_follow_up_verification_moment_uses_verification_rate():
    moment = HeadlineMoment(
        dim_key="verification",
        suggested_alternative="ask 'list every table this migration writes' before running migrations",
    )
    snap = _empty_snapshot()
    fu = build_follow_up(
        week_iso="2026-W21",
        headline_moment=moment,
        snapshot=snap,
        verification_rate=0.5,
        delegation_rate=0.2,
    )
    assert fu.week_iso == "2026-W21"
    assert fu.dim_key == "verification"
    assert (
        fu.commitment_text
        == "ask 'list every table this migration writes' before running migrations"
    )
    assert fu.target_metric == "verification_rate"
    assert fu.baseline_value == 0.5
    assert fu.measured_value is None
    assert fu.outcome == "pending"


def test_build_follow_up_iteration_moment_uses_delegation_rate():
    moment = HeadlineMoment(
        dim_key="iteration",
        suggested_alternative="push back on the first draft before accepting",
    )
    snap = _empty_snapshot()
    fu = build_follow_up(
        week_iso="2026-W21",
        headline_moment=moment,
        snapshot=snap,
        verification_rate=0.5,
        delegation_rate=0.25,
    )
    assert fu.dim_key == "iteration"
    assert fu.target_metric == "delegation_rate"
    assert fu.baseline_value == 0.25
    assert fu.outcome == "pending"


def test_build_follow_up_planning_moment_uses_dim_mean():
    moment = HeadlineMoment(
        dim_key="planning",
        suggested_alternative="state goal + constraints before prompting",
    )
    snap = _empty_snapshot({"planning": 6.1})
    fu = build_follow_up(
        week_iso="2026-W21",
        headline_moment=moment,
        snapshot=snap,
        verification_rate=0.5,
        delegation_rate=0.2,
    )
    assert fu.dim_key == "planning"
    assert fu.target_metric == "planning_dim_mean"
    assert fu.baseline_value == 6.1
    assert fu.outcome == "pending"


def test_build_follow_up_commitment_text_is_moments_suggested_alternative():
    text = "always read the generated SQL before applying"
    moment = HeadlineMoment(dim_key="verification", suggested_alternative=text)
    fu = build_follow_up(
        week_iso="2026-W21",
        headline_moment=moment,
        snapshot=_empty_snapshot(),
        verification_rate=0.3,
        delegation_rate=0.4,
    )
    assert fu.commitment_text == text


# ---- ProfileStore.save_follow_up / load_follow_up -----------------------


def test_save_and_load_follow_up_roundtrip(tmp_home):
    store = ProfileStore()
    fu = FollowUp(
        week_iso="2026-W21",
        dim_key="verification",
        commitment_text="ask 'list every table this migration writes'",
        target_metric="verification_rate",
        baseline_value=0.42,
    )
    store.save_follow_up(fu)
    loaded = store.load_follow_up("2026-W21")
    assert loaded is not None
    assert loaded.week_iso == "2026-W21"
    assert loaded.dim_key == "verification"
    assert loaded.commitment_text == "ask 'list every table this migration writes'"
    assert loaded.target_metric == "verification_rate"
    assert loaded.baseline_value == 0.42
    assert loaded.measured_value is None
    assert loaded.outcome == "pending"


def test_load_follow_up_returns_none_when_absent(tmp_home):
    store = ProfileStore()
    assert store.load_follow_up("2025-W01") is None


def test_save_follow_up_is_idempotent_on_week_iso(tmp_home):
    """Two saves for the same week_iso must not produce two rows."""
    store = ProfileStore()
    fu1 = FollowUp(
        week_iso="2026-W21",
        dim_key="verification",
        commitment_text="first commitment",
        target_metric="verification_rate",
        baseline_value=0.4,
    )
    store.save_follow_up(fu1)
    fu2 = replace(fu1, commitment_text="second commitment", baseline_value=0.55)
    store.save_follow_up(fu2)

    # Read back the row directly to assert exactly one exists for this week.
    with sqlite3.connect(store.db_path) as conn:
        rows = list(conn.execute("SELECT * FROM follow_ups WHERE week_iso = ?", ("2026-W21",)))
    assert len(rows) == 1
    loaded = store.load_follow_up("2026-W21")
    assert loaded is not None
    assert loaded.commitment_text == "second commitment"
    assert loaded.baseline_value == 0.55


def test_outcome_check_constraint_rejects_unknown_value(tmp_home):
    """Spec section 14 CHECK constraint must reject outcomes outside the enum."""
    store = ProfileStore()
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "INSERT INTO follow_ups (week_iso, dim_key, commitment_text, "
                "target_metric, baseline_value, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-W22", "verification", "x", "verification_rate", 0.1, "bogus"),
            )


def test_two_distinct_weeks_can_coexist(tmp_home):
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="verification",
            commitment_text="week 21 commitment",
            target_metric="verification_rate",
            baseline_value=0.4,
        )
    )
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W22",
            dim_key="planning",
            commitment_text="week 22 commitment",
            target_metric="planning_dim_mean",
            baseline_value=6.0,
        )
    )
    assert store.load_follow_up("2026-W21") is not None
    assert store.load_follow_up("2026-W22") is not None


# ---- end-to-end: build then save ----------------------------------------


def test_build_then_save_writes_one_row_per_week(tmp_home):
    """The AC: each weekly digest run writes one follow_ups row keyed by week_iso."""
    store = ProfileStore()
    moment = HeadlineMoment(
        dim_key="verification",
        suggested_alternative="ask 'what tables does this migration touch?'",
    )
    fu = build_follow_up(
        week_iso="2026-W21",
        headline_moment=moment,
        snapshot=_empty_snapshot(),
        verification_rate=0.5,
        delegation_rate=0.2,
    )
    store.save_follow_up(fu)
    loaded = store.load_follow_up("2026-W21")
    assert loaded is not None
    assert loaded.dim_key == moment.dim_key
    assert loaded.commitment_text == moment.suggested_alternative
    assert loaded.target_metric == "verification_rate"
    assert loaded.baseline_value == 0.5
    assert loaded.measured_value is None
    assert loaded.outcome == "pending"


# =========================================================================
# US-046: compute outcome from data, never from the LLM
# =========================================================================


# ---- compute_outcome: higher-is-better metrics --------------------------


def test_compute_outcome_improved_when_measured_exceeds_baseline_plus_threshold():
    assert compute_outcome("verification_rate", baseline_value=0.4, measured_value=0.95) == "improved"


def test_compute_outcome_worse_when_measured_falls_below_baseline_minus_threshold():
    assert compute_outcome("verification_rate", baseline_value=0.95, measured_value=0.4) == "worse"


def test_compute_outcome_unchanged_inside_band():
    # Small movements stay inside the +/- 0.5 dead zone.
    assert compute_outcome("verification_rate", baseline_value=0.5, measured_value=0.6) == "unchanged"
    assert compute_outcome("verification_rate", baseline_value=0.5, measured_value=0.4) == "unchanged"


def test_compute_outcome_threshold_is_strict_above():
    """A delta of exactly +0.5 is NOT 'improved' (spec uses '>', not '>=')."""
    assert compute_outcome("verification_rate", baseline_value=0.0, measured_value=0.5) == "unchanged"


def test_compute_outcome_threshold_is_strict_below():
    """A delta of exactly -0.5 is NOT 'worse' (spec uses '<', not '<=')."""
    assert compute_outcome("verification_rate", baseline_value=0.5, measured_value=0.0) == "unchanged"


def test_compute_outcome_dim_mean_uses_same_higher_is_better_rule():
    assert compute_outcome("planning_dim_mean", baseline_value=6.0, measured_value=7.5) == "improved"
    assert compute_outcome("planning_dim_mean", baseline_value=7.5, measured_value=6.0) == "worse"
    assert compute_outcome("planning_dim_mean", baseline_value=6.0, measured_value=6.3) == "unchanged"


# ---- compute_outcome: delegation_rate (lower is better, inverted) -------


def test_compute_outcome_delegation_rate_dropping_is_improvement():
    """delegation_rate is an atrophy signal -- lower is better -- so a drop is improvement."""
    assert compute_outcome("delegation_rate", baseline_value=0.8, measured_value=0.2) == "improved"


def test_compute_outcome_delegation_rate_rising_is_worse():
    assert compute_outcome("delegation_rate", baseline_value=0.2, measured_value=0.8) == "worse"


def test_compute_outcome_delegation_rate_inside_band_unchanged():
    assert compute_outcome("delegation_rate", baseline_value=0.5, measured_value=0.4) == "unchanged"
    assert compute_outcome("delegation_rate", baseline_value=0.5, measured_value=0.6) == "unchanged"


def test_compute_outcome_delegation_rate_inverts_verification_rate_judgment():
    """Same numeric movement, opposite outcomes by metric direction."""
    # measured drops by 0.6 in both cases
    assert compute_outcome("verification_rate", baseline_value=0.8, measured_value=0.2) == "worse"
    assert compute_outcome("delegation_rate", baseline_value=0.8, measured_value=0.2) == "improved"
    # measured rises by 0.6 in both cases
    assert compute_outcome("verification_rate", baseline_value=0.2, measured_value=0.8) == "improved"
    assert compute_outcome("delegation_rate", baseline_value=0.2, measured_value=0.8) == "worse"


# ---- compute_outcome: purity / "no LLM" property ------------------------


def test_compute_outcome_is_deterministic():
    """Same inputs always produce the same outcome -- no randomness, no I/O."""
    args = ("verification_rate", 0.4, 0.95)
    assert {compute_outcome(*args) for _ in range(50)} == {"improved"}


def test_follow_up_module_does_not_reference_llm_judge():
    """US-046 AC: the LLM is never asked to decide the outcome.

    Enforced structurally: praxis.follow_up must not import or invoke the
    LLM judge or any provider SDK. If a future change wires an LLM into the
    outcome path, this test will fail and force a deliberate spec revisit
    (see spec sections 2, 6.3, 17.3).
    """
    src = inspect.getsource(follow_up_module)
    forbidden = [
        "score_session",        # praxis.scoring.judge entry point
        "praxis.scoring.judge",
        "praxis.scoring.coach",
        "anthropic",
        "Anthropic",
        "openai",
        "OpenAI",
    ]
    for token in forbidden:
        assert token not in src, (
            f"praxis.follow_up should not reference {token!r} "
            "(US-046 AC: outcome is computed from data, not the LLM)"
        )


# ---- close_follow_up ----------------------------------------------------


def test_close_follow_up_populates_measured_and_outcome_for_verification_rate():
    prior = FollowUp(
        week_iso="2026-W20",
        dim_key="verification",
        commitment_text="ask 'list every table this migration writes'",
        target_metric="verification_rate",
        baseline_value=0.4,
    )
    closed = close_follow_up(
        prior,
        snapshot=_empty_snapshot(),
        verification_rate=0.95,
        delegation_rate=0.1,
    )
    assert closed.measured_value == 0.95
    assert closed.outcome == "improved"


def test_close_follow_up_populates_measured_and_outcome_for_delegation_rate():
    prior = FollowUp(
        week_iso="2026-W20",
        dim_key="iteration",
        commitment_text="push back on the first draft",
        target_metric="delegation_rate",
        baseline_value=0.8,
    )
    # delegation_rate dropping from 0.8 to 0.2 -> "improved" (inverted rule)
    closed = close_follow_up(
        prior,
        snapshot=_empty_snapshot(),
        verification_rate=0.5,
        delegation_rate=0.2,
    )
    assert closed.measured_value == 0.2
    assert closed.outcome == "improved"


def test_close_follow_up_populates_measured_and_outcome_for_dim_mean():
    prior = FollowUp(
        week_iso="2026-W20",
        dim_key="planning",
        commitment_text="state goal + constraints before prompting",
        target_metric="planning_dim_mean",
        baseline_value=6.0,
    )
    snap = _empty_snapshot({"planning": 4.5})
    closed = close_follow_up(prior, snap, verification_rate=0.5, delegation_rate=0.2)
    assert closed.measured_value == 4.5
    assert closed.outcome == "worse"


def test_close_follow_up_marks_unchanged_for_small_movements():
    prior = FollowUp(
        week_iso="2026-W20",
        dim_key="planning",
        commitment_text="state goal + constraints",
        target_metric="planning_dim_mean",
        baseline_value=6.0,
    )
    snap = _empty_snapshot({"planning": 6.3})
    closed = close_follow_up(prior, snap, 0.5, 0.2)
    assert closed.outcome == "unchanged"
    assert closed.measured_value == 6.3


def test_close_follow_up_does_not_mutate_input():
    prior = FollowUp(
        week_iso="2026-W20",
        dim_key="verification",
        commitment_text="ask for source",
        target_metric="verification_rate",
        baseline_value=0.4,
    )
    closed = close_follow_up(prior, _empty_snapshot(), 0.95, 0.1)
    assert prior.measured_value is None
    assert prior.outcome == "pending"
    assert closed is not prior


def test_close_follow_up_preserves_identity_fields():
    prior = FollowUp(
        week_iso="2026-W20",
        dim_key="verification",
        commitment_text="ask 'list every table this migration writes'",
        target_metric="verification_rate",
        baseline_value=0.4,
    )
    closed = close_follow_up(prior, _empty_snapshot(), 0.95, 0.1)
    assert closed.week_iso == prior.week_iso
    assert closed.dim_key == prior.dim_key
    assert closed.commitment_text == prior.commitment_text
    assert closed.target_metric == prior.target_metric
    assert closed.baseline_value == prior.baseline_value


# ---- ProfileStore.prior_follow_up --------------------------------------


def test_prior_follow_up_returns_most_recent_earlier_row(tmp_home):
    store = ProfileStore()
    for week_iso in ["2026-W19", "2026-W20", "2026-W21"]:
        store.save_follow_up(
            FollowUp(
                week_iso=week_iso,
                dim_key="verification",
                commitment_text=f"commitment for {week_iso}",
                target_metric="verification_rate",
                baseline_value=0.4,
            )
        )
    prior = store.prior_follow_up("2026-W21")
    assert prior is not None
    assert prior.week_iso == "2026-W20"


def test_prior_follow_up_returns_none_when_no_earlier_row(tmp_home):
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="verification",
            commitment_text="only week",
            target_metric="verification_rate",
            baseline_value=0.4,
        )
    )
    assert store.prior_follow_up("2026-W21") is None


def test_prior_follow_up_returns_none_when_store_empty(tmp_home):
    store = ProfileStore()
    assert store.prior_follow_up("2026-W21") is None


def test_prior_follow_up_handles_year_boundary(tmp_home):
    """ISO week strings are zero-padded so lexical < matches chronological <."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2025-W52",
            dim_key="verification",
            commitment_text="end of 2025",
            target_metric="verification_rate",
            baseline_value=0.4,
        )
    )
    prior = store.prior_follow_up("2026-W01")
    assert prior is not None
    assert prior.week_iso == "2025-W52"


def test_prior_follow_up_skips_the_query_week_itself(tmp_home):
    """`before_week_iso` is strict: a row at the query week must not match."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W20",
            dim_key="verification",
            commitment_text="prior week",
            target_metric="verification_rate",
            baseline_value=0.4,
        )
    )
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="verification",
            commitment_text="current week",
            target_metric="verification_rate",
            baseline_value=0.4,
        )
    )
    prior = store.prior_follow_up("2026-W21")
    assert prior is not None
    assert prior.week_iso == "2026-W20"


# ---- end-to-end: close prior week and persist --------------------------


# ---- ProfileStore.latest_follow_up -------------------------------------


def test_latest_follow_up_returns_none_when_store_empty(tmp_home):
    store = ProfileStore()
    assert store.latest_follow_up() is None


def test_latest_follow_up_returns_single_row(tmp_home):
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W21",
            dim_key="verification",
            commitment_text="only week",
            target_metric="verification_rate",
            baseline_value=0.4,
        )
    )
    latest = store.latest_follow_up()
    assert latest is not None
    assert latest.week_iso == "2026-W21"


def test_latest_follow_up_returns_most_recent_by_week_iso(tmp_home):
    store = ProfileStore()
    for week_iso in ["2026-W19", "2026-W21", "2026-W20"]:  # out of insertion order
        store.save_follow_up(
            FollowUp(
                week_iso=week_iso,
                dim_key="verification",
                commitment_text=f"commitment for {week_iso}",
                target_metric="verification_rate",
                baseline_value=0.4,
            )
        )
    latest = store.latest_follow_up()
    assert latest is not None
    assert latest.week_iso == "2026-W21"


def test_latest_follow_up_handles_year_boundary(tmp_home):
    """Zero-padded ISO week strings sort chronologically under lexical DESC."""
    store = ProfileStore()
    store.save_follow_up(
        FollowUp(
            week_iso="2025-W52",
            dim_key="verification",
            commitment_text="end of 2025",
            target_metric="verification_rate",
            baseline_value=0.4,
        )
    )
    store.save_follow_up(
        FollowUp(
            week_iso="2026-W01",
            dim_key="verification",
            commitment_text="start of 2026",
            target_metric="verification_rate",
            baseline_value=0.5,
        )
    )
    latest = store.latest_follow_up()
    assert latest is not None
    assert latest.week_iso == "2026-W01"


def test_close_and_persist_updates_existing_row_in_place(tmp_home):
    """AC: on the next weekly run, the prior week's row is updated (not duplicated)."""
    store = ProfileStore()
    # Week N-1: persist a pending commitment.
    prior = FollowUp(
        week_iso="2026-W20",
        dim_key="verification",
        commitment_text="ask for source",
        target_metric="verification_rate",
        baseline_value=0.4,
    )
    store.save_follow_up(prior)

    # Week N: find the prior week's row, close it against this week's data, save.
    found = store.prior_follow_up("2026-W21")
    assert found is not None
    closed = close_follow_up(
        found, _empty_snapshot(), verification_rate=0.95, delegation_rate=0.1
    )
    store.save_follow_up(closed)

    reloaded = store.load_follow_up("2026-W20")
    assert reloaded is not None
    assert reloaded.measured_value == 0.95
    assert reloaded.outcome == "improved"
    # And exactly one row exists for that week (INSERT OR REPLACE -> update).
    with sqlite3.connect(store.db_path) as conn:
        rows = list(
            conn.execute("SELECT * FROM follow_ups WHERE week_iso = ?", ("2026-W20",))
        )
    assert len(rows) == 1


# =========================================================================
# US-022: user_chosen + display_text + 'superseded' Outcome
# =========================================================================


def test_followup_defaults_user_chosen_zero_and_display_text_none():
    """The new fields default so existing call sites keep working."""
    fu = FollowUp(
        week_iso="2026-W21",
        dim_key="verification",
        commitment_text="ask for source",
        target_metric="verification_rate",
        baseline_value=0.4,
    )
    assert fu.user_chosen == 0
    assert fu.display_text is None


def test_build_follow_up_accepts_user_chosen_and_display_text():
    """build_follow_up threads the new params straight through to FollowUp."""
    moment = HeadlineMoment(
        dim_key="planning",
        suggested_alternative="state goal + constraints before prompting",
    )
    fu = build_follow_up(
        week_iso="2026-W21",
        headline_moment=moment,
        snapshot=_empty_snapshot({"planning": 6.0}),
        verification_rate=0.5,
        delegation_rate=0.2,
        user_chosen=1,
        display_text="state goal + constraints before prompting",
    )
    assert fu.user_chosen == 1
    assert fu.display_text == "state goal + constraints before prompting"


def test_build_follow_up_defaults_user_chosen_zero_when_omitted():
    """Existing orchestrator callers should keep getting user_chosen=0."""
    moment = HeadlineMoment(
        dim_key="planning",
        suggested_alternative="state goal + constraints",
    )
    fu = build_follow_up(
        week_iso="2026-W21",
        headline_moment=moment,
        snapshot=_empty_snapshot({"planning": 6.0}),
        verification_rate=0.5,
        delegation_rate=0.2,
    )
    assert fu.user_chosen == 0
    assert fu.display_text is None


def test_outcome_literal_includes_superseded():
    """Outcome literal accepts the new 'superseded' value.

    Tested via dataclass construction with the literal string; mypy
    enforces the Literal type at typecheck time, while this runtime
    assertion guards against accidental removal.
    """
    fu = FollowUp(
        week_iso="2026-W21",
        dim_key="verification",
        commitment_text="ask for source",
        target_metric="verification_rate",
        baseline_value=0.4,
        outcome="superseded",
    )
    assert fu.outcome == "superseded"


def test_save_follow_up_persists_user_chosen_and_display_text(tmp_home):
    """save_follow_up writes both new columns and load_follow_up reads them back."""
    store = ProfileStore()
    fu = FollowUp(
        week_iso="2026-W21",
        dim_key="planning",
        commitment_text="state goal + constraints",
        target_metric="planning_dim_mean",
        baseline_value=6.0,
        outcome="pending",
        user_chosen=1,
        display_text="state goal + constraints",
    )
    store.save_follow_up(fu)
    loaded = store.load_follow_up("2026-W21")
    assert loaded is not None
    assert loaded.user_chosen == 1
    assert loaded.display_text == "state goal + constraints"