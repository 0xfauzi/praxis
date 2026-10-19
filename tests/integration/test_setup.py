"""`praxis install <agent>` and `praxis remove <agent>`, driven as the owner drives
them: the installed binary on a pseudo-terminal, typing the answer, against a temporary
HOME. Every case reads the file back and asserts what changed and what did not.
AGENTS.md invariant 8.
"""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

ALL_CLAUDE_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
}
TOOLS = "Edit|Write|MultiEdit|NotebookEdit|Bash"
MATCHED = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
OTHER_HOOK = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "echo other"}],
}


class Home:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.praxis_home = tmp_path / "praxis-home"
        self.home.mkdir()

    @property
    def settings(self) -> Path:
        return self.home / ".claude" / "settings.json"

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PRAXIS_HOME": str(self.praxis_home),
            "PATH": os.environ.get("PATH", ""),
            "TERM": "dumb",
        }

    def write_settings(self, data: Any) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, indent=2) + "\n"
        self.settings.write_text(text)

    def read_settings(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.settings.read_text())
        return loaded

    def on_tty(self, *args: str, answer: str) -> tuple[int, str]:
        """Run `praxis <args>` with stdin and stdout on one pseudo-terminal."""
        found = shutil.which("praxis")
        assert found is not None
        leader, follower = pty.openpty()
        try:
            proc = subprocess.Popen(
                [found, *args],
                stdin=follower,
                stdout=follower,
                stderr=follower,
                env=self.env(),
                close_fds=True,
            )
            os.close(follower)
            output = _pump(leader, proc, answer)
            proc.wait(timeout=10)
            return proc.returncode, output.decode("utf-8", "replace")
        finally:
            os.close(leader)

    def piped(self, *args: str) -> subprocess.CompletedProcess[str]:
        found = shutil.which("praxis")
        assert found is not None
        return subprocess.run(
            [found, *args],
            input="y\n",
            capture_output=True,
            text=True,
            check=False,
            env=self.env(),
            timeout=60,
        )


