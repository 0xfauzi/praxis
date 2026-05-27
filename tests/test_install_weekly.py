"""Tests for the macOS install-weekly / uninstall-weekly paths (US-079, US-080).

Acceptance criteria covered:
  - ``praxis install-weekly`` writes ~/Library/LaunchAgents/co.praxis.weekly.plist
    with the day/time from ~/.praxis/config.toml.
  - The plist's ProgramArguments invoke ``praxis week --notify``.
  - Re-running install-weekly is idempotent (existing job is unloaded,
    the plist is overwritten, and the new job is loaded again).
  - launchctl failure surfaces as CLI exit code 4 with a clear message.
  - ``praxis uninstall-weekly`` unloads and deletes the plist.
  - ``praxis uninstall-weekly`` is a no-op (exit 0) when no plist exists.

Subprocess is monkeypatched: real ``launchctl`` is not invoked. The
tmp_home fixture patches ``Path.home()`` so the plist lands inside
``tmp_path / Library / LaunchAgents``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from praxis.cli.__main__ import main
from praxis.cli.install_weekly import (
    PLIST_LABEL,
    InstallWeeklyError,
    PlistContext,
    build_plist,
    install_weekly_macos,
    plist_path,
    uninstall_weekly_macos,
)


# ---------------------------------------------------------------------------
# build_plist -- pure function, no subprocess.
# ---------------------------------------------------------------------------


def test_build_plist_encodes_weekday_hour_minute():
    """The StartCalendarInterval block reflects the dataclass fields verbatim."""
    text = build_plist(
        PlistContext(
            program_arguments=["/usr/local/bin/praxis", "week", "--notify"],
            weekday=0,
            hour=18,
            minute=0,
            log_dir=Path("/tmp/.praxis/logs"),
        )
    )
    assert "<key>Weekday</key>" in text
    assert "<integer>0</integer>" in text
    assert "<key>Hour</key>" in text
    assert "<integer>18</integer>" in text
    assert "<key>Minute</key>" in text


def test_build_plist_lists_program_arguments_in_order():
    """ProgramArguments survives in its input order, one <string> per arg."""
    text = build_plist(
        PlistContext(
            program_arguments=["/usr/local/bin/praxis", "week", "--notify"],
            weekday=1,
            hour=9,
            minute=30,
            log_dir=Path("/tmp/.praxis/logs"),
        )
    )
    bin_idx = text.index("/usr/local/bin/praxis")
    week_idx = text.index("<string>week</string>")
    notify_idx = text.index("<string>--notify</string>")
    assert bin_idx < week_idx < notify_idx


def test_build_plist_includes_label():
    """The Label key matches the install-weekly module's canonical label."""
    text = build_plist(
        PlistContext(
            program_arguments=["praxis", "week", "--notify"],
            weekday=2,
            hour=8,
            minute=45,
            log_dir=Path("/tmp/.praxis/logs"),
        )
    )
    assert f"<string>{PLIST_LABEL}</string>" in text


# ---------------------------------------------------------------------------
# install_weekly_macos -- end-to-end with launchctl stubbed.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_launchctl(monkeypatch):
    """Record every subprocess.run call from install_weekly module; return success.

    Tests assert on the recorded ``calls`` list so they can verify the
    unload/load ordering without actually invoking launchctl.
    """
    calls: list[list[str]] = []

    def _run(cmd, *args, **kwargs):  # noqa: ARG001
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("praxis.cli.install_weekly.subprocess.run", _run)
    return calls


def test_install_weekly_writes_plist_to_library_launchagents(tmp_home, fake_launchctl):
    """The plist lands at ~/Library/LaunchAgents/co.praxis.weekly.plist (tmp_home-scoped)."""
    path = install_weekly_macos()
    expected = tmp_home / "Library" / "LaunchAgents" / "co.praxis.weekly.plist"
    assert path == expected
    assert expected.exists()


