"""Transcript compression for the LLM-judge path (spec section 9.2).

Compresses a Session into a compact text transcript that the judge sees.
The user's behavior is the thing we score, so user turns are preserved
verbatim. Later stories shrink the assistant noise:

- US-012 (this file): every user turn appears character-for-character,
  in order.
- US-013: assistant turns longer than 200 chars become
  ``first 200 chars + "...[+N more chars, M tool calls]"``.
- US-014: tool-result blocks are dropped; tool calls become
  ``name(args)`` with args trimmed to 80 chars.

Moment substring checks (spec section 4.4) verify against the full
Session in storage, not this output, so compression never weakens that
check.
"""
from __future__ import annotations

from praxis.models import Role, Session


def compress_transcript(session: Session) -> str:
    """Return a compressed text transcript of ``session``.

    Every user turn appears character-for-character; the order of user
    turns matches the input session.
    """
    blocks: list[str] = []
    for turn in session.turns:
        if turn.role == Role.USER:
            blocks.append(f"<user>\n{turn.content}\n</user>")
    return "\n".join(blocks)
