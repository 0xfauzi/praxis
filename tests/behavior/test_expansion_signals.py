"""Fixture-driven tests for the US-005 expansion signals.

Each signal has 5 positive and 5 negative session fixtures under
tests/behavior/fixtures/<signal_name>/. Positive fixtures must produce
count >= 1; negative fixtures must produce count == 0. Empty input
also yields 0 (no false positives on blank transcripts).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from praxis.behavior.signals import extract
from praxis.models import Provider, Role, Session, Turn


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_session(fixture_path: Path) -> Session:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    turns = [
        Turn(role=Role(item["role"]), content=item["content"])
        for item in payload
    ]
    return Session(
        provider=Provider.CLAUDE,
        session_id=f"fixture-{fixture_path.stem}",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        turns=turns,
        source_path=str(fixture_path),
    )


def _fixture_paths(signal: str, polarity: str) -> list[Path]:
    paths = sorted((FIXTURES_DIR / signal).glob(f"{polarity}_*.json"))
    assert len(paths) == 5, (
        f"Expected 5 {polarity} fixtures for {signal}, found {len(paths)}"
    )
    return paths


@pytest.mark.parametrize(
    "signal,attribute",
    [
        ("specification_artifact", "specification_artifact_count"),
        ("error_naming", "error_naming_count"),
        ("iterative_refinement", "iterative_refinement_count"),
    ],
)
def test_positive_fixtures_fire(signal: str, attribute: str) -> None:
    for path in _fixture_paths(signal, "positive"):
        session = _load_session(path)
        sig = extract(session)
        count = getattr(sig, attribute)
        assert count >= 1, (
            f"Positive fixture {path.name} for {signal} produced {attribute}={count}"
        )


@pytest.mark.parametrize(
    "signal,attribute",
    [
        ("specification_artifact", "specification_artifact_count"),
        ("error_naming", "error_naming_count"),
        ("iterative_refinement", "iterative_refinement_count"),
    ],
)
def test_negative_fixtures_silent(signal: str, attribute: str) -> None:
    for path in _fixture_paths(signal, "negative"):
        session = _load_session(path)
        sig = extract(session)
        count = getattr(sig, attribute)
        assert count == 0, (
            f"Negative fixture {path.name} for {signal} produced {attribute}={count}"
        )


def test_empty_session_has_zero_new_counts() -> None:
    session = Session(
        provider=Provider.CLAUDE,
        session_id="empty",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        turns=[],
        source_path="/tmp/empty",
    )
    sig = extract(session)
    assert sig.specification_artifact_count == 0
    assert sig.error_naming_count == 0
    assert sig.iterative_refinement_count == 0


def test_whitespace_only_session_has_zero_new_counts() -> None:
    session = Session(
        provider=Provider.CLAUDE,
        session_id="ws",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        turns=[Turn(role=Role.USER, content="   \n\t  \n  ")],
        source_path="/tmp/ws",
    )
    sig = extract(session)
    assert sig.specification_artifact_count == 0
    assert sig.error_naming_count == 0
    assert sig.iterative_refinement_count == 0
