"""Tests for score aggregation.

Spec 9.6:
  - _blend(heuristic, None) returns heuristic unchanged
  - _blend(heuristic, judge) respects 0.3h + 0.7j
  - _weighted_overall of all-5s returns 5.0 within float epsilon
  - ProfileSnapshot.from_scores([]) returns valid empty snapshot
"""
from __future__ import annotations

from praxis.scoring.aggregate import (
    HEURISTIC_WEIGHT,
    JUDGE_WEIGHT,
    ProfileSnapshot,
    _blend,
    _weighted_overall,
)
from praxis.scoring.rubric import RUBRIC


def _all_keys(value: float) -> dict[str, float]:
    return {d.key: value for d in RUBRIC}


def test_blend_no_judge_returns_heuristic():
    h = _all_keys(3.0)
    result = _blend(h, None)
    assert result == h
    # Should be a copy, not the same dict.
    h["planning"] = 99.0
    assert result["planning"] == 3.0


def test_blend_with_judge_respects_weights():
    h = _all_keys(2.0)
    j = _all_keys(8.0)
    result = _blend(h, j)
    expected = HEURISTIC_WEIGHT * 2.0 + JUDGE_WEIGHT * 8.0
    for v in result.values():
        assert abs(v - expected) < 1e-9


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
