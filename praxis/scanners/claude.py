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
from datetime import UTC, datetime
from pathlib import Path

from praxis.models import Provider, Role, Session, Turn
from praxis.scanners.base import BaseScanner
from praxis.scanners.preamble import is_tool_injected_content


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    # A timestamp with no 'Z' and no offset parses to a naive datetime.
    # Downstream week-window filters compare against aware UTC bounds, so
    # a naive value would raise "can't compare offset-naive and aware".
    # Treat a missing zone as UTC at the parse boundary.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


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

    # Harness-noise project-hint substrings. Claude Code names each project
    # directory after the cwd it was launched in; sessions launched from
    # tooling sandboxes carry those directory names as the project_hint.
    # Filtering by the directory name (not the full file path) keeps the
    # test fixtures - which legitimately live under pytest tmpdirs - while
    # excluding REAL sessions whose project_hint indicates a harness origin.
    _HARNESS_NOISE_HINT_SUBSTRINGS: tuple[str, ...] = (
        "ralph-worktrees",
        "ralph-phase",
        "ralph-sandbox",
        # pytest-of-<user> appears when claude code is launched inside a
        # pytest tmpdir during automated testing - the real session does
        # not live there in production usage.
        "pytest-of-",
    )

    def _is_harness_noise(self, path: Path) -> bool:
        # The hint is the immediate parent dir name (path.parent.name).
        # Some sessions are nested one more level (sub-agents), so also
        # check the grandparent.
        hint_candidates = [path.parent.name]
        gp = path.parent.parent
        if gp is not None:
            hint_candidates.append(gp.name)
        for hint in hint_candidates:
            if any(s in hint for s in self._HARNESS_NOISE_HINT_SUBSTRINGS):
                return True
        return False

    def discover(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        for path in self.root.rglob("*.jsonl"):
            if self._is_harness_noise(path):
                continue
            yield path

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
                    # A line that is valid JSON but not an object (a bare
                    # array/number/string/bool/null from a truncated or
                    # hand-edited file) would make entry.get(...) raise
                    # AttributeError, which is NOT a JSONDecodeError and
                    # would escape parse() and abort the whole scan.
                    if not isinstance(entry, dict):
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
                        # Only a real string model id is usable; a dict/number
                        # here would flow into cost estimation and rendering.
                        if isinstance(m, str) and m and not (m.startswith("<") and m.endswith(">")):
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
                            tool_injected=(role == Role.USER and is_tool_injected_content(text)),
                        )
                    )
        except OSError:
            return None

        if not turns:
            return None

        return Session(
            provider=Provider.CLAUDE,
            session_id=session_id,
            started_at=started_at or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            turns=turns,
            source_path=str(path),
            project_hint=project_hint,
            model_hint=model_hint,
        )
