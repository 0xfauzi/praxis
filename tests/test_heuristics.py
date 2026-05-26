"""Tests for heuristic feature extraction.

Spec 7.7:
  - Session with markers across all dimensions produces non-zero features
  - Zero-user-turns returns all zeros without raising
"""
from __future__ import annotations

from datetime import datetime, timezone

from praxis.models import Provider, Role, Session, Turn
from praxis.scoring.features import extract


def _make_session(turns):
    return Session(
        provider=Provider.CLAUDE,
        session_id="t1",
        started_at=datetime.now(timezone.utc),
        turns=turns,
        source_path="/tmp/t1",
    )


def test_heuristics_empty_session_returns_zeros():
    s = _make_session([])
    f = extract(s)
    assert f.user_turn_count == 0
    assert f.planning_density == 0.0
    assert f.avg_context_richness == 0.0
    assert f.has_multi_turn is False


def test_heuristics_detects_planning_marker():
    s = _make_session([
        Turn(role=Role.USER, content="Goal: build a parser. Constraints: minimal deps."),
    ])
    f = extract(s)
    assert f.planning_density > 0


def test_heuristics_detects_verification_markers():
    s = _make_session([
        Turn(
            role=Role.USER,
            content="Cite your sources for the claim about HTTP/3. Are you sure about that?",
        ),
    ])
    f = extract(s)
    assert f.verification_rate > 0


def test_heuristics_multi_turn_flag():
    s = _make_session([
        Turn(role=Role.USER, content="first"),
        Turn(role=Role.ASSISTANT, content="ok"),
        Turn(role=Role.USER, content="second"),
    ])
    f = extract(s)
    assert f.has_multi_turn is True
