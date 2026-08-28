"""Tests for ``praxis.scanners.preamble.is_tool_injected_content``.

Issue #4: behavioural-signal extractors were counting tool-synthesised
preambles (Codex AGENTS.md, Claude Code ``<system-reminder>`` wrappers,
slash-command caveats) against the user. The scanner now tags those
turns ``tool_injected=True`` and signal extractors iterate
``session.user_authored_turns`` to skip them.

These tests pin down exactly what counts as tool-injected and what
does not -- in particular, mixed-content turns (wrapper followed by a
real user prompt) must NOT be flagged.
"""

from __future__ import annotations

from praxis.scanners.preamble import is_tool_injected_content

# ----- positive cases: turns we expect to be flagged --------------------


def test_codex_agents_md_preamble_is_flagged():
    content = (
        "# AGENTS.md\n\nThis project follows these conventions...\n<INSTRUCTIONS>...</INSTRUCTIONS>"
    )
    assert is_tool_injected_content(content) is True


def test_codex_agents_md_preamble_lowercase_is_flagged():
    assert is_tool_injected_content("agents.md\n\ntext") is True


def test_codex_instructions_block_is_flagged():
    content = "<INSTRUCTIONS>do the thing</INSTRUCTIONS>\nmore text"
    assert is_tool_injected_content(content) is True


def test_claude_system_reminder_only_is_flagged():
    content = "<system-reminder>auto mode active</system-reminder>"
    assert is_tool_injected_content(content) is True


def test_claude_system_reminder_with_surrounding_whitespace_is_flagged():
    content = "   \n<system-reminder>hint</system-reminder>\n  "
    assert is_tool_injected_content(content) is True


def test_claude_command_name_block_is_flagged():
    content = (
        "<command-name>/foo</command-name>"
        "<command-message>foo</command-message>"
        "<command-args>bar</command-args>"
    )
    assert is_tool_injected_content(content) is True


def test_claude_local_command_caveat_is_flagged():
    content = "<local-command-caveat>caveat</local-command-caveat>"
    assert is_tool_injected_content(content) is True


def test_claude_local_command_stdout_is_flagged():
    content = "<local-command-stdout>hello</local-command-stdout>"
    assert is_tool_injected_content(content) is True


def test_claude_local_command_stderr_is_flagged():
    content = "<local-command-stderr>oops</local-command-stderr>"
    assert is_tool_injected_content(content) is True


def test_claude_user_prompt_submit_hook_is_flagged():
    content = "<user-prompt-submit-hook>hook output</user-prompt-submit-hook>"
    assert is_tool_injected_content(content) is True


def test_empty_content_is_flagged():
    assert is_tool_injected_content("") is True


def test_whitespace_only_content_is_flagged():
    assert is_tool_injected_content("   \n\t  ") is True


# ----- negative cases: real user content we must NOT flag ---------------


def test_plain_question_is_not_flagged():
    assert is_tool_injected_content("why does this fail?") is False


def test_long_prompt_is_not_flagged():
    content = (
        "Goal: refactor the auth module. Constraints: keep the public "
        "API stable and add migration notes. Plan first then implement."
    )
    assert is_tool_injected_content(content) is False


def test_content_mentioning_agents_md_inside_text_is_not_flagged():
    # The literal token "AGENTS.md" appears in the middle of a sentence,
    # not as the opening token. Should remain user content.
    content = "Please update AGENTS.md to mention the new linter."
    assert is_tool_injected_content(content) is False


def test_mixed_wrapper_then_real_prompt_is_not_flagged():
    # A slash-command wrapper followed by a real user question. The
    # regex extractors will still see the question, so we keep this
    # turn in user_authored_turns.
    content = (
        "<command-name>/help</command-name>"
        "<command-message>help</command-message>"
        "<command-args></command-args>\n"
        "Now show me how to debug a flaky test."
    )
    assert is_tool_injected_content(content) is False


def test_system_reminder_followed_by_real_prompt_is_not_flagged():
    content = (
        "<system-reminder>auto mode</system-reminder>\nWhat is the time complexity of quicksort?"
    )
    assert is_tool_injected_content(content) is False


def test_html_unrelated_to_wrappers_is_not_flagged():
    content = "<p>Some HTML I am pasting into the prompt.</p>"
    assert is_tool_injected_content(content) is False
