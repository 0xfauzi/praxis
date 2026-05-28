"""Tests for US-044: `praxis --help` reorganisation under Loop / More headings.

Acceptance criteria (paraphrased from prd.json):
  1. `praxis --help` lists commit, nudge, reflect, review, install-coach first
     under a 'Loop' (or 'Coaching') heading; the operational verbs (scan,
     re-score, ..., uninstall-shell-nudge) appear under a 'More' heading.
  2. `praxis week --help` exits non-zero (the verb was removed); `praxis
     review --help` succeeds and matches the new masthead documentation.
  3. An automated test captures stdout of `praxis --help` and asserts the
     ordering: 'commit' appears before 'scan' in the output text.

The tests go through ``main(argv)`` rather than shelling out to a subprocess
so they stay fast and remain valid even when the praxis console script is
not on PATH (e.g. when running from a fresh checkout without `pip install`).
"""
from __future__ import annotations

import argparse
import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from praxis.cli.__main__ import build_parser, main


LOOP_VERBS = ("commit", "nudge", "reflect", "review", "install-coach")
MORE_VERBS = (
    "scan",
    "re-score",
    "baseline",
    "history",
    "show",
    "report",
    "open",
    "last",
    "status",
    "rubric",
    "follow-up",
    "models",
    "config",
    "install-weekly",
    "uninstall-weekly",
    "shell-nudge",
    "install-shell-nudge",
    "uninstall-shell-nudge",
)


def _run_capture(argv: list[str]) -> tuple[str, str, int]:
    """Run ``main(argv)`` capturing stdout, stderr, and the exit code.

    Argparse raises ``SystemExit`` on ``--help`` and on parse errors;
    we normalise both into a returned int so each test asserts cleanly.
    """
    out, err = io.StringIO(), io.StringIO()
    code: int = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code) if exc.code is not None else 0
    return out.getvalue(), err.getvalue(), code


def _root_help() -> str:
    """`praxis --help` stdout as a single string."""
    out, _err, code = _run_capture(["--help"])
    assert code == 0, f"`praxis --help` should exit 0, got {code}"
    return out


# --- AC #1: Loop / More headings present and populated ---


def test_root_help_has_loop_heading():
    text = _root_help()
    # "Loop" OR "Coaching" satisfies AC #1; we picked "Loop" but the spec
    # explicitly allows either label.
    assert "Loop:" in text or "Coaching:" in text, (
        "Expected a 'Loop:' (or 'Coaching:') section heading in `praxis --help`"
    )


def test_root_help_has_more_heading():
    text = _root_help()
    assert "More:" in text


@pytest.mark.parametrize("verb", LOOP_VERBS)
def test_root_help_lists_each_loop_verb(verb: str):
    text = _root_help()
    assert verb in text, f"Loop verb {verb!r} missing from `praxis --help`"


@pytest.mark.parametrize("verb", MORE_VERBS)
def test_root_help_lists_each_more_verb(verb: str):
    text = _root_help()
    assert verb in text, f"More verb {verb!r} missing from `praxis --help`"


def test_loop_section_precedes_more_section():
    """Cosmetic ordering: the Loop heading should come before More."""
    text = _root_help()
    loop_idx = text.find("Loop:")
    more_idx = text.find("More:")
    assert loop_idx != -1 and more_idx != -1
    assert loop_idx < more_idx, (
        "Loop heading must appear before More heading in `praxis --help`"
    )


# --- AC #3: 'commit' appears before 'scan' in the help text ---


def test_commit_appears_before_scan_in_root_help():
    text = _root_help()
    commit_idx = text.find("commit")
    scan_idx = text.find("scan")
    assert commit_idx != -1 and scan_idx != -1
    assert commit_idx < scan_idx, (
        "AC US-044 #3: 'commit' must appear before 'scan' in "
        f"`praxis --help` output. Got commit@{commit_idx}, scan@{scan_idx}."
    )


# --- AC #2: `praxis week --help` exits non-zero, `praxis review --help` succeeds ---


def test_review_help_exits_zero():
    """`praxis review --help` must succeed (the renamed digest verb)."""
    _out, _err, code = _run_capture(["review", "--help"])
    assert code == 0


def test_review_help_describes_digest():
    """`praxis review --help` description aligns with the README masthead.

    README masthead: 'Render this week's digest -- what changed against last
    week's commitment.' We assert the help text mentions 'digest' so a
    drift between the README and the CLI surface fails loudly.
    """
    out, _err, _code = _run_capture(["review", "--help"])
    assert "digest" in out.lower(), (
        "`praxis review --help` should describe the digest (AC US-044 #2)"
    )


def test_review_help_mentions_loop_step():
    """The review masthead names the Review step of the loop explicitly."""
    out, _err, _code = _run_capture(["review", "--help"])
    # Either spelling is acceptable; both come from the same masthead text.
    text = out.lower()
    assert "review" in text and ("loop" in text or "commitment" in text), (
        "`praxis review --help` should reference the Commit -> Cue -> "
        "Reflect -> Review loop or the weekly commitment narrative."
    )


def test_week_help_exits_nonzero():
    """`praxis week --help` must exit non-zero (verb removed in US-033/US-044)."""
    _out, _err, code = _run_capture(["week", "--help"])
    assert code != 0, (
        "`praxis week --help` should exit non-zero now that `week` was "
        f"renamed to `review`. Got exit code {code}."
    )


# --- Structural guards: parser registration ---


def test_review_is_registered_subparser():
    parser = build_parser()
    args = parser.parse_args(["review"])
    assert args.cmd == "review"


def test_week_is_not_a_registered_subparser():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["week"])


@pytest.mark.parametrize("verb", LOOP_VERBS)
def test_each_loop_verb_parses(verb: str):
    parser = build_parser()
    args = parser.parse_args([verb])
    assert args.cmd == verb


# test_loop_stubs_exit_zero was removed at merge time: the
# readme-and-help-rewrite branch was developed against a tree where
# commit/nudge/reflect/install-coach were still stubs; in the merged
# tree those verbs are real commands (US-019..032) so the stub
# assertion no longer applies. Each verb has its own dedicated tests
# under tests/test_commit.py / test_cli.py / test_install_coach.py.


def test_help_epilog_has_no_suppress_marker():
    """Guard against argparse leaking the ==SUPPRESS== sentinel into help output.

    The grouping is achieved by suppressing the per-subparser help text and
    using a curated epilog. If the formatter ever regresses, the literal
    string ``==SUPPRESS==`` appears in the help output -- this test catches
    that regression so the user never sees it.
    """
    text = _root_help()
    assert "==SUPPRESS==" not in text


def test_help_does_not_advertise_week_as_a_command():
    """`week` must not appear in the curated epilog (AC #1 -- not in either list)."""
    text = _root_help()
    # Allow the word "week" to appear in other contexts (e.g. "weekly digest",
    # "past week"), but never as a standalone command-listing entry. The
    # epilog uses two-or-more-space-aligned columns, so look for "  week  ".
    assert "  week  " not in text, (
        "`week` should not be listed as a command in `praxis --help` after "
        "the US-044 rename to `review`."
    )


# --- mypy sanity: build_parser returns an ArgumentParser ---


def test_build_parser_returns_argument_parser():
    assert isinstance(build_parser(), argparse.ArgumentParser)
