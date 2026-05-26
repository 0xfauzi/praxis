"""Claude Code session scanner.

Claude Code stores each conversation as a JSONL file at:
  ~/.claude/projects/<project-hash>/<session-uuid>.jsonl

Each line is one event. Relevant types include 'user' and 'assistant'
messages, plus tool_use / tool_result entries.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from praxis.models import Provider, Role, Session, Turn
from praxis.scanners.base import BaseScanner


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _extract_text(content: object) -> str:
    """Claude content is sometimes a string, sometimes a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "tool_use":
                # Represent tool calls as a compact line so the scorer can see them.
                name = block.get("name", "tool")
                parts.append(f"[tool_use:{name}]")
        return "\n".join(parts)
    return ""


class ClaudeScanner(BaseScanner):
    provider_name = "claude"

    def __init__(self, root: Path | None = None):
        if root is not None:
            self.root = root
        elif env_root := os.environ.get("PRAXIS_CLAUDE_ROOT"):
            self.root = Path(env_root)
        else:
            self.root = Path.home() / ".claude" / "projects"

    def discover(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        yield from self.root.rglob("*.jsonl")

    def parse(self, path: Path) -> Session | None:
        turns: list[Turn] = []
        session_id = path.stem
        started_at: datetime | None = None
        project_hint = path.parent.name
        model_hint: str | None = None

        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    entry_type = entry.get("type")
                    if entry_type not in {"user", "assistant"}:
                        continue

                    message = entry.get("message") or {}
                    role_str = message.get("role") or entry_type
                    try:
                        role = Role(role_str)
                    except ValueError:
                        continue

                    text = _extract_text(message.get("content", ""))
                    if not text:
                        continue

                    ts = _parse_ts(entry.get("timestamp"))
                    if started_at is None and ts is not None:
                        started_at = ts

                    if model_hint is None:
                        m = message.get("model")
                        if m and not (isinstance(m, str) and m.startswith("<") and m.endswith(">")):
                            model_hint = m

                    tool_calls: list[dict] = []
                    raw_content = message.get("content")
                    if isinstance(raw_content, list):
                        for block in raw_content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_calls.append(
                                    {"name": block.get("name"), "input": block.get("input")}
                                )

                    turns.append(
                        Turn(
                            role=role,
                            content=text,
                            timestamp=ts,
                            tool_calls=tool_calls,
                            meta={"model": message.get("model")},
                        )
                    )
        except OSError:
            return None

        if not turns:
            return None

        return Session(
            provider=Provider.CLAUDE,
            session_id=session_id,
            started_at=started_at or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            turns=turns,
            source_path=str(path),
            project_hint=project_hint,
            model_hint=model_hint,
        )