def _pump(leader: int, proc: subprocess.Popen[bytes], answer: str) -> bytes:
    """Read the terminal until the process ends, typing the answer at the prompt."""
    output = b""
    deadline = time.time() + 60
    answered = False
    while time.time() < deadline:
        ready, _, _ = select.select([leader], [], [], 0.1)
        if not ready:
            if proc.poll() is not None:
                break
            continue
        try:
            chunk = os.read(leader, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if b"[y/N]" in output and not answered:
            os.write(leader, answer.encode() + b"\n")
            answered = True
    return output


def praxis_entries(data: dict[str, Any], event: str) -> list[Any]:
    return [
        e
        for e in data.get("hooks", {}).get(event, [])
        if any(h.get("command", "").endswith("praxis hook") for h in e.get("hooks", []))
    ]


def test_apply_creates_the_file_when_absent_after_a_yes(tmp_path: Path) -> None:
    h = Home(tmp_path)
    code, out = h.on_tty("install", "claude", answer="y")
    assert code == 0, out
    assert f"praxis wrote {h.settings}." in out
    data = h.read_settings()
    assert set(data["hooks"]) == ALL_CLAUDE_EVENTS
    for event in ALL_CLAUDE_EVENTS:
        assert len(praxis_entries(data, event)) == 1
    assert {e: data["hooks"][e][0].get("matcher") for e in MATCHED} == {
        "PreToolUse": TOOLS,
        "PostToolUse": f"{TOOLS}|AskUserQuestion|ExitPlanMode",
        "PostToolUseFailure": TOOLS,
    }
    assert data["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 10
    # The Stop notice must come from a synchronous hook: an async one's JSON reaches
    # the model (design 5.4).
    assert "async" not in json.dumps(data)
    # Nothing to back up when the file did not exist.
    assert not (h.praxis_home / "settings-backups").exists()


def test_apply_preserves_other_hooks_and_keys_and_keeps_the_previous_bytes(
    tmp_path: Path,
) -> None:
    h = Home(tmp_path)
    before = {
        "model": "opus",
        "permissions": {"allow": ["Bash(git *)"]},
        "hooks": {"PostToolUse": [OTHER_HOOK], "Notification": [OTHER_HOOK]},
    }
    h.write_settings(before)
    previous_bytes = h.settings.read_bytes()
    code, out = h.on_tty("install", "claude", answer="yes")
    assert code == 0, out
    data = h.read_settings()
    assert data["model"] == "opus"
    assert data["permissions"] == before["permissions"]
    assert data["hooks"]["Notification"] == [OTHER_HOOK]
    assert data["hooks"]["PostToolUse"][0] == OTHER_HOOK, "the other hook stays first"
    assert len(praxis_entries(data, "PostToolUse")) == 1
    backups = list((h.praxis_home / "settings-backups").iterdir())
    assert len(backups) == 1
    assert backups[0].read_bytes() == previous_bytes


def test_apply_twice_is_idempotent(tmp_path: Path) -> None:
    h = Home(tmp_path)
    assert h.on_tty("install", "claude", answer="y")[0] == 0
    first = h.settings.read_bytes()
    code, out = h.on_tty("install", "claude", answer="y")
    assert code == 1, out
    assert "  no change: the claude settings file needs no change" in out
    assert h.settings.read_bytes() == first


def test_remove_takes_only_praxis_out(tmp_path: Path) -> None:
    h = Home(tmp_path)
    h.write_settings({"hooks": {"PostToolUse": [OTHER_HOOK]}, "model": "opus"})
    assert h.on_tty("install", "claude", answer="y")[0] == 0
    code, out = h.on_tty("remove", "claude", answer="y")
    assert code == 0, out
    data = h.read_settings()
    assert data == {"hooks": {"PostToolUse": [OTHER_HOOK]}, "model": "opus"}


def test_remove_prunes_the_hooks_key_when_nothing_else_is_left(tmp_path: Path) -> None:
    h = Home(tmp_path)
    assert h.on_tty("install", "claude", answer="y")[0] == 0
    assert h.on_tty("remove", "claude", answer="y")[0] == 0
    assert h.read_settings() == {}


def test_no_answer_means_no_write(tmp_path: Path) -> None:
    h = Home(tmp_path)
    h.write_settings({"model": "opus"})
    code, out = h.on_tty("install", "claude", answer="n")
    assert code == 1, out
    assert "praxis did not write the file." in out
    assert h.read_settings() == {"model": "opus"}


def test_without_a_terminal_apply_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    h = Home(tmp_path)
    done = h.piped("install", "claude")
    assert done.returncode == 2, done.stdout
    assert done.stdout == (
        f"refused: `praxis install claude` writes {h.settings}, a file outside this"
        " repository.\nIt writes only after you type yes on a terminal (AGENTS.md"
        " invariant 8).\nRun the command again on a terminal.\nTo show the settings"
        " and write no file, run `praxis show install claude`.\n"
    )
    assert not h.settings.exists()
    assert not h.praxis_home.exists()


def test_invalid_json_is_refused_and_left_alone(tmp_path: Path) -> None:
    h = Home(tmp_path)
    h.write_settings("{ not json")
    code, out = h.on_tty("install", "claude", answer="y")
    assert code == 2, out
    assert "not valid JSON" in out
    assert h.settings.read_text() == "{ not json"


def test_codex_block_has_no_post_tool_use_failure(tmp_path: Path) -> None:
    h = Home(tmp_path)
    code, out = h.on_tty("install", "codex", answer="y")
    assert code == 0, out
    data = json.loads((h.home / ".codex" / "hooks.json").read_text())
    assert set(data["hooks"]) == ALL_CLAUDE_EVENTS - {"PostToolUseFailure"}
    # Codex has no question tools; its Edit and Write matchers also fire for
    # apply_patch.
    assert {data["hooks"][e][0]["matcher"] for e in MATCHED[:2]} == {TOOLS}


def test_show_status_is_read_only_and_reports_the_hooks(tmp_path: Path) -> None:
    h = Home(tmp_path)

    def status() -> list[str]:
        found = shutil.which("praxis")
        assert found is not None
        done = subprocess.run(
            [found, "show", "status"],
            capture_output=True, text=True, check=False, env=h.env(), cwd=tmp_path,
            timeout=60,
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        return done.stdout.splitlines()

    codex = h.home / ".codex" / "hooks.json"
    run = "To install the praxis hooks, run `praxis install"
    lines = status()
    for line in (
        f"claude: {h.settings} does not exist. {run} claude`.",
        f"codex: {codex} does not exist. {run} codex`.",
        f"ledger: {h.praxis_home / 'ledger.db'} does not exist. The first hook event"
        " makes it.",
        "current directory: not in a git repository",
    ):
        assert line in lines, line
    assert not h.praxis_home.exists(), "show status must create nothing"
    assert h.on_tty("install", "claude", answer="y")[0] == 0
    assert "claude: all 6 events have a praxis hook." in status()
    data = h.read_settings()
    h.write_settings({"hooks": {"Stop": data["hooks"]["Stop"], "Notification": []}})
    assert (
        "claude: 1 of 6 events have a praxis hook. These events have none:"
        " SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure."
        f" {run} claude`."
    ) in status()
    h.write_settings({"hooks": []})
    assert f"claude: {h.settings} has no hooks. {run} claude`." in status()


def test_show_install_prints_a_valid_snippet_and_install_refuses(
    tmp_path: Path,
) -> None:
    h = Home(tmp_path)
    found = shutil.which("praxis")
    assert found is not None
    out = subprocess.run(
        [found, "show", "install", "claude"],
        capture_output=True, check=True, env=h.env(),
    )  # fmt: skip
    block = json.loads(out.stdout)
    assert set(block["hooks"]) == ALL_CLAUDE_EVENTS
    assert block["hooks"]["PostToolUse"][0]["matcher"] == (
        f"{TOOLS}|AskUserQuestion|ExitPlanMode"
    )
    assert block["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("praxis hook")
    # Without a terminal there is no consent, so install refuses and writes nothing.
    refused = h.piped("install", "claude")
    assert refused.returncode == 2
    assert "invariant 8" in refused.stdout
    assert sorted(h.home.rglob("*")) == [], "show install wrote under HOME"
