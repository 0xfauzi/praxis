"""README sanity checks for the coach repositioning (US-043).

The user-facing positioning is "AI usage coach," not "scorecard." US-043
made the Commit / Cue / Reflect / Review loop the headline; this test
guards against regressions to the older scorecard framing and makes sure
the four loop verbs stay visible in the README.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"

LOOP_VERBS = ("Commit", "Cue", "Reflect", "Review")


@pytest.fixture(scope="module")
def readme_lines() -> list[str]:
    return README_PATH.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_readme_exists() -> None:
    assert README_PATH.is_file(), f"README.md is missing at {README_PATH}"


def test_readme_has_praxis_heading(readme_lines: list[str]) -> None:
    assert "# Praxis" in readme_lines, (
        "README must include the '# Praxis' top-level heading"
    )


def test_readme_has_loop_heading(readme_lines: list[str]) -> None:
    assert "## The loop" in readme_lines, (
        "README must include a '## The loop' section that frames the product"
    )


@pytest.mark.parametrize("verb", LOOP_VERBS)
def test_readme_mentions_loop_verb(readme_text: str, verb: str) -> None:
    assert verb in readme_text, (
        f"README must name the loop verb {verb!r}; the four-step loop is "
        "Commit / Cue / Reflect / Review."
    )


def test_readme_does_not_mention_scorecard(readme_text: str) -> None:
    assert "scorecard" not in readme_text.lower(), (
        "README must not contain the word 'scorecard' (case-insensitive); "
        "Praxis is positioned as an AI usage coach, not a scorecard."
    )