def test_install_weekly_uses_config_day_and_time(tmp_home, fake_launchctl, monkeypatch):
    """The plist's StartCalendarInterval honors schedule.day/hour/minute from config.

    Edits the default config to wednesday@09:30 so we can verify the
    rendered plist picks those values up rather than the defaults.
    """
    from praxis.config import ensure_config_file

    ensure_config_file()
    # Hand-edit the toml file so install-weekly reads non-default values.
    cfg_path = tmp_home / ".praxis" / "config.toml"
    text = cfg_path.read_text()
    text = text.replace('day = "sunday"', 'day = "wednesday"')
    text = text.replace("hour = 18", "hour = 9")
    text = text.replace("minute = 0", "minute = 30")
    cfg_path.write_text(text)

    path = install_weekly_macos()
    plist_text = path.read_text()
    # Wednesday is launchd weekday 3.
    assert "<key>Weekday</key>" in plist_text
    assert "<integer>3</integer>" in plist_text
    assert "<integer>9</integer>" in plist_text
    assert "<integer>30</integer>" in plist_text


def test_install_weekly_program_arguments_run_praxis_week_notify(tmp_home, fake_launchctl):
    """The launchd ProgramArguments include 'week' and '--notify' in order."""
    path = install_weekly_macos()
    text = path.read_text()
    assert "<string>week</string>" in text
    assert "<string>--notify</string>" in text
    # `week` MUST come before `--notify` so the launchd invocation
    # parses identically to a hand-typed `praxis week --notify`.
    assert text.index("<string>week</string>") < text.index("<string>--notify</string>")


def test_install_weekly_calls_launchctl_load(tmp_home, fake_launchctl):
    """The installer must shell out to ``launchctl load`` for the new plist."""
    path = install_weekly_macos()
    # On a clean machine the plist did not exist, so only one call (load).
    assert len(fake_launchctl) == 1
    cmd = fake_launchctl[0]
    assert cmd[0] == "launchctl"
    assert "load" in cmd
    assert cmd[-1] == str(path)


def test_install_weekly_is_idempotent_on_re_run(tmp_home, fake_launchctl):
    """Second run unloads the existing job, rewrites the plist, and reloads.

    Asserts the sequence: first run loads once, second run unloads then
    loads. The plist file ends up at the same path and remains valid XML.
    """
    first = install_weekly_macos()
    initial_calls = list(fake_launchctl)
    assert len(initial_calls) == 1
    assert initial_calls[0][1] == "load"

    second = install_weekly_macos()
    assert second == first
    assert second.exists()
    # Re-run added an unload (because the plist already existed) and a load.
    new_calls = fake_launchctl[len(initial_calls):]
    assert len(new_calls) == 2
    assert new_calls[0][1] == "unload"
    assert new_calls[1][1] == "load"


def test_install_weekly_returns_exit_4_when_launchctl_fails(tmp_home, monkeypatch):
    """A non-zero launchctl exit code surfaces as InstallWeeklyError + CLI exit 4."""

    def _run(cmd, *args, **kwargs):  # noqa: ARG001
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr="boom: not permitted"
        )

    monkeypatch.setattr("praxis.cli.install_weekly.subprocess.run", _run)

    with pytest.raises(InstallWeeklyError) as exc_info:
        install_weekly_macos()
    assert "boom: not permitted" in str(exc_info.value)


# ---------------------------------------------------------------------------
# CLI wrapper exit codes (the contract callers actually rely on).
# ---------------------------------------------------------------------------


def test_cli_install_weekly_exits_0_on_success(tmp_home, capsys, monkeypatch):
    """``praxis install-weekly`` exits 0 and prints the plist path."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, returncode=0, stdout="", stderr=""
        ),
    )
    code = main(["install-weekly"])
    out = capsys.readouterr().out
    assert code == 0
    assert "co.praxis.weekly.plist" in out


def test_cli_install_weekly_exits_4_on_launchctl_failure(tmp_home, capsys, monkeypatch):
    """``praxis install-weekly`` exits 4 when launchctl returns non-zero (spec 12.3)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, returncode=2, stdout="", stderr="nope"
        ),
    )
    code = main(["install-weekly"])
    err = capsys.readouterr().err
    assert code == 4
    assert "install-weekly failed" in err
    assert "nope" in err


