"""Shared pytest fixtures.

We sandbox all on-disk state to `tmp_path` so tests cannot touch the
user's real `~/.claude/projects`, `~/.codex/sessions`, or
`~/.praxis`. The fixtures set both `Path.home()` (for the
default scanner paths) and the PRAXIS_* env vars (for code that
re-resolves them at runtime).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from praxis.models import Provider, Role, Session, Turn


@pytest.fixture(autouse=True)
def tmp_home(monkeypatch, tmp_path):
    """Redirect Path.home() and all PRAXIS_* env vars to a tmp dir.

    autouse: applies to EVERY test so a stray ``ProfileStore()`` (or any
    code that re-resolves ``resolve_home()`` with ``PRAXIS_HOME`` unset)
    can never touch the developer's real ``~/.praxis/profile.db``. Tests
    that need the sandbox path still request ``tmp_home`` by name and get
    this same instance's return value.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_CLAUDE_ROOT", str(tmp_path / ".claude" / "projects"))
    monkeypatch.setenv("PRAXIS_CODEX_HOME", str(tmp_path / ".codex"))
    # Make sure no real API keys leak in.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def synthetic_claude_session(tmp_home) -> Path:
    """Write a small synthetic Claude JSONL and return its path."""
    root = tmp_home / ".claude" / "projects" / "test-project"
    root.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    path = root / f"{session_id}.jsonl"
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    events = [
        {
            "type": "user",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": "Goal: refactor my auth code."},
        },
        {
            "type": "assistant",
            "timestamp": (when + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [
                    {"type": "text", "text": "Here's a plan..."},
                    {"type": "tool_use", "name": "Read", "input": {"path": "auth.py"}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": (when + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            "message": {
                "role": "user",
                "content": "Why does that approach work? Explain the security trade-off.",
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


@pytest.fixture
def synthetic_codex_session(tmp_home) -> Path:
    """Write a small synthetic Codex rollout JSONL and return its path."""
    when = datetime.now(timezone.utc) - timedelta(hours=2)
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
            "timestamp": (when + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            "payload": {"role": "user", "content": "write me a python function to parse json"},
        },
        {
            "type": "message",
            "timestamp": (when + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            "payload": {"role": "assistant", "content": [
                {"type": "output_text", "text": "Here's a function..."},
            ]},
        },
        {
            "type": "function_call",
            "timestamp": (when + timedelta(seconds=21)).isoformat().replace("+00:00", "Z"),
            "payload": {"name": "shell", "arguments": "{\"cmd\": \"pytest\"}"},
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


@pytest.fixture
def synthetic_session_object() -> Session:
    """Return a Session object directly (no file I/O)."""
    when = datetime.now(timezone.utc) - timedelta(hours=3)
    return Session(
        provider=Provider.CLAUDE,
        session_id="abc-123",
        started_at=when,
        turns=[
            Turn(role=Role.USER, content="Goal: improve test coverage. Constraints: keep CI under 5min."),
            Turn(role=Role.ASSISTANT, content="Plan: identify gaps then add tests."),
            Turn(role=Role.USER, content="Why does coverage matter most for the auth module?"),
            Turn(role=Role.ASSISTANT, content="Because auth bugs become security bugs..."),
        ],
        source_path="/tmp/abc-123.jsonl",
        model_hint="claude-opus-4-7",
    )
