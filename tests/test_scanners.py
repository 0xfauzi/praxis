"""Tests for the per-provider scanners.

Spec 5.6 acceptance criteria:
  - ClaudeScanner returns 0 sessions when ~/.claude/projects doesn't exist
  - ClaudeScanner parses mixed string/block-list content correctly
  - CodexScanner groups function_call events under the preceding assistant turn
  - CopilotScanner doesn't crash on malformed JSON or locked SQLite
  - All scanners filter out zero-turn files via base scan()
"""
from __future__ import annotations

from praxis.scanners import (
    ClaudeScanner,
    CodexScanner,
    CopilotScanner,
)


def test_claude_scanner_missing_root_returns_nothing(tmp_home):
    scanner = ClaudeScanner()
    # No projects dir created — should silently produce nothing.
    assert list(scanner.scan()) == []


def test_claude_scanner_parses_mixed_content(synthetic_claude_session, tmp_home):
    scanner = ClaudeScanner()
    sessions = list(scanner.scan())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.provider.value == "claude"
    assert s.model_hint == "claude-opus-4-7"
    # Three events; two user, one assistant with a tool_use block.
    assert len(s.user_turns) == 2
    assert len(s.assistant_turns) == 1
    assert "tool_use:Read" in s.assistant_turns[0].content
    assert s.assistant_turns[0].tool_calls
    assert s.assistant_turns[0].tool_calls[0]["name"] == "Read"


def test_claude_scanner_skips_malformed_lines(synthetic_claude_session, tmp_home):
    # Append a malformed line to the synthetic session.
    with synthetic_claude_session.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
        f.write("\n")  # blank line
    scanner = ClaudeScanner()
    sessions = list(scanner.scan())
    assert len(sessions) == 1
    # The garbage line shouldn't have prevented the rest from parsing.
    assert sessions[0].turn_count == 3


def test_codex_scanner_groups_function_calls(synthetic_codex_session, tmp_home):
    scanner = CodexScanner()
    sessions = list(scanner.scan())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.provider.value == "codex"
    assert s.model_hint == "gpt-5"
    # function_call event should have attached to the preceding assistant turn.
    assert s.assistant_turns
    assert s.assistant_turns[0].tool_calls
    assert s.assistant_turns[0].tool_calls[0]["name"] == "shell"


def test_codex_scanner_missing_root_returns_nothing(tmp_home):
    scanner = CodexScanner()
    assert list(scanner.scan()) == []


def test_claude_scanner_marks_system_reminder_turn_tool_injected(tmp_home):
    """Regression for issue #4.

    A user turn whose entire content is a Claude Code ``<system-reminder>``
    wrapper must be tagged ``tool_injected=True`` so the behavioural-signal
    extractors (which iterate ``session.user_authored_turns``) skip it.
    """
    import json
    import uuid
    from datetime import datetime, timezone

    root = tmp_home / ".claude" / "projects" / "preamble-project"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{uuid.uuid4()}.jsonl"
    when = datetime.now(timezone.utc)
    events = [
        {
            "type": "user",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "user",
                "content": "<system-reminder>auto mode active</system-reminder>",
            },
        },
        {
            "type": "user",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": "Now help me refactor auth.py"},
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    scanner = ClaudeScanner()
    sessions = list(scanner.scan())
    assert len(sessions) == 1
    s = sessions[0]
    assert len(s.user_turns) == 2
    assert s.user_turns[0].tool_injected is True
    assert s.user_turns[1].tool_injected is False
    # The authored-only view drops the wrapper turn.
    assert len(s.user_authored_turns) == 1
    assert s.user_authored_turns[0].content == "Now help me refactor auth.py"


def test_codex_scanner_marks_agents_md_preamble_tool_injected(tmp_home):
    """Regression for issue #4: Codex's AGENTS.md preamble must be flagged."""
    import json
    import uuid
    from datetime import datetime, timezone

    when = datetime.now(timezone.utc)
    day_dir = (
        tmp_home / ".codex" / "sessions"
        / f"{when.year:04d}" / f"{when.month:02d}" / f"{when.day:02d}"
    )
    day_dir.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex[:12]
    path = day_dir / f"rollout-{when.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
    events = [
        {
            "type": "session_meta",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "payload": {"model": "gpt-5", "cwd": "/tmp/repo"},
        },
        {
            "type": "message",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "payload": {
                "role": "user",
                "content": "# AGENTS.md\n\nProject conventions: use uv. Never use pip.",
            },
        },
        {
            "type": "message",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "payload": {"role": "user", "content": "Please add a flag to skip mocks."},
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    scanner = CodexScanner()
    sessions = list(scanner.scan())
    assert len(sessions) == 1
    s = sessions[0]
    assert len(s.user_turns) == 2
    assert s.user_turns[0].tool_injected is True
    assert s.user_turns[1].tool_injected is False
    assert len(s.user_authored_turns) == 1
    assert s.user_authored_turns[0].content.startswith("Please add a flag")


def test_copilot_scanner_handles_missing_dirs(tmp_home):
    # With no VS Code data, copilot scanner must not raise.
    scanner = CopilotScanner(roots=[])
    assert list(scanner.scan()) == []


def test_copilot_scanner_skips_malformed_json(tmp_home, tmp_path):
    user_dir = tmp_path / "Code" / "User"
    ws = user_dir / "workspaceStorage" / "abc"
    chat = ws / "chatSessions"
    chat.mkdir(parents=True, exist_ok=True)
    (chat / "session1.json").write_text("{not valid json", encoding="utf-8")
    scanner = CopilotScanner(roots=[user_dir])
    # Should silently skip the bad file, not raise.
    assert list(scanner.scan()) == []