def test_plist_path_uses_home_override(tmp_path):
    """plist_path honors the explicit ``home`` argument for non-test callers too."""
    expected = tmp_path / "Library" / "LaunchAgents" / "co.praxis.weekly.plist"
    assert plist_path(home=tmp_path) == expected


# ---------------------------------------------------------------------------
# uninstall_weekly_macos -- US-080.
# ---------------------------------------------------------------------------


def test_uninstall_weekly_unloads_and_deletes_plist(tmp_home, fake_launchctl):
    """Uninstall calls ``launchctl unload`` on the plist and removes the file."""
    # Install first so there's something to remove (this records a
    # ``launchctl load`` call in fake_launchctl that we slice off
    # before asserting on uninstall's calls).
    installed = install_weekly_macos()
    assert installed.exists()
    install_call_count = len(fake_launchctl)

    removed = uninstall_weekly_macos()

    assert removed is True
    assert not installed.exists()
    new_calls = fake_launchctl[install_call_count:]
    assert len(new_calls) == 1
    assert new_calls[0][0] == "launchctl"
    assert new_calls[0][1] == "unload"
    assert new_calls[0][-1] == str(installed)


def test_uninstall_weekly_when_absent_is_a_noop(tmp_home, fake_launchctl):
    """When no plist exists, uninstall returns False and does not call launchctl."""
    expected = plist_path()
    assert not expected.exists()

    removed = uninstall_weekly_macos()

    assert removed is False
    # No subprocess call should have been made: there was nothing to unload.
    assert fake_launchctl == []


def test_uninstall_weekly_tolerates_launchctl_failure(tmp_home, monkeypatch):
    """A non-zero ``launchctl unload`` must not block plist deletion.

    launchctl returns non-zero when the job is not currently loaded,
    and we cannot distinguish that from a real failure; either way,
    the plist file should still be removed so the user reaches the
    "nothing scheduled" end state.
    """
    # Place a plist via the real installer (with a stub that succeeds),
    # then swap the stub for one that fails on unload to simulate a
    # half-detached job.
    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, returncode=0, stdout="", stderr=""
        ),
    )
    installed = install_weekly_macos()
    assert installed.exists()

    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr="not loaded"
        ),
    )

    removed = uninstall_weekly_macos()
    assert removed is True
    assert not installed.exists()


def test_cli_uninstall_weekly_exits_0_after_removal(tmp_home, capsys, monkeypatch):
    """``praxis uninstall-weekly`` exits 0 and reports the path it removed."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, returncode=0, stdout="", stderr=""
        ),
    )
    installed = install_weekly_macos()
    assert installed.exists()

    code = main(["uninstall-weekly"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Removed" in out
    assert "co.praxis.weekly.plist" in out
    assert not installed.exists()


def test_cli_uninstall_weekly_exits_0_when_nothing_installed(
    tmp_home, capsys, monkeypatch
):
    """``praxis uninstall-weekly`` exits 0 even when there is no plist to remove."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, returncode=0, stdout="", stderr=""
        ),
    )
    assert not plist_path().exists()

    code = main(["uninstall-weekly"])

    out = capsys.readouterr().out
    assert code == 0
    assert "No weekly LaunchAgent" in out


def test_cli_uninstall_weekly_non_macos_exits_0(tmp_home, capsys, monkeypatch):
    """On non-macOS, ``praxis uninstall-weekly`` exits 0 and explains the no-op."""
    monkeypatch.setattr(sys, "platform", "linux")

    code = main(["uninstall-weekly"])
    err = capsys.readouterr().err

    assert code == 0
    assert "non-macOS" in err
