"""Praxis user config file.

Creates ``~/.praxis/config.toml`` on first run with documented defaults.
Subsequent runs never overwrite the file: users can edit it freely
(see ``praxis config``).

Spec section 12.2 defines the canonical schema; ``DEFAULT_CONFIG_TOML``
below mirrors it verbatim, including comments, so the file the user
opens looks exactly like the spec.

``load_config()`` returns a typed :class:`Config` view of the file with
documented defaults for any field the user omitted -- never raises on
missing keys (Spec 12.2 says the file is user-editable).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from praxis.storage.profile_store import resolve_home


# Mirrors PRAXIS_V0_2_SPEC.md section 12.2 verbatim. If the spec
# changes, update this string -- the file's job is to be a readable,
# editable copy of the documented defaults, not to be regenerated from
# code constants. Comments survive the round-trip because we ship the
# file as a literal string.
DEFAULT_CONFIG_TOML = """\
[schedule]
day = "monday"           # any weekday name
hour = 9                 # 0-23 local time
minute = 0

[scan]
since_days = 7
max_new = 200

[judge]
primary_provider = "anthropic"   # "anthropic" or "openai"
frontier_model = "claude-opus-4-7"
cheap_model = "claude-haiku-4-5"
# OpenAI fallback used automatically if anthropic credit / errors

[notification]
enabled = true           # macOS only; ignored elsewhere
sound = "default"
# style: "banner" | "alert" | "terminal-notifier"
#   banner            -- non-interactive macOS notification (default).
#   alert             -- AppleScript modal dialog with an "Open" button
#                        that opens ~/.praxis/latest.html on click.
#   terminal-notifier -- clickable banner via the third-party
#                        `terminal-notifier` Homebrew binary; opens the
#                        latest digest on click. Falls back to "banner"
#                        if the binary is not on PATH.
style = "banner"

[privacy]
redact_secrets = true    # MUST default true (Section 4.4)

[reflect]
# Threshold gates for `praxis reflect --session-end` (US-026). When the
# AI tool's Stop hook fires, we only prompt the user if the session had
# at least `turns_min` user turns AND lasted at least
# `elapsed_seconds_min`; shorter sessions write a 'skip' reflection row
# so opt-outs / nuisance sessions are still counted in the digest panel.
turns_min = 2
elapsed_seconds_min = 60
# Soft budget for the Stop-hook parent to return (US-027). The parent
# always returns immediately after spawning a detached child, so this
# value is the documented upper bound rather than an enforced timeout.
hook_timeout_seconds = 5
"""


def config_path(home: Path | None = None) -> Path:
    """Return the canonical path to the user's ``config.toml``."""
    base = home if home is not None else resolve_home()
    return base / "config.toml"


def ensure_config_file(home: Path | None = None) -> Path:
    """Create ``~/.praxis/config.toml`` with defaults if missing.

    Never overwrites an existing file. Returns the path either way so
    callers can immediately read or display it.
    """
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return path


# Defaults below MUST match DEFAULT_CONFIG_TOML above. The string is
# what the user reads/edits; the dataclasses are what code consumes.
# Both copies exist so the file stays human-readable (with comments)
# while typed access stays cheap (no parse on every attribute read).


@dataclass(frozen=True)
class ScheduleConfig:
    day: str = "monday"
    hour: int = 9
    minute: int = 0


@dataclass(frozen=True)
class ScanConfig:
    since_days: int = 7
    max_new: int = 200


@dataclass(frozen=True)
class JudgeConfig:
    primary_provider: str = "anthropic"
    frontier_model: str = "claude-opus-4-7"
    cheap_model: str = "claude-haiku-4-5"


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = True
    sound: str = "default"
    # "banner" stays the default to preserve the existing UX. The other
    # two add a click-to-open affordance: "alert" via AppleScript modal,
    # "terminal-notifier" via the optional Homebrew binary. Anything not
    # in the allowed set is treated as "banner" so a typo in config.toml
    # is harmless.
    style: str = "banner"


@dataclass(frozen=True)
class PrivacyConfig:
    redact_secrets: bool = True


@dataclass(frozen=True)
class ReflectConfig:
    """Threshold and timing gates for ``praxis reflect --session-end``.

    ``turns_min`` and ``elapsed_seconds_min`` (US-026) gate whether the
    Stop hook escalates to an interactive prompt. ``hook_timeout_seconds``
    (US-027) is the soft budget for the parent process to return before
    the AI tool considers the hook hung; the actual parent spawn returns
    immediately, so this value documents the upper bound rather than
    enforcing a timeout.

    All three are integers and must be non-negative. The loader rejects
    negative values at load time so a typo in ``config.toml`` surfaces
    immediately rather than silently producing surprising behavior. Zero
    is allowed for the threshold gates (it disables them) but not for
    ``hook_timeout_seconds`` (a zero budget would defeat the purpose of
    the soft limit; we still accept zero in case a future story wants to
    opt the parent into blocking).
    """

    turns_min: int = 2
    elapsed_seconds_min: int = 60
    hook_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.turns_min, int) or isinstance(self.turns_min, bool):
            raise ValueError(
                "[reflect] turns_min must be a non-negative integer; "
                f"got {self.turns_min!r}"
            )
        if not isinstance(self.elapsed_seconds_min, int) or isinstance(
            self.elapsed_seconds_min, bool
        ):
            raise ValueError(
                "[reflect] elapsed_seconds_min must be a non-negative integer; "
                f"got {self.elapsed_seconds_min!r}"
            )
        if not isinstance(self.hook_timeout_seconds, int) or isinstance(
            self.hook_timeout_seconds, bool
        ):
            raise ValueError(
                "[reflect] hook_timeout_seconds must be a non-negative integer; "
                f"got {self.hook_timeout_seconds!r}"
            )
        if self.turns_min < 0:
            raise ValueError(
                "[reflect] turns_min must be >= 0; "
                f"got {self.turns_min}"
            )
        if self.elapsed_seconds_min < 0:
            raise ValueError(
                "[reflect] elapsed_seconds_min must be >= 0; "
                f"got {self.elapsed_seconds_min}"
            )
        if self.hook_timeout_seconds < 0:
            raise ValueError(
                "[reflect] hook_timeout_seconds must be >= 0; "
                f"got {self.hook_timeout_seconds}"
            )


@dataclass(frozen=True)
class Config:
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    reflect: ReflectConfig = field(default_factory=ReflectConfig)


def _section(cls: type, data: Any) -> Any:
    """Build a section dataclass, ignoring unknown keys and defaulting missing ones.

    The user's config.toml is allowed to drift from the spec (new keys
    we don't know about yet, old keys removed). We only consume keys
    the dataclass declares; anything else is silently ignored so the
    loader never raises on a hand-edited file.
    """
    if not isinstance(data, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    return cls(**kwargs)


def load_config(home: Path | None = None) -> Config:
    """Return the parsed :class:`Config`, creating the file if missing.

    Missing sections or fields fall back to the documented defaults
    (see :data:`DEFAULT_CONFIG_TOML`). Unknown keys are ignored so the
    loader survives forward/backward config drift without raising.
    """
    path = ensure_config_file(home)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Config(
        schedule=_section(ScheduleConfig, raw.get("schedule")),
        scan=_section(ScanConfig, raw.get("scan")),
        judge=_section(JudgeConfig, raw.get("judge")),
        notification=_section(NotificationConfig, raw.get("notification")),
        privacy=_section(PrivacyConfig, raw.get("privacy")),
        reflect=_section(ReflectConfig, raw.get("reflect")),
    )
