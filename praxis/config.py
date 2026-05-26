"""Praxis user config file.

Creates ``~/.praxis/config.toml`` on first run with documented defaults.
Subsequent runs never overwrite the file: users can edit it freely
(see ``praxis config``).

Spec section 12.2 defines the canonical schema; ``DEFAULT_CONFIG_TOML``
below mirrors it verbatim, including comments, so the file the user
opens looks exactly like the spec.
"""
from __future__ import annotations

from pathlib import Path

from praxis.storage.profile_store import resolve_home


# Mirrors PRAXIS_V0_2_SPEC.md section 12.2 verbatim. If the spec
# changes, update this string -- the file's job is to be a readable,
# editable copy of the documented defaults, not to be regenerated from
# code constants. Comments survive the round-trip because we ship the
# file as a literal string.
DEFAULT_CONFIG_TOML = """\
[schedule]
day = "sunday"           # any weekday name
hour = 18                # 0-23 local time
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

[privacy]
redact_secrets = true    # MUST default true (Section 4.4)
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
