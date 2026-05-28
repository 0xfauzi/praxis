"""Codex CLI session scanner.

Codex CLI stores sessions as JSONL rollout files at:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl

Each line has a 'type' (session_meta, message, function_call, ...) and a
'payload' that varies by type.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from praxis.models import Provider, Role, Session, Turn
from praxis.scanners.base import BaseScanner
from praxis.scanners.preamble import is_tool_injected_content


def _real_model(value):
    """Return value unless it's a placeholder like '<synthetic>'."""
    if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
        return None
    return value


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class CodexScanner(BaseScanner):
    provider_name = "codex"

    def __init__(self, root: Path | None = None):
        # Honor (in order): explicit arg, PRAXIS_CODEX_HOME (sandbox override),
        # CODEX_HOME (official Codex CLI env var), then the default location.
        praxis_home = os.environ.get("PRAXIS_CODEX_HOME")
        codex_home = os.environ.get("CODEX_HOME")
        if root is not None:
            self.root = root
        elif praxis_home:
            self.root = Path(praxis_home) / "sessions"
        elif codex_home:
            self.root = Path(codex_home) / "sessions"
        else:
            self.root = Path.home() / ".codex" / "sessions"

    def discover(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        yield from self.root.rglob("rollout-*.jsonl")

    def parse(self, path: Path) -> Session | None:
        turns: list[Turn] = []
        session_id = path.stem
        started_at: datetime | None = None
        model_hint: str | None = None
        cwd_hint: str | None = None

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
                    payload = entry.get("payload", {}) or {}
                    ts = _parse_ts(entry.get("timestamp"))

                    if entry_type == "session_meta":
                        if model_hint is None:
                            model_hint = _real_model(payload.get("model"))
                        cwd_hint = payload.get("cwd") or cwd_hint
                        if started_at is None and ts is not None:
                            started_at = ts
                        continue

                    if entry_type == "turn_context":
                        # Newer Codex CLI emits the model on turn_context, not session_meta.
                        if model_hint is None:
                            model_hint = _real_model(payload.get("model"))
                        continue

                    # Newer Codex CLI wraps the real payload as a `response_item`
                    # with its own inner `type` (message, function_call, custom_tool_call, ...).
                    if entry_type == "response_item":
                        entry_type = payload.get("type")
                        # payload of response_item *is* the inner record itself.

                    # Codex emits typed events. The ones we care about:
                    #   - 'message' with role user/assistant
                    #   - 'function_call' / 'custom_tool_call' / 'tool_search_call' / 'web_search_call'
                    if entry_type == "message":
                        role_str = payload.get("role")
                        try:
                            role = Role(role_str)
                        except ValueError:
                            continue
                        content = payload.get("content")
                        text = ""
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = "\n".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") in {"text", "input_text", "output_text"}
                            )
                        if not text.strip():
                            continue
                        turns.append(
                            Turn(
                                role=role,
                                content=text,
                                timestamp=ts,
                                meta={"model": model_hint},
                                tool_injected=(
                                    role == Role.USER
                                    and is_tool_injected_content(text)
                                ),
                            )
                        )

                    elif entry_type in {
                        "function_call",
                        "custom_tool_call",
                        "tool_search_call",
                        "web_search_call",
                    }:
                        name = (
                            payload.get("name")
                            or payload.get("action", {}).get("type")
                            or entry_type
                        )
                        args = payload.get("arguments") or payload.get("input") or payload.get("action")
                        # Attach tool call info to the previous assistant turn if one exists,
                        # otherwise create a marker turn so the scorer sees agentic behavior.
                        if turns and turns[-1].role == Role.ASSISTANT:
                            turns[-1].tool_calls.append({"name": name, "arguments": args})
                        else:
                            turns.append(
                                Turn(
                                    role=Role.ASSISTANT,
                                    content=f"[tool_call:{name}]",
                                    timestamp=ts,
                                    tool_calls=[{"name": name, "arguments": args}],
                                )
                            )
        except OSError:
            return None

        if not turns:
            return None

        if started_at is None:
            started_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

        return Session(
            provider=Provider.CODEX,
            session_id=session_id,
            started_at=started_at,
            turns=turns,
            source_path=str(path),
            project_hint=cwd_hint,
            model_hint=model_hint,
        )
