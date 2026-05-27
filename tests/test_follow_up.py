"""Tests for the follow-up engine (US-045).

Covers:
  - target_metric_for branches (verification_rate / delegation_rate / <dim>_dim_mean)
  - build_follow_up assembles the row correctly from a HeadlineMoment + snapshot
  - baseline_value reflects the right metric per branch
  - outcome defaults to 'pending' and measured_value defaults to None
  - ProfileStore.save_follow_up + load_follow_up roundtrip
  - one row per week_iso (re-saving the same week replaces, not duplicates)
  - the outcome CHECK constraint rejects unknown values
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from praxis.follow_up import (
    FollowUp,
    HeadlineMoment,
    build_follow_up,
    compute_baseline_value,
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
