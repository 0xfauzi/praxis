"""Tests for the masthead commitment rollup (US-034).

Covers:
  - CommitmentRollup is None when no follow-up is on file for the week.
  - When a follow-up exists, the rollup captures display_text, target dim,
    per-week session counts, and per-dimension before/after means.
  - self_report tally is zero-filled when session_reflections is absent
    (the table only lands in a later schema migration).
  - The rollup is exposed on WeeklyRunSummary AND surfaced through both
    digest renderers via praxis/reports/adapter.py, so terminal and HTML
    digests receive the same rollup payload.
  - Renderers handle a None rollup without crashing (the masthead block
    is implemented in US-035/036; here we only assert the data shape is
    plumbed through cleanly).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from praxis.follow_up import FollowUp
from praxis.reports import digest_html as dh
from praxis.reports import digest_terminal as dt
from praxis.reports.adapter import build_html_digest, build_terminal_digest
from praxis.reports.commitment_rollup import (
    SELF_REPORT_KEYS,
    CommitmentRollup,
    build_commitment_rollup,
    fetch_self_report_tally,
)
from praxis.scoring.aggregate import ProfileSnapshot
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore


def _snapshot(dim_means: dict[str, float] | None = None) -> ProfileSnapshot:
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


def _follow_up(
    dim_key: str = "verification",
    commitment_text: str = "Ask 'list the tables this writes' before running migrations.",
    week_iso: str = "2026-W21",
) -> FollowUp:
    return FollowUp(
        week_iso=week_iso,
        dim_key=dim_key,
        commitment_text=commitment_text,
        target_metric="verification_rate",
        baseline_value=0.3,
        measured_value=None,
        outcome="pending",
    )


# ---- build_commitment_rollup --------------------------------------------


def test_rollup_is_none_without_follow_up():
    """Spec section 2: masthead's commitment block is omitted when there
    is no commitment to roll up. None signals that to the renderer."""
    assert (
        build_commitment_rollup(
            follow_up=None,
            snapshot=_snapshot(),
            prior_week_means=None,
            sessions_this_week=0,
            sessions_prior_week=0,
        )
        is None
    )


def test_rollup_captures_display_text_and_dim_from_follow_up():
    follow_up = _follow_up()
    rollup = build_commitment_rollup(
        follow_up=follow_up,
        snapshot=_snapshot(),
        prior_week_means=None,
        sessions_this_week=4,
        sessions_prior_week=2,
    )
    assert rollup is not None
    assert rollup.display_text == follow_up.commitment_text
    assert rollup.target_dim_key == "verification"


def test_rollup_captures_session_counts():
    rollup = build_commitment_rollup(
        follow_up=_follow_up(),
        snapshot=_snapshot(),
        prior_week_means=None,
        sessions_this_week=7,
        sessions_prior_week=3,
    )
    assert rollup is not None
    assert rollup.sessions_this_week == 7
    assert rollup.sessions_prior_week == 3


def test_rollup_dim_after_uses_current_week_means():
    """`dim_after` mirrors the snapshot's dimension means - the week's
    session_scores roll into the snapshot before the rollup is built."""
    snap = _snapshot({"verification": 6.2, "planning": 7.1})
    rollup = build_commitment_rollup(
        follow_up=_follow_up(),
        snapshot=snap,
        prior_week_means=None,
        sessions_this_week=1,
        sessions_prior_week=0,
    )
    assert rollup is not None
    assert rollup.dim_after["verification"] == pytest.approx(6.2)
    assert rollup.dim_after["planning"] == pytest.approx(7.1)


def test_rollup_dim_before_uses_prior_week_means_when_present():
    snap = _snapshot({"verification": 6.2})
    rollup = build_commitment_rollup(
        follow_up=_follow_up(),
        snapshot=snap,
        prior_week_means={"verification": 4.8, "planning": 6.0},
        sessions_this_week=1,
        sessions_prior_week=1,
    )
    assert rollup is not None
    assert rollup.dim_before["verification"] == pytest.approx(4.8)
    assert rollup.dim_before["planning"] == pytest.approx(6.0)


def test_rollup_dim_before_empty_without_prior_week_data():
    """First weekly run has no prior digest; dim_before stays empty so
    the renderer can show 'baseline forming' rather than a fake zero."""
    rollup = build_commitment_rollup(
        follow_up=_follow_up(),
        snapshot=_snapshot(),
        prior_week_means=None,
        sessions_this_week=1,
        sessions_prior_week=0,
    )
    assert rollup is not None
    assert rollup.dim_before == {}


def test_rollup_self_report_tally_zero_filled_by_default():
    """No reflections saved yet -> the masthead reads four zeros, not None."""
    rollup = build_commitment_rollup(
        follow_up=_follow_up(),
        snapshot=_snapshot(),
        prior_week_means=None,
        sessions_this_week=2,
        sessions_prior_week=0,
    )
    assert rollup is not None
    assert rollup.self_report_tally == {key: 0 for key in SELF_REPORT_KEYS}


def test_rollup_self_report_tally_normalizes_provided_counts():
    """The builder fills missing keys with zero and ignores unknown keys."""
    rollup = build_commitment_rollup(
        follow_up=_follow_up(),
        snapshot=_snapshot(),
        prior_week_means=None,
        sessions_this_week=4,
        sessions_prior_week=0,
        self_report_tally={"yes": 3, "skip": 1, "unknown_bucket": 99},
    )
    assert rollup is not None
    assert rollup.self_report_tally == {"yes": 3, "no": 0, "partial": 0, "skip": 1}


# ---- fetch_self_report_tally --------------------------------------------


def test_fetch_self_report_tally_returns_zeros_with_no_store():
    assert fetch_self_report_tally(None, "2026-W21") == {key: 0 for key in SELF_REPORT_KEYS}


def test_fetch_self_report_tally_returns_zeros_when_table_missing(tmp_home):
    """The session_reflections table lives behind a later migration. The
    masthead must still build a rollup when the table is absent: the
    tally degrades to all-zero instead of crashing with OperationalError."""
    store = ProfileStore()
    tally = fetch_self_report_tally(store, "2026-W21")
    assert tally == {key: 0 for key in SELF_REPORT_KEYS}


# ---- WeeklyRunSummary plumbing ------------------------------------------


@dataclass
class _StubSummary:
    """Minimum WeeklyRunSummary surface the adapter reads."""

    week_iso: str = "2026-W21"
    sessions: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    judge_results: dict = field(default_factory=dict)
    moments: list = field(default_factory=list)
    selection: object | None = None
    snapshot: ProfileSnapshot = field(default_factory=_snapshot)
    trajectory: object | None = None
    cost_total_usd: float | None = None
    cost_baseline_usd: float | None = None
    last_week_means: dict[str, float] | None = None
    commitment_rollup: CommitmentRollup | None = None


def test_weekly_run_summary_has_commitment_rollup_field():
    """The orchestrator's WeeklyRunSummary exposes the rollup as a field
    so consumers (adapter, CLI) read it without coupling to a getattr
    fallback path."""
    from praxis.orchestrator import WeeklyRunSummary

    field_names = {f.name for f in WeeklyRunSummary.__dataclass_fields__.values()}
    assert "commitment_rollup" in field_names


def test_terminal_digest_carries_commitment_rollup():
    rollup = CommitmentRollup(
        display_text="ask before running migrations",
        target_dim_key="verification",
        sessions_this_week=4,
        sessions_prior_week=2,
    )
    digest = build_terminal_digest(_StubSummary(commitment_rollup=rollup))
    assert isinstance(digest, dt.WeeklyDigest)
    assert digest.commitment_rollup is rollup


def test_terminal_digest_omits_rollup_when_summary_has_none():
    digest = build_terminal_digest(_StubSummary(commitment_rollup=None))
    assert digest.commitment_rollup is None


def test_html_digest_carries_commitment_rollup():
    rollup = CommitmentRollup(
        display_text="ask before running migrations",
        target_dim_key="verification",
        sessions_this_week=4,
        sessions_prior_week=2,
    )
    digest = build_html_digest(_StubSummary(commitment_rollup=rollup))
    assert isinstance(digest, dh.WeeklyDigest)
    assert digest.commitment_rollup is rollup


def test_html_digest_omits_rollup_when_summary_has_none():
    digest = build_html_digest(_StubSummary(commitment_rollup=None))
    assert digest.commitment_rollup is None


# ---- Renderer resilience (US-034 AC #3) ---------------------------------


def test_terminal_renderer_does_not_crash_with_none_rollup():
    """When CommitmentRollup is None the terminal renderer must still
    produce a complete digest. The masthead's commitment block belongs
    to US-035; this test only proves the data path is non-crashing."""
    digest = build_terminal_digest(_StubSummary(commitment_rollup=None))
    output = dt.render(digest)
    assert isinstance(output, str)
    assert "PRAXIS" in output


def test_html_renderer_does_not_crash_with_none_rollup():
    digest = build_html_digest(_StubSummary(commitment_rollup=None))
    output = dh.render(digest)
    assert isinstance(output, str)
    assert "<html" in output.lower() or "<!doctype" in output.lower()


def test_terminal_renderer_does_not_crash_with_real_rollup():
    rollup = CommitmentRollup(
        display_text="ask before running migrations",
        target_dim_key="verification",
        sessions_this_week=4,
        sessions_prior_week=2,
        dim_before={"verification": 4.8},
        dim_after={"verification": 6.2},
    )
    digest = build_terminal_digest(_StubSummary(commitment_rollup=rollup))
    output = dt.render(digest)
    assert isinstance(output, str)
    assert "PRAXIS" in output


def test_html_renderer_does_not_crash_with_real_rollup():
    rollup = CommitmentRollup(
        display_text="ask before running migrations",
        target_dim_key="verification",
        sessions_this_week=4,
        sessions_prior_week=2,
        dim_before={"verification": 4.8},
        dim_after={"verification": 6.2},
    )
    digest = build_html_digest(_StubSummary(commitment_rollup=rollup))
    output = dh.render(digest)
    assert isinstance(output, str)


# ---- _count_sessions_in_iso_week helper ---------------------------------


def test_count_sessions_in_iso_week_returns_zero_for_empty_store(tmp_home):
    """Helper used by run_weekly to populate sessions_prior_week. With
    no rows on file, the count is 0 (not None / not crashing)."""
    from praxis.orchestrator import _count_sessions_in_iso_week

    store = ProfileStore()
    assert _count_sessions_in_iso_week(store, "2026-W21") == 0


def test_count_sessions_in_iso_week_returns_zero_for_malformed_iso(tmp_home):
    """Bad ISO week strings are ignored rather than crashing the masthead
    build."""
    from praxis.orchestrator import _count_sessions_in_iso_week

    store = ProfileStore()
    assert _count_sessions_in_iso_week(store, "not-a-week") == 0
