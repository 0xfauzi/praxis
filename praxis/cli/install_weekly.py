"""``praxis install-weekly`` -- macOS launchd integration (spec 12.4, 13).

The weekly digest cadence is the core delivery surface in v0.2: a
LaunchAgent fires ``praxis week --notify`` on the user-configured day
and time, which writes the HTML digest under ``~/.praxis/weeks/`` and
posts a macOS notification. ``install_weekly_macos`` generates and
loads the launchd plist; ``uninstall_weekly_macos`` reverses it.

This module is intentionally testable: the plist text is built by a
pure function (:func:`build_plist`) and the launchctl calls go through
``subprocess.run`` so tests can monkeypatch them.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from praxis.config import ScheduleConfig, load_config


PLIST_LABEL = "co.praxis.weekly"


class InstallWeeklyError(Exception):
    """Raised when install-weekly cannot complete (bad config or launchctl error).

    The CLI catches this, prints :func:`str(exc)` to stderr, and exits
    with code 4 per spec 12.3.
    """


# launchd's StartCalendarInterval.Weekday is 0=Sunday..6=Saturday (7
# also accepted for Sunday). We accept English weekday names in the
# config file because that is what spec 12.2 documents.
_WEEKDAY_BY_NAME: dict[str, int] = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
}


def _normalize_day(day: str) -> int:
    key = day.strip().lower()
    if key not in _WEEKDAY_BY_NAME:
        valid = ", ".join(_WEEKDAY_BY_NAME.keys())
        raise InstallWeeklyError(
            f"Invalid schedule.day {day!r}: expected one of {valid}."
        )
    return _WEEKDAY_BY_NAME[key]


def _normalize_hour(hour: int) -> int:
    if not (0 <= hour <= 23):
        raise InstallWeeklyError(
            f"Invalid schedule.hour {hour}: must be 0-23."
        )
    return hour


def _normalize_minute(minute: int) -> int:
    if not (0 <= minute <= 59):
        raise InstallWeeklyError(
            f"Invalid schedule.minute {minute}: must be 0-59."
        )
    return minute


def plist_path(home: Path | None = None) -> Path:
    """Canonical LaunchAgent path: ``~/Library/LaunchAgents/co.praxis.weekly.plist``.

    The ``home`` override is for tests; the tmp_home fixture patches
    ``Path.home()`` so the default path falls inside the sandbox.
    """
    base = home if home is not None else Path.home()
    return base / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _resolve_praxis_command() -> list[str]:
    """Return the absolute argv that runs ``praxis`` from launchd's PATH-less env.

    Prefer the installed console script (``shutil.which('praxis')``)
    because that is the binary the user invoked us with. Fall back to
    ``[sys.executable, '-m', 'praxis.cli']`` if the script isn't on
    PATH -- this still works in editable / venv installs because
    sys.executable is always absolute.
    """
    found = shutil.which("praxis")
    if found:
        return [found]
    return [sys.executable, "-m", "praxis.cli"]


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@dataclass(frozen=True)
class PlistContext:
    """Inputs that fully determine the plist text.

    Kept as an explicit dataclass so tests can construct a context with
    known values and assert on :func:`build_plist` output without going
    through the full installer.
    """
    program_arguments: Sequence[str]
    weekday: int
    hour: int
    minute: int
    log_dir: Path


def build_plist(ctx: PlistContext) -> str:
    """Render the LaunchAgent XML for ``co.praxis.weekly``.

    The plist runs ``praxis week --notify`` on the configured day/time
    in local time, writes stdout/stderr to ``~/.praxis/logs/`` so the
    job is debuggable (Appendix A item 4), and uses
    ``StartCalendarInterval`` so launchd handles missed runs
    automatically when the machine sleeps past the scheduled time.
    """
    args_xml = "\n".join(
        f"        <string>{_xml_escape(a)}</string>" for a in ctx.program_arguments
    )
    stdout_path = ctx.log_dir / "weekly.out.log"
    stderr_path = ctx.log_dir / "weekly.err.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>{ctx.weekday}</integer>
        <key>Hour</key>
        <integer>{ctx.hour}</integer>
        <key>Minute</key>
        <integer>{ctx.minute}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(stdout_path))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(stderr_path))}</string>
</dict>
</plist>
"""


def _build_context(
    schedule: ScheduleConfig,
    praxis_argv: Sequence[str],
    log_dir: Path,
) -> PlistContext:
    return PlistContext(
        program_arguments=[*praxis_argv, "week", "--notify"],
        weekday=_normalize_day(schedule.day),
        hour=_normalize_hour(schedule.hour),
        minute=_normalize_minute(schedule.minute),
        log_dir=log_dir,
    )


def install_weekly_macos(
    home: Path | None = None,
    praxis_home: Path | None = None,
) -> Path:
    """Generate and load the LaunchAgent plist; return its path.

    Idempotent: if the plist already exists, it is unloaded first so
    launchd doesn't refuse the reload, then overwritten in place and
    re-loaded. The unload step is best-effort -- launchctl returns
    non-zero when the job isn't loaded, and that's fine.

    Raises :class:`InstallWeeklyError` (wrapping the launchctl exit
    code / stderr) when the load step itself fails, which the CLI maps
    to exit 4.
    """
    from praxis.storage.profile_store import resolve_home

    cfg = load_config(praxis_home)
    plist_file = plist_path(home)
    plist_file.parent.mkdir(parents=True, exist_ok=True)

    log_dir = (praxis_home if praxis_home is not None else resolve_home()) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    ctx = _build_context(cfg.schedule, _resolve_praxis_command(), log_dir)
    plist_text = build_plist(ctx)

    if plist_file.exists():
        # Best-effort unload so launchctl will accept the reload. We
        # cannot distinguish "wasn't loaded" from "actually failed", so
        # both are tolerated here; the load step below is the gate.
        subprocess.run(
            ["launchctl", "unload", str(plist_file)],
            check=False,
            capture_output=True,
        )

    plist_file.write_text(plist_text, encoding="utf-8")

    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise InstallWeeklyError(
            f"launchctl load failed (exit {result.returncode}): "
            f"{stderr or 'no stderr output'}"
        )
    return plist_file


def uninstall_weekly_macos(home: Path | None = None) -> bool:
    """Unload the LaunchAgent and delete the plist; return whether anything existed.

    Per AC US-080, this must exit 0 even when the job is already gone:
    the user-facing contract is "after uninstall-weekly, the job is not
    scheduled," and that contract is trivially satisfied when nothing
    was scheduled in the first place. So we treat both the
    "plist missing" case and a non-zero ``launchctl unload`` as
    success (launchctl returns non-zero for "not loaded," which is
    indistinguishable from a real error from our side -- and either
    way, deleting the file gets us to the desired end state).

    Returns ``True`` when a plist file was found and removed, ``False``
    when nothing was scheduled. The CLI does not currently branch on
    this, but tests assert on it and a future "--quiet" flag could.
    """
    plist_file = plist_path(home)
    if not plist_file.exists():
        return False

    # Best-effort unload. launchctl returns non-zero when the job is
    # not loaded, and we cannot distinguish that from a real failure;
    # in both cases the right next move is to delete the file.
    subprocess.run(
        ["launchctl", "unload", str(plist_file)],
        check=False,
        capture_output=True,
    )
    plist_file.unlink()
    return True
