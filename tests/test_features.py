"""Tests for session feature extraction.

Features are pure data: turn counts, average user prompt length, and
marker hit counts. They are inputs/metadata only, not scores or labels.
"""

from __future__ import annotations

from datetime import UTC, datetime

from praxis.models import Provider, Role, Session, Turn
from praxis.scoring.features import SessionFeatures, extract


def _make_session(turns):
    return Session(
        provider=Provider.CLAUDE,
        session_id="t1",
        started_at=datetime.now(UTC),
        turns=turns,
        source_path="/tmp/t1",
    )


def test_features_dataclass_shape():
    # The acceptance criteria for US-013: SessionFeatures must expose
    # turn_count, avg_prompt_chars, and marker_hit_counts.
    fields = SessionFeatures.__dataclass_fields__
    assert set(fields.keys()) == {"turn_count", "avg_prompt_chars", "marker_hit_counts"}


def test_features_empty_session_returns_zeros():
    s = _make_session([])
    f = extract(s)
    assert f.turn_count == 0
    assert f.avg_prompt_chars == 0.0
    assert f.marker_hit_counts == {
        "planning": 0,
        "verification": 0,
        "iteration": 0,
        "pushback": 0,
    }


def test_features_counts_planning_marker_hits():
    s = _make_session(
        [
            Turn(role=Role.USER, content="Goal: build a parser. Constraints: minimal deps."),
        ]
    )
    f = extract(s)
    assert f.marker_hit_counts["planning"] >= 1


def test_features_counts_verification_marker_hits():
    s = _make_session(
        [
            Turn(
                role=Role.USER,
                content="Cite your sources for the claim about HTTP/3. Are you sure about that?",
            ),
        ]
    )
    f = extract(s)
    assert f.marker_hit_counts["verification"] >= 1


def test_features_turn_count_includes_all_roles():
    s = _make_session(
        [
            Turn(role=Role.USER, content="first"),
            Turn(role=Role.ASSISTANT, content="ok"),
            Turn(role=Role.USER, content="second"),
        ]
    )
    f = extract(s)
    assert f.turn_count == 3


def test_features_avg_prompt_chars_is_over_user_turns():
    # Two user turns of length 4 and 6 -> average 5.0. The assistant turn
    # in between must not pull the average down.
    s = _make_session(
        [
            Turn(role=Role.USER, content="abcd"),
            Turn(role=Role.ASSISTANT, content="x" * 100),
            Turn(role=Role.USER, content="abcdef"),
        ]
    )
    f = extract(s)
    assert f.avg_prompt_chars == 5.0
