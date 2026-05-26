"""Transcript compression for the LLM-judge path (spec section 9.2).

Compresses a Session into a compact text transcript that the judge sees.
The user's behavior is the thing we score, so user turns are preserved
verbatim. Later stories shrink the assistant noise:

- US-012: every user turn appears character-for-character, in order.
- US-013 (this file): assistant turns longer than 200 chars become
  ``first 200 chars + "...[+N more chars, M tool calls]"``.
- US-014: tool-result blocks are dropped; tool calls become
  ``name(args)`` with args trimmed to 80 chars.

Moment substring checks (spec section 4.4) verify against the full
Session in storage, not this output, so compression never weakens that
check.
"""
from __future__ import annotations

from praxis.models import Role, Session, Turn

ASSISTANT_TRUNCATE_LIMIT = 200


def _format_assistant(turn: Turn) -> str:
    content = turn.content
    if len(content) <= ASSISTANT_TRUNCATE_LIMIT:
        body = content
    else:
        extra = len(content) - ASSISTANT_TRUNCATE_LIMIT
        tool_calls = len(turn.tool_calls)
        body = (
            f"{content[:ASSISTANT_TRUNCATE_LIMIT]}"
            f"...[+{extra} more chars, {tool_calls} tool calls]"
        )
    return f"<assistant>\n{body}\n</assistant>"


def compress_transcript(session: Session) -> str:
    """Return a compressed text transcript of ``session``.

    Every user turn appears character-for-character; the order of user
    turns matches the input session. Assistant turns longer than 200
    chars are truncated to their first 200 chars followed by a summary
    marker `...[+N more chars, M tool calls]`; shorter assistant turns
    are preserved verbatim.
    """
    blocks: list[str] = []
    for turn in session.turns:
        if turn.role == Role.USER:
            blocks.append(f"<user>\n{turn.content}\n</user>")
        elif turn.role == Role.ASSISTANT:
            blocks.append(_format_assistant(turn))
    return "\n".join(blocks)
