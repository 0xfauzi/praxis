"""Transcript compression for the LLM-judge path (spec section 9.2).

Compresses a Session into a compact text transcript that the judge sees.
The user's behavior is the thing we score, so user turns are preserved
verbatim. Later stories shrink the assistant noise:

- US-012: every user turn appears character-for-character, in order.
- US-013: assistant turns longer than 200 chars become
  ``first 200 chars + "...[+N more chars, M tool calls]"``.
- US-014 (this file): tool-result blocks are dropped; tool calls become
  ``name(args)`` with args trimmed to 80 chars.

Moment substring checks (spec section 4.4) verify against the full
Session in storage, not this output, so compression never weakens that
check.
"""

from __future__ import annotations

import json
from typing import Any

from praxis.models import Role, Session, Turn

ASSISTANT_TRUNCATE_LIMIT = 200
TOOL_ARGS_LIMIT = 80


def _stringify_args(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(args)


def _format_tool_call(tc: dict[str, Any]) -> str:
    name = tc.get("name") or "?"
    raw_args = tc.get("input")
    if raw_args is None:
        raw_args = tc.get("arguments")
    args_str = _stringify_args(raw_args)
    if len(args_str) > TOOL_ARGS_LIMIT:
        args_str = args_str[:TOOL_ARGS_LIMIT]
    return f"{name}({args_str})"


def _format_assistant(turn: Turn) -> str:
    content = turn.content
    if len(content) <= ASSISTANT_TRUNCATE_LIMIT:
        body = content
    else:
        extra = len(content) - ASSISTANT_TRUNCATE_LIMIT
        tool_calls = len(turn.tool_calls)
        body = (
            f"{content[:ASSISTANT_TRUNCATE_LIMIT]}...[+{extra} more chars, {tool_calls} tool calls]"
        )
    lines = [body]
    for tc in turn.tool_calls:
        lines.append(_format_tool_call(tc))
    inner = "\n".join(lines)
    return f"<assistant>\n{inner}\n</assistant>"


def compress_transcript(session: Session) -> str:
    """Return a compressed text transcript of ``session``.

    Every user turn appears character-for-character; the order of user
    turns matches the input session. Assistant turns longer than 200
    chars are truncated to their first 200 chars followed by a summary
    marker `...[+N more chars, M tool calls]`; shorter assistant turns
    are preserved verbatim. Each assistant tool call is rendered as
    ``name(args)`` with args trimmed to the first 80 chars and appended
    to the assistant block. Tool-result turns (``Role.TOOL``) are
    dropped entirely.
    """
    blocks: list[str] = []
    for turn in session.turns:
        if turn.role == Role.USER:
            blocks.append(f"<user>\n{turn.content}\n</user>")
        elif turn.role == Role.ASSISTANT:
            blocks.append(_format_assistant(turn))
    return "\n".join(blocks)
