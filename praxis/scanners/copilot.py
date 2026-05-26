"""GitHub Copilot Chat scanner.

Copilot chat history lives in VS Code's per-workspace storage. There are
two formats in the wild:

  1. ~/Library/Application Support/Code/User/workspaceStorage/<hash>/chatSessions/*.json
     (newer; one JSON per chat session)
  2. ~/Library/Application Support/Code/User/workspaceStorage/<hash>/state.vscdb
     (SQLite; older versions stored chat in the workspace key-value store)

We try (1) first, then fall back to (2). Format is undocumented and
volatile — we parse defensively.
"""
from __future__ import annotations

import json
import platform
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from praxis.models import Provider, Role, Session, Turn
from praxis.scanners.base import BaseScanner


def _vscode_user_paths() -> list[Path]:
    """Possible VS Code 'User' dir locations across OSes and forks."""
    system = platform.system()
    home = Path.home()
    candidates: list[Path] = []
    if system == "Darwin":
        base = home / "Library" / "Application Support"
        for variant in ("Code", "Code - Insiders", "Cursor", "VSCodium"):
            candidates.append(base / variant / "User")
    elif system == "Linux":
        for variant in ("Code", "Code - Insiders", "Cursor", "VSCodium"):
            candidates.append(home / ".config" / variant / "User")
    elif system == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        for variant in ("Code", "Code - Insiders", "Cursor", "VSCodium"):
            candidates.append(appdata / variant / "User")
    return [p for p in candidates if p.exists()]


# Some platforms need os imported lazily for Windows path; import at top:
import os  # noqa: E402


class CopilotScanner(BaseScanner):
    provider_name = "copilot"

    def __init__(self, roots: list[Path] | None = None):
        self.roots = roots or _vscode_user_paths()

    def discover(self) -> Iterator[Path]:
        for user_dir in self.roots:
            ws_storage = user_dir / "workspaceStorage"
            if not ws_storage.exists():
                continue
            for workspace in ws_storage.iterdir():
                if not workspace.is_dir():
                    continue
                # Newer format
                chat_dir = workspace / "chatSessions"
                if chat_dir.exists():
                    yield from chat_dir.glob("*.json")
                # Older format
                vscdb = workspace / "state.vscdb"
                if vscdb.exists():
                    yield vscdb

    def parse(self, path: Path) -> Session | None:
        if path.suffix == ".json":
            return self._parse_json(path)
        if path.name == "state.vscdb":
            return self._parse_sqlite(path)
        return None

    def _parse_json(self, path: Path) -> Session | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None

        # The schema varies; common shape: { "requests": [ {"message": {...}, "response": {...}}, ... ] }
        turns: list[Turn] = []
        requests = data.get("requests") or data.get("requesterUsername") or []
        if isinstance(requests, list):
            for req in requests:
                if not isinstance(req, dict):
                    continue
                user_msg = self._extract_message(req.get("message"))
                if user_msg:
                    turns.append(Turn(role=Role.USER, content=user_msg))
                response = req.get("response")
                resp_text = self._extract_response(response)
                if resp_text:
                    turns.append(Turn(role=Role.ASSISTANT, content=resp_text))

        if not turns:
            return None

        return Session(
            provider=Provider.COPILOT,
            session_id=path.stem,
            started_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            turns=turns,
            source_path=str(path),
            project_hint=path.parent.parent.name,
        )

    def _parse_sqlite(self, path: Path) -> Session | None:
        """Best-effort SQLite extraction. Returns at most one Session per DB."""
        tmpdir = Path(tempfile.mkdtemp(prefix="praxis-copilot-"))
        try:
            tmp_db = tmpdir / "state.vscdb"
            shutil.copy2(path, tmp_db)
            conn = sqlite3.connect(str(tmp_db))
            try:
                cursor = conn.execute(
                    "SELECT key, value FROM ItemTable WHERE key LIKE '%chat%' OR key LIKE '%copilot%'"
                )
                turns: list[Turn] = []
                for _key, value in cursor.fetchall():
                    if not value:
                        continue
                    try:
                        data = json.loads(value) if isinstance(value, str) else json.loads(value.decode("utf-8"))
                    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                        continue
                    turns.extend(self._mine_turns(data))
                if not turns:
                    return None
                return Session(
                    provider=Provider.COPILOT,
                    session_id=path.parent.name,
                    started_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                    turns=turns,
                    source_path=str(path),
                    project_hint=path.parent.name,
                )
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _mine_turns(self, blob: object) -> list[Turn]:
        """Recursively find user/assistant messages in arbitrary JSON."""
        out: list[Turn] = []
        if isinstance(blob, dict):
            # Heuristic: if it looks like a chat turn, extract it
            role = blob.get("role")
            content = blob.get("content") or blob.get("text") or blob.get("value")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                out.append(Turn(role=Role(role), content=content))
            for v in blob.values():
                out.extend(self._mine_turns(v))
        elif isinstance(blob, list):
            for v in blob:
                out.extend(self._mine_turns(v))
        return out

    @staticmethod
    def _extract_message(msg: object) -> str:
        if isinstance(msg, str):
            return msg
        if isinstance(msg, dict):
            text = msg.get("text") or msg.get("content") or ""
            if isinstance(text, str):
                return text
        return ""

    @staticmethod
    def _extract_response(resp: object) -> str:
        if isinstance(resp, str):
            return resp
        if isinstance(resp, list):
            parts: list[str] = []
            for item in resp:
                if isinstance(item, dict):
                    val = item.get("value") or item.get("text") or ""
                    if isinstance(val, str):
                        parts.append(val)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        if isinstance(resp, dict):
            val = resp.get("value") or resp.get("text") or ""
            if isinstance(val, str):
                return val
        return ""
