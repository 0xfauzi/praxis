"""Tests for score aggregation.

The heuristic decision path (US-012) has been removed: the LLM judge is
now the only source of dimension scores. These tests cover the remaining
shape of the pipeline.
"""

from __future__ import annotations

from praxis.scoring.aggregate import (
    ProfileSnapshot,
    _weighted_overall,
)
from praxis.scoring.rubric import RUBRIC


def _all_keys(value: float) -> dict[str, float]:
    return {d.key: value for d in RUBRIC}


def test_weighted_overall_of_fives_is_five():
    # All dimensions at 5.0 should produce an overall of 5.0 (sum of weights = 1.0).
    assert abs(_weighted_overall(_all_keys(5.0)) - 5.0) < 1e-9


def test_empty_profile_snapshot_is_valid():
    snap = ProfileSnapshot.from_scores([])
    assert snap.session_count == 0
    assert snap.overall == 0.0
    assert set(snap.dimension_means.keys()) == {d.key for d in RUBRIC}
    assert all(v == 0.0 for v in snap.dimension_means.values())
    assert snap.provider_breakdown == {}
