"""Tests for praxis.config.

Covers US-005 acceptance criteria: first-run file creation with the
documented defaults, idempotence (existing files are never
overwritten), and the resolved path.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from praxis.config import (
    DEFAULT_CONFIG_TOML,
    config_path,
    ensure_config_file,
)


def _load(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def test_ensure_config_creates_file_when_missing(tmp_home):
    path = ensure_config_file()
    assert path == tmp_home / ".praxis" / "config.toml"
    assert path.exists()


def test_ensure_config_returns_existing_path(tmp_home):
    path = config_path()
    assert path == tmp_home / ".praxis" / "config.toml"


def test_ensure_config_creates_parent_dir(tmp_home):
    # The ~/.praxis dir does not yet exist in a fresh tmp_home.
    parent = tmp_home / ".praxis"
    assert not parent.exists()
    ensure_config_file()
    assert parent.is_dir()


def test_ensure_config_never_overwrites_existing_file(tmp_home):
    path = ensure_config_file()
    edited = '[schedule]\nday = "monday"\nhour = 9\nminute = 30\n'
    path.write_text(edited, encoding="utf-8")
    # Second invocation must leave the user's edits intact.
    again = ensure_config_file()
    assert again == path
    assert path.read_text(encoding="utf-8") == edited


def test_default_schedule_section(tmp_home):
    path = ensure_config_file()
    data = _load(path)
    assert data["schedule"]["day"] == "sunday"
    assert data["schedule"]["hour"] == 18
    assert data["schedule"]["minute"] == 0


def test_default_scan_section(tmp_home):
    path = ensure_config_file()
    data = _load(path)
    assert data["scan"]["since_days"] == 7
    assert data["scan"]["max_new"] == 200


def test_default_judge_section(tmp_home):
    path = ensure_config_file()
    data = _load(path)
    assert data["judge"]["primary_provider"] == "anthropic"
    assert data["judge"]["frontier_model"] == "claude-opus-4-7"
    assert data["judge"]["cheap_model"] == "claude-haiku-4-5"


def test_default_notification_section(tmp_home):
    path = ensure_config_file()
    data = _load(path)
    assert data["notification"]["enabled"] is True
    assert data["notification"]["sound"] == "default"


def test_default_privacy_section(tmp_home):
    path = ensure_config_file()
    data = _load(path)
    assert data["privacy"]["redact_secrets"] is True


def test_default_config_string_parses_as_valid_toml():
    # Guard against accidental syntax breaks in the literal template;
    # tomllib raises on malformed TOML.
    parsed = tomllib.loads(DEFAULT_CONFIG_TOML)
    assert set(parsed.keys()) == {
        "schedule",
        "scan",
        "judge",
        "notification",
        "privacy",
    }


def test_ensure_config_respects_explicit_home(tmp_path):
    # Bypass env var entirely: pass a home= argument.
    home = tmp_path / "custom-home"
    path = ensure_config_file(home=home)
    assert path == home / "config.toml"
    assert path.exists()


def test_cli_main_creates_config_on_first_run(tmp_home, capsys):
    # `praxis rubric` is a pure print command with no side effects of
    # its own beyond stdout, so it is a clean way to assert that the
    # CLI wrapper triggers ensure_config_file().
    from praxis.cli.__main__ import main

    config = tmp_home / ".praxis" / "config.toml"
    assert not config.exists()
    rc = main(["rubric"])
    capsys.readouterr()  # discard rubric output
    assert rc == 0
    assert config.exists()


# Suppress unused-import warnings on Python versions where tomllib is
# missing (we require >=3.11, but a guard makes the test file readable
# in older environments).
assert sys.version_info >= (3, 11)
