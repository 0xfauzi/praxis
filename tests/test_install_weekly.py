"""Tests for the install-weekly / uninstall-weekly paths (US-079, US-080, US-083).

Acceptance criteria covered:
  - ``praxis install-weekly`` writes ~/Library/LaunchAgents/co.praxis.weekly.plist
    with the day/time from ~/.praxis/config.toml.
  - The plist's ProgramArguments invoke ``praxis review --notify``.
  - Re-running install-weekly is idempotent (existing job is unloaded,
    the plist is overwritten, and the new job is loaded again).
  - launchctl failure surfaces as CLI exit code 4 with a clear message.
  - ``praxis uninstall-weekly`` unloads and deletes the plist.
  - ``praxis uninstall-weekly`` is a no-op (exit 0) when no plist exists.
  - On non-macOS, install-weekly prints the equivalent systemd timer
    or Task Scheduler XML and saves it to
    ``~/.praxis/install-weekly-snippet.txt``, exiting 0 without
    scheduling anything.

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
    build_systemd_snippet,
    build_task_scheduler_snippet,
    install_weekly_macos,
    plist_path,
    snippet_path,
    uninstall_weekly_macos,
    write_non_macos_snippet,
)
from praxis.config import ScheduleConfig

# ---------------------------------------------------------------------------
# build_plist -- pure function, no subprocess.
# ---------------------------------------------------------------------------


def test_build_plist_encodes_weekday_hour_minute():
    """The StartCalendarInterval block reflects the dataclass fields verbatim."""
    text = build_plist(
        PlistContext(
            program_arguments=["/usr/local/bin/praxis", "review", "--notify"],
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
            program_arguments=["/usr/local/bin/praxis", "review", "--notify"],
            weekday=1,
            hour=9,
            minute=30,
            log_dir=Path("/tmp/.praxis/logs"),
        )
    )
    bin_idx = text.index("/usr/local/bin/praxis")
    review_idx = text.index("<string>review</string>")
    notify_idx = text.index("<string>--notify</string>")
    assert bin_idx < review_idx < notify_idx


def test_build_plist_includes_label():
    """The Label key matches the install-weekly module's canonical label."""
    text = build_plist(
        PlistContext(
            program_arguments=["praxis", "review", "--notify"],
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

    def _run(cmd, *args, **kwargs):
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
    text = text.replace('day = "monday"', 'day = "wednesday"')
    # Hour is already 9 in the new defaults so leave that line alone.
    text = text.replace("minute = 0", "minute = 30")
    cfg_path.write_text(text)

    path = install_weekly_macos()
    plist_text = path.read_text()
    # Wednesday is launchd weekday 3.
    assert "<key>Weekday</key>" in plist_text
    assert "<integer>3</integer>" in plist_text
    assert "<integer>9</integer>" in plist_text
    assert "<integer>30</integer>" in plist_text


def test_install_weekly_program_arguments_run_praxis_review_notify(tmp_home, fake_launchctl):
    """The launchd ProgramArguments include 'review' and '--notify' in order."""
    path = install_weekly_macos()
    text = path.read_text()
    assert "<string>review</string>" in text
    assert "<string>--notify</string>" in text
    # `review` MUST come before `--notify` so the launchd invocation
    # parses identically to a hand-typed `praxis review --notify`.
    assert text.index("<string>review</string>") < text.index("<string>--notify</string>")


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
    new_calls = fake_launchctl[len(initial_calls) :]
    assert len(new_calls) == 2
    assert new_calls[0][1] == "unload"
    assert new_calls[1][1] == "load"


def test_install_weekly_returns_exit_4_when_launchctl_fails(tmp_home, monkeypatch):
    """A non-zero launchctl exit code surfaces as InstallWeeklyError + CLI exit 4."""

    def _run(cmd, *args, **kwargs):
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
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr=""),
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
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr=""),
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
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr=""),
    )
    installed = install_weekly_macos()
    assert installed.exists()

    code = main(["uninstall-weekly"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Removed" in out
    assert "co.praxis.weekly.plist" in out
    assert not installed.exists()


def test_cli_uninstall_weekly_exits_0_when_nothing_installed(tmp_home, capsys, monkeypatch):
    """``praxis uninstall-weekly`` exits 0 even when there is no plist to remove."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr=""),
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


# ---------------------------------------------------------------------------
# US-083 -- non-macOS install-weekly snippet.
#
# Renderers are pure: we exercise build_systemd_snippet /
# build_task_scheduler_snippet directly with known inputs, then verify
# the CLI / dispatch wrapper writes the file and exits 0 without
# scheduling anything.
# ---------------------------------------------------------------------------


def test_build_systemd_snippet_uses_day_and_time():
    """The OnCalendar line reflects the configured day/hour/minute."""
    text = build_systemd_snippet(
        ScheduleConfig(day="wednesday", hour=9, minute=30),
        ["/usr/local/bin/praxis"],
    )
    assert "OnCalendar=Wed *-*-* 09:30:00" in text


def test_build_systemd_snippet_invokes_praxis_review_notify():
    """The ExecStart line ends with ``review --notify`` for the configured argv."""
    text = build_systemd_snippet(
        ScheduleConfig(),
        ["/usr/local/bin/praxis"],
    )
    assert "ExecStart=/usr/local/bin/praxis review --notify" in text


def test_build_systemd_snippet_rejects_invalid_day():
    """A typo in schedule.day surfaces as InstallWeeklyError, not a bad snippet."""
    with pytest.raises(InstallWeeklyError):
        build_systemd_snippet(
            ScheduleConfig(day="someday", hour=18, minute=0),
            ["praxis"],
        )


def test_build_systemd_snippet_documents_install_steps():
    """The snippet teaches the user how to enable the timer."""
    text = build_systemd_snippet(
        ScheduleConfig(),
        ["praxis"],
    )
    # The three install steps must appear so the user is not left to
    # guess where the unit files go or how to activate them.
    assert "co.praxis.weekly.service" in text
    assert "co.praxis.weekly.timer" in text
    assert "systemctl --user daemon-reload" in text
    assert "systemctl --user enable --now co.praxis.weekly.timer" in text


def test_build_task_scheduler_snippet_uses_day_and_time():
    """Task Scheduler XML encodes the day inside <DaysOfWeek>."""
    text = build_task_scheduler_snippet(
        ScheduleConfig(day="monday", hour=7, minute=15),
        ["praxis.exe"],
    )
    assert "<DaysOfWeek><Monday/></DaysOfWeek>" in text
    assert "<StartBoundary>2020-01-01T07:15:00</StartBoundary>" in text


def test_build_task_scheduler_snippet_runs_praxis_review_notify():
    """Command + Arguments split praxis_argv[0] from the trailing 'review --notify'."""
    text = build_task_scheduler_snippet(
        ScheduleConfig(),
        ["praxis.exe"],
    )
    assert "<Command>praxis.exe</Command>" in text
    assert "<Arguments>review --notify</Arguments>" in text


def test_build_task_scheduler_snippet_rejects_invalid_day():
    """Invalid day strings raise InstallWeeklyError instead of producing bad XML."""
    with pytest.raises(InstallWeeklyError):
        build_task_scheduler_snippet(
            ScheduleConfig(day="someday", hour=12, minute=0),
            ["praxis.exe"],
        )


def test_write_non_macos_snippet_linux_writes_systemd_to_default_path(tmp_home):
    """On Linux, the snippet file lands at ~/.praxis/install-weekly-snippet.txt."""
    path, content = write_non_macos_snippet(platform_override="linux")
    expected = tmp_home / ".praxis" / "install-weekly-snippet.txt"
    assert path == expected
    assert path.exists()
    assert path.read_text(encoding="utf-8") == content
    assert "[Timer]" in content
    assert "OnCalendar=" in content


def test_write_non_macos_snippet_windows_writes_xml(tmp_home):
    """On Windows (``win32``), the snippet contains Task Scheduler XML."""
    path, content = write_non_macos_snippet(platform_override="win32")
    assert path.exists()
    assert content.startswith("<?xml")
    assert "<Task" in content
    assert "<ScheduleByWeek>" in content


def test_write_non_macos_snippet_unknown_unix_falls_back_to_systemd(tmp_home):
    """FreeBSD / other UNIXes get systemd output (easier to adapt than TaskSched XML)."""
    _path, content = write_non_macos_snippet(platform_override="freebsd13")
    assert "[Timer]" in content


def test_cli_install_weekly_non_macos_prints_snippet_to_stdout(tmp_home, capsys, monkeypatch):
    """``praxis install-weekly`` on Linux prints the snippet to stdout and exits 0."""
    monkeypatch.setattr(sys, "platform", "linux")

    # Tripwire: this code path must not shell out to anything. If a
    # future refactor reintroduces a subprocess call here, the test
    # will fail loudly.
    def _fail(*args, **kwargs):
        raise AssertionError("install-weekly on non-macOS must not call subprocess.")

    monkeypatch.setattr("praxis.cli.install_weekly.subprocess.run", _fail)

    code = main(["install-weekly"])
    captured = capsys.readouterr()

    assert code == 0
    # Snippet body goes to stdout (per AC: "printed to stdout").
    assert "OnCalendar=" in captured.out
    assert "co.praxis.weekly.service" in captured.out
    # The CLI also tells the user where the saved copy lives.
    assert "Saved snippet to:" in captured.out
    assert "install-weekly-snippet.txt" in captured.out


def test_cli_install_weekly_non_macos_saves_snippet_to_disk(tmp_home, capsys, monkeypatch):
    """The snippet is persisted to ~/.praxis/install-weekly-snippet.txt."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "praxis.cli.install_weekly.subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not shell out on non-macOS")),
    )

    code = main(["install-weekly"])
    _ = capsys.readouterr()

    expected = tmp_home / ".praxis" / "install-weekly-snippet.txt"
    assert code == 0
    assert expected.exists()
    saved = expected.read_text(encoding="utf-8")
    assert "[Timer]" in saved
    assert "OnCalendar=" in saved


def test_cli_install_weekly_windows_saves_task_scheduler_xml(tmp_home, capsys, monkeypatch):
    """On Windows, the saved snippet is Task Scheduler XML (not systemd)."""
    monkeypatch.setattr(sys, "platform", "win32")
    # Real shutil.which calls into _winapi on a "win32" sys.platform,
    # which crashes when the test host is actually macOS. Stub it.
    monkeypatch.setattr(
        "praxis.cli.install_weekly.shutil.which",
        lambda name: None,
    )

    code = main(["install-weekly"])
    captured = capsys.readouterr()

    expected = tmp_home / ".praxis" / "install-weekly-snippet.txt"
    assert code == 0
    assert expected.exists()
    saved = expected.read_text(encoding="utf-8")
    assert saved.startswith("<?xml")
    assert "<ScheduleByWeek>" in saved
    # And stdout shows the same content.
    assert "<ScheduleByWeek>" in captured.out


def test_snippet_path_uses_home_override(tmp_path):
    """snippet_path honors an explicit home= the same way plist_path does."""
    expected = tmp_path / "install-weekly-snippet.txt"
    assert snippet_path(home=tmp_path) == expected
