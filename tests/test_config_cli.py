"""Tests for ``praxis config`` (US-007).

Covers:
  * ``--get`` returns the typed value (string / int / bool) and exits 0.
  * ``--set`` updates the TOML file while preserving:
      - other keys within the same section,
      - other sections in the file,
      - inline comments on lines we did not edit.
  * Invalid dotted keys and malformed inputs exit non-zero with a
    clear error message on stderr.
  * Bare ``praxis config`` launches ``$EDITOR`` on the config path.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from praxis.cli.__main__ import main
from praxis.config import ensure_config_file
from praxis.config_cli import (
    ConfigCLIError,
    config_path,
    get_value,
    open_editor,
    set_value,
)


def _load(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


# ---- --get ------------------------------------------------------------------


def test_get_string_value(tmp_home):
    assert get_value("schedule.day") == "monday"


def test_get_int_value(tmp_home):
    assert get_value("schedule.hour") == "9"


def test_get_bool_value_true(tmp_home):
    assert get_value("notification.enabled") == "true"


def test_get_bool_value_false_after_edit(tmp_home):
    path = ensure_config_file()
    path.write_text(
        '[notification]\nenabled = false\nsound = "default"\n',
        encoding="utf-8",
    )
    assert get_value("notification.enabled") == "false"


def test_get_via_cli_prints_to_stdout(tmp_home, capsys):
    rc = main(["config", "--get", "schedule.day"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert out.strip() == "monday"
    assert err == ""


def test_get_via_cli_prints_int_without_quotes(tmp_home, capsys):
    rc = main(["config", "--get", "scan.since_days"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.strip() == "7"


# ---- --get / --set: invalid dotted keys -------------------------------------


def test_get_missing_dot_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        get_value("schedule")
    assert "dotted form" in str(exc.value)


def test_get_unknown_section_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        get_value("bogus.day")
    assert "Unknown section" in str(exc.value)
    assert "bogus" in str(exc.value)


def test_get_unknown_field_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        get_value("schedule.dayz")
    assert "Unknown field" in str(exc.value)
    assert "dayz" in str(exc.value)


def test_get_three_dot_key_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        get_value("schedule.day.extra")
    assert "exactly one '.'" in str(exc.value)


def test_get_empty_section_raises(tmp_home):
    with pytest.raises(ConfigCLIError):
        get_value(".day")


def test_get_empty_field_raises(tmp_home):
    with pytest.raises(ConfigCLIError):
        get_value("schedule.")


def test_get_via_cli_invalid_key_exits_nonzero(tmp_home, capsys):
    rc = main(["config", "--get", "bogus.day"])
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "Unknown section" in err


def test_get_via_cli_no_dot_exits_nonzero(tmp_home, capsys):
    rc = main(["config", "--get", "noDotKey"])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "dotted form" in err


# ---- --set ------------------------------------------------------------------


def test_set_string_updates_file(tmp_home):
    set_value("schedule.day", "monday")
    data = _load(config_path())
    assert data["schedule"]["day"] == "monday"


def test_set_int_updates_file(tmp_home):
    set_value("schedule.hour", "9")
    data = _load(config_path())
    assert data["schedule"]["hour"] == 9


def test_set_bool_updates_file(tmp_home):
    set_value("notification.enabled", "false")
    data = _load(config_path())
    assert data["notification"]["enabled"] is False


def test_set_bool_accepts_truthy_aliases(tmp_home):
    for raw in ("true", "yes", "1", "on", "TRUE", "True"):
        set_value("notification.enabled", raw)
        assert _load(config_path())["notification"]["enabled"] is True
    for raw in ("false", "no", "0", "off", "FALSE", "False"):
        set_value("notification.enabled", raw)
        assert _load(config_path())["notification"]["enabled"] is False


def test_set_preserves_other_keys_in_same_section(tmp_home):
    set_value("schedule.day", "friday")
    data = _load(config_path())
    # Other keys in [schedule] keep their defaults.
    assert data["schedule"]["hour"] == 9
    assert data["schedule"]["minute"] == 0


def test_set_preserves_other_sections(tmp_home):
    set_value("schedule.day", "friday")
    data = _load(config_path())
    # All four other sections are untouched.
    assert data["scan"] == {"since_days": 7, "max_new": 200}
    assert data["judge"]["primary_provider"] == "anthropic"
    assert data["judge"]["frontier_model"] == "claude-opus-4-7"
    assert data["judge"]["cheap_model"] == "claude-haiku-4-5"
    assert data["notification"] == {
        "enabled": True,
        "sound": "default",
        "style": "banner",
    }
    assert data["privacy"] == {"redact_secrets": True}


def test_set_preserves_inline_comments_on_other_lines(tmp_home):
    # The default template embeds explanatory comments. Editing one
    # value must not strip comments off the other lines.
    set_value("schedule.day", "monday")
    text = config_path().read_text(encoding="utf-8")
    assert "# 0-23 local time" in text
    assert "# MUST default true (Section 4.4)" in text


def test_set_two_keys_round_trip(tmp_home):
    # Sequential edits to two different sections should both persist.
    set_value("schedule.day", "monday")
    set_value("judge.primary_provider", "openai")
    data = _load(config_path())
    assert data["schedule"]["day"] == "monday"
    assert data["judge"]["primary_provider"] == "openai"


def test_set_two_keys_same_section_round_trip(tmp_home):
    set_value("schedule.day", "monday")
    set_value("schedule.hour", "9")
    data = _load(config_path())
    assert data["schedule"]["day"] == "monday"
    assert data["schedule"]["hour"] == 9
    assert data["schedule"]["minute"] == 0


def test_set_overwrites_existing_user_edit(tmp_home):
    set_value("schedule.day", "monday")
    set_value("schedule.day", "tuesday")
    assert _load(config_path())["schedule"]["day"] == "tuesday"


def test_set_inserts_missing_field_at_end_of_section(tmp_home):
    path = ensure_config_file()
    path.write_text(
        '[schedule]\nday = "monday"\n',
        encoding="utf-8",
    )
    set_value("schedule.hour", "9")
    data = _load(path)
    assert data["schedule"]["day"] == "monday"
    assert data["schedule"]["hour"] == 9


def test_set_appends_missing_section(tmp_home):
    path = ensure_config_file()
    # Strip out the [scan] section entirely so set_value must append it.
    path.write_text(
        '[schedule]\nday = "sunday"\n',
        encoding="utf-8",
    )
    set_value("scan.since_days", "14")
    data = _load(path)
    assert data["scan"]["since_days"] == 14
    assert data["schedule"]["day"] == "sunday"


def test_set_string_with_special_chars(tmp_home):
    # TOML basic strings need backslash and double-quote escaping; the
    # round-trip via tomllib confirms the writer produces valid TOML.
    set_value("notification.sound", 'ping "ding"')
    assert _load(config_path())["notification"]["sound"] == 'ping "ding"'


def test_set_persists_under_tomllib_reload(tmp_home):
    # After --set, load_config() returns the new value (US-006 loader
    # must still parse the file we wrote).
    from praxis.config import load_config

    set_value("schedule.day", "wednesday")
    assert load_config().schedule.day == "wednesday"


# ---- --set: invalid inputs --------------------------------------------------


def test_set_unknown_section_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        set_value("bogus.day", "monday")
    assert "Unknown section" in str(exc.value)


def test_set_unknown_field_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        set_value("schedule.dayz", "monday")
    assert "Unknown field" in str(exc.value)


def test_set_invalid_int_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        set_value("schedule.hour", "not-a-number")
    assert "Invalid int" in str(exc.value)


def test_set_invalid_bool_raises(tmp_home):
    with pytest.raises(ConfigCLIError) as exc:
        set_value("notification.enabled", "maybe")
    assert "Invalid bool" in str(exc.value)


def test_set_via_cli_no_equals_exits_nonzero(tmp_home, capsys):
    rc = main(["config", "--set", "schedule.day"])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "expected 'key=value'" in err


def test_set_via_cli_unknown_key_exits_nonzero(tmp_home, capsys):
    rc = main(["config", "--set", "bogus.day=monday"])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "Unknown section" in err


def test_set_via_cli_invalid_int_exits_nonzero(tmp_home, capsys):
    rc = main(["config", "--set", "schedule.hour=abc"])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "Invalid int" in err


def test_set_via_cli_happy_path(tmp_home, capsys):
    rc = main(["config", "--set", "schedule.day=monday"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert _load(config_path())["schedule"]["day"] == "monday"


# ---- bare `praxis config`: open in $EDITOR ----------------------------------


def test_open_editor_uses_env_editor(tmp_home, monkeypatch):
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return FakeResult()

    monkeypatch.setenv("EDITOR", "myedit")
    monkeypatch.setattr("praxis.config_cli.subprocess.run", fake_run)

    rc = open_editor()
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == "myedit"
    assert calls[0][1] == str(config_path())


def test_open_editor_returns_editor_returncode(tmp_home, monkeypatch):
    class FakeResult:
        returncode = 42

    monkeypatch.setenv("EDITOR", "myedit")
    monkeypatch.setattr(
        "praxis.config_cli.subprocess.run",
        lambda cmd, check=False: FakeResult(),
    )
    assert open_editor() == 42


def test_open_editor_falls_back_to_vi_when_env_unset(tmp_home, monkeypatch):
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(
        "praxis.config_cli.subprocess.run",
        lambda cmd, check=False: (calls.append(list(cmd)), FakeResult())[1],
    )
    open_editor()
    assert calls[0][0] == "vi"


def test_open_editor_missing_binary_raises(tmp_home, monkeypatch):
    def fake_run(cmd, check=False):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setenv("EDITOR", "definitely-not-an-editor")
    monkeypatch.setattr("praxis.config_cli.subprocess.run", fake_run)

    with pytest.raises(ConfigCLIError) as exc:
        open_editor()
    assert "Editor not found" in str(exc.value)


def test_bare_config_via_cli_launches_editor(tmp_home, monkeypatch, capsys):
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return FakeResult()

    monkeypatch.setenv("EDITOR", "myedit")
    monkeypatch.setattr("praxis.config_cli.subprocess.run", fake_run)

    rc = main(["config"])
    assert rc == 0
    capsys.readouterr()
    assert len(calls) == 1
    assert calls[0][0] == "myedit"
    assert calls[0][1].endswith("config.toml")


def test_bare_config_via_cli_propagates_editor_failure(tmp_home, monkeypatch, capsys):
    def fake_run(cmd, check=False):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setenv("EDITOR", "definitely-not-an-editor")
    monkeypatch.setattr("praxis.config_cli.subprocess.run", fake_run)

    rc = main(["config"])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "Editor not found" in err
