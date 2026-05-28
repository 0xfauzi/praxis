"""Detection helpers for tool-injected user-turn content.

A ``Role.USER`` turn in a JSONL transcript can be one of:

  - the human's actual prompt (what we want to score)
  - content the AI tool inserted to bootstrap the session
    (Codex's ``AGENTS.md`` preamble, Claude Code's ``<system-reminder>``
    wrappers, slash-command caveats, hook outputs, ...)

The behavioural-signal extractors in :mod:`praxis.behavior.signals`
were inflating engagement / atrophy counts by counting the second
category as if the user had written it (issue #4). Each scanner now
calls :func:`is_tool_injected_content` at parse time and tags the
resulting :class:`praxis.models.Turn` with ``tool_injected=True`` when
the entire content is synthesised by the tool. The signal extractors
then iterate ``session.user_authored_turns`` (which filters those out)
instead of ``session.user_turns``.

Detection is intentionally conservative: a turn that contains BOTH a
tool wrapper AND a real user prompt (e.g. a slash-command followed by
a question) is NOT marked tool_injected here; the regex extractors
will see the real prompt content too. We only flag turns where the
entire visible content is synthetic.
"""
from __future__ import annotations

import re


# Codex injects the project's AGENTS.md (and the CLI's own
# ``<INSTRUCTIONS>`` block) into the first user turn of every session.
# The whole block is the tool's content; the human's first real prompt
# arrives in a subsequent turn.
_CODEX_PREAMBLE_RE = re.compile(
    r"\A\s*(?:#\s*)?AGENTS\.md\b|"
    r"\A\s*<INSTRUCTIONS>\b",
    re.IGNORECASE,
)

# Claude Code wraps its own injections in known tag triples. A turn whose
# entire body is one of these wrappers carries no user prompt:
#   - ``<system-reminder>...</system-reminder>`` (out-of-band notices)
#   - ``<command-name>.../</command-args>`` (slash command runner)
#   - ``<local-command-caveat>...`` (``!`` shell exec caveat block)
#   - ``<local-command-stdout>...`` / ``<local-command-stderr>...``
#   - ``<user-prompt-submit-hook>...`` (PreSubmit hook output)
_CLAUDE_FULL_WRAPPER_RES = (
    re.compile(r"\A\s*<system-reminder>.*?</system-reminder>\s*\Z", re.DOTALL),
    re.compile(
        r"\A\s*<command-name>.*?</command-name>\s*"
        r"<command-message>.*?</command-message>\s*"
        r"<command-args>.*?</command-args>\s*\Z",
        re.DOTALL,
    ),
    re.compile(r"\A\s*<local-command-caveat>.*?</local-command-caveat>\s*\Z", re.DOTALL),
    re.compile(r"\A\s*<local-command-stdout>.*?</local-command-stdout>\s*\Z", re.DOTALL),
    re.compile(r"\A\s*<local-command-stderr>.*?</local-command-stderr>\s*\Z", re.DOTALL),
    re.compile(
        r"\A\s*<user-prompt-submit-hook>.*?</user-prompt-submit-hook>\s*\Z",
        re.DOTALL,
    ),
)


def is_tool_injected_content(content: str) -> bool:
    """Return True iff ``content`` is entirely tool-synthesised.

    Detection covers the two surfaces that produced the false positives
    in the issue-#4 e2e run:

      - Codex's AGENTS.md preamble (turn starts with ``# AGENTS.md`` or
        ``<INSTRUCTIONS>``).
      - Claude Code's ``<system-reminder>`` / ``<command-name>`` /
        ``<local-command-*>`` / ``<user-prompt-submit-hook>`` wrappers
        when they make up the entire turn body.

    Mixed-content turns (e.g. a slash-command wrapper followed by a real
    user question) return False -- the regex extractors will still see
    the user's content. Empty / whitespace-only turns return True (the
    user typed nothing).
    """
    if not content or not content.strip():
        return True
    if _CODEX_PREAMBLE_RE.match(content):
        return True
    for pat in _CLAUDE_FULL_WRAPPER_RES:
        if pat.match(content):
            return True
    return False


__all__ = ["is_tool_injected_content"]
