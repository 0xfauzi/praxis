"""Tests for praxis.config.

Covers US-005 acceptance criteria: first-run file creation with the
documented defaults, idempotence (existing files are never
overwritten), and the resolved path.

Also covers US-006: typed ``load_config()`` exposing all five
sections with documented defaults filling in for any missing field.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from praxis.config import (
    DEFAULT_CONFIG_TOML,
    Config,
    JudgeConfig,
    NotificationConfig,
    NudgeConfig,
    PrivacyConfig,
    ScanConfig,
    ScheduleConfig,
    config_path,
    ensure_config_file,
    load_config,
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
    assert data["schedule"]["day"] == "monday"
    assert data["schedule"]["hour"] == 9
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


def test_default_nudge_section(tmp_home):
    path = ensure_config_file()
    data = _load(path)
    assert data["nudge"]["throttle_minutes"] == 30


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
        "nudge",
        "privacy",
        "reflect",
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


# ---- US-006: typed load_config ---------------------------------------------


def test_load_config_returns_typed_object_with_all_sections(tmp_home):
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert isinstance(cfg.schedule, ScheduleConfig)
    assert isinstance(cfg.scan, ScanConfig)
    assert isinstance(cfg.judge, JudgeConfig)
    assert isinstance(cfg.notification, NotificationConfig)
    assert isinstance(cfg.nudge, NudgeConfig)
    assert isinstance(cfg.privacy, PrivacyConfig)


def test_load_config_returns_documented_defaults_on_first_run(tmp_home):
    cfg = load_config()
    # Mirrors the spec's section-12.2 defaults exactly.
    assert cfg.schedule.day == "monday"
    assert cfg.schedule.hour == 9
    assert cfg.schedule.minute == 0
    assert cfg.scan.since_days == 7
    assert cfg.scan.max_new == 200
    assert cfg.judge.primary_provider == "anthropic"
    assert cfg.judge.frontier_model == "claude-opus-4-7"
    assert cfg.judge.cheap_model == "claude-haiku-4-5"
    assert cfg.notification.enabled is True
    assert cfg.notification.sound == "default"
    assert cfg.notification.style == "banner"
    assert cfg.nudge.throttle_minutes == 30
    assert cfg.privacy.redact_secrets is True


def test_load_config_reads_user_edits(tmp_home):
    path = ensure_config_file()
    path.write_text(
        '[schedule]\nday = "monday"\nhour = 9\nminute = 30\n'
        '[scan]\nsince_days = 14\nmax_new = 50\n'
        '[judge]\nprimary_provider = "openai"\n'
        'frontier_model = "gpt-5"\n'
        'cheap_model = "gpt-5-mini"\n'
        '[notification]\nenabled = false\nsound = "ping"\n'
        '[privacy]\nredact_secrets = false\n',
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.schedule.day == "monday"
    assert cfg.schedule.hour == 9
    assert cfg.schedule.minute == 30
    assert cfg.scan.since_days == 14
    assert cfg.scan.max_new == 50
    assert cfg.judge.primary_provider == "openai"
    assert cfg.judge.frontier_model == "gpt-5"
    assert cfg.judge.cheap_model == "gpt-5-mini"
    assert cfg.notification.enabled is False
    assert cfg.notification.sound == "ping"
    assert cfg.privacy.redact_secrets is False


def test_load_config_missing_fields_fall_back_to_defaults(tmp_home):
    # User edited the file and removed several keys; loader must not raise.
    path = ensure_config_file()
    path.write_text(
        '[schedule]\nday = "friday"\n'  # hour and minute missing
        '[scan]\n'  # both keys missing
        # judge section missing entirely
        '[notification]\nsound = "ping"\n'  # enabled missing
        '[privacy]\n',  # redact_secrets missing
        encoding="utf-8",
    )
    cfg = load_config()
    # User-set field survives.
    assert cfg.schedule.day == "friday"
    # Missing fields use documented defaults.
    assert cfg.schedule.hour == 9
    assert cfg.schedule.minute == 0
    assert cfg.scan.since_days == 7
    assert cfg.scan.max_new == 200
    assert cfg.judge.primary_provider == "anthropic"
    assert cfg.judge.frontier_model == "claude-opus-4-7"
    assert cfg.judge.cheap_model == "claude-haiku-4-5"
    assert cfg.notification.enabled is True
    assert cfg.notification.sound == "ping"
    assert cfg.privacy.redact_secrets is True


def test_load_config_empty_file_yields_all_defaults(tmp_home):
    path = ensure_config_file()
    path.write_text("", encoding="utf-8")
    cfg = load_config()
    assert cfg == Config()


def test_load_config_unknown_keys_are_ignored(tmp_home):
    # Forward-compat: an old config with a key we no longer recognize
    # must not raise; the known fields still resolve correctly.
    path = ensure_config_file()
    path.write_text(
        '[schedule]\nday = "tuesday"\nunknown_field = "ignored"\n'
        '[future_section]\nfoo = "bar"\n',
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.schedule.day == "tuesday"
    # All other sections fall back to defaults.
    assert cfg.scan == ScanConfig()
    assert cfg.judge == JudgeConfig()


def test_load_config_creates_file_when_missing(tmp_home):
    # load_config() on a fresh home must materialize the file (so the
    # user can immediately `praxis config` it) and return defaults.
    config = tmp_home / ".praxis" / "config.toml"
    assert not config.exists()
    cfg = load_config()
    assert config.exists()
    assert cfg == Config()


def test_load_config_respects_explicit_home(tmp_path):
    home = tmp_path / "custom-home"
    cfg = load_config(home=home)
    assert (home / "config.toml").exists()
    assert cfg == Config()


def test_default_config_string_matches_typed_defaults():
    # Sanity check: the template the user reads parses into the same
    # values the dataclasses declare. If a future edit drifts one but
    # not the other, this test fails loudly.
    parsed = tomllib.loads(DEFAULT_CONFIG_TOML)
    defaults = Config()
    assert parsed["schedule"]["day"] == defaults.schedule.day
    assert parsed["schedule"]["hour"] == defaults.schedule.hour
    assert parsed["schedule"]["minute"] == defaults.schedule.minute
    assert parsed["scan"]["since_days"] == defaults.scan.since_days
    assert parsed["scan"]["max_new"] == defaults.scan.max_new
    assert parsed["judge"]["primary_provider"] == defaults.judge.primary_provider
    assert parsed["judge"]["frontier_model"] == defaults.judge.frontier_model
    assert parsed["judge"]["cheap_model"] == defaults.judge.cheap_model
    assert parsed["notification"]["enabled"] == defaults.notification.enabled
    assert parsed["notification"]["sound"] == defaults.notification.sound
    assert parsed["nudge"]["throttle_minutes"] == defaults.nudge.throttle_minutes
    assert parsed["privacy"]["redact_secrets"] == defaults.privacy.redact_secrets


# Suppress unused-import warnings on Python versions where tomllib is
# missing (we require >=3.11, but a guard makes the test file readable
# in older environments).
assert sys.version_info >= (3, 11)
