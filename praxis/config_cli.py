"""CLI helpers for ``praxis config``.

The :mod:`praxis.config` module owns the schema and the default file.
This module owns the read-modify-write logic for the dotted-key
``--get`` / ``--set`` operations and the ``$EDITOR`` launch path.

Splitting it out keeps ``cli/__main__.py`` a thin argparse shim and
makes the value-coercion / line-rewrite logic testable on its own.

The TOML rewriter is line-based on purpose: the spec's flat structure
has no arrays or nested tables, so a small regex pass preserves user
comments and other keys without pulling in a third-party serializer
(tomllib in stdlib is read-only).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import fields
from pathlib import Path
from typing import Any, get_type_hints

from praxis.config import (
    JudgeConfig,
    NotificationConfig,
    PrivacyConfig,
    ScanConfig,
    ScheduleConfig,
    config_path,
    ensure_config_file,
    load_config,
)

SECTION_TYPES: dict[str, type] = {
    "schedule": ScheduleConfig,
    "scan": ScanConfig,
    "judge": JudgeConfig,
    "notification": NotificationConfig,
    "privacy": PrivacyConfig,
}


class ConfigCLIError(Exception):
    """Raised when ``--get`` / ``--set`` receives a malformed or unknown input.

    The CLI catches this, prints :func:`str(exc)` to stderr, and exits
    with a non-zero code. Messages are user-facing -- they name the
    bad input and, where possible, list the valid alternatives.
    """


def _split_dotted_key(key: str) -> tuple[str, str]:
    """Validate ``section.field`` form; return ``(section, field)``.

    Raises :class:`ConfigCLIError` for any structural problem (missing
    or extra dots, empty parts, unknown section, unknown field).
    """
    if "." not in key:
        raise ConfigCLIError(
            f"Invalid key {key!r}: expected dotted form 'section.field' (e.g., schedule.day)."
        )
    parts = key.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ConfigCLIError(
            f"Invalid key {key!r}: expected exactly one '.' separating "
            f"section and field (e.g., schedule.day)."
        )
    section, field_name = parts
    if section not in SECTION_TYPES:
        valid = ", ".join(sorted(SECTION_TYPES.keys()))
        raise ConfigCLIError(f"Unknown section {section!r}. Valid sections: {valid}.")
    valid_fields = {f.name for f in fields(SECTION_TYPES[section])}
    if field_name not in valid_fields:
        valid = ", ".join(sorted(valid_fields))
        raise ConfigCLIError(
            f"Unknown field {field_name!r} in section {section!r}. Valid fields: {valid}."
        )
    return section, field_name


def _field_type(section: str, field_name: str) -> type:
    # get_type_hints() evaluates the forward references created by
    # ``from __future__ import annotations`` in praxis/config.py.
    return get_type_hints(SECTION_TYPES[section])[field_name]


def get_value(key: str, home: Path | None = None) -> str:
    """Return the current value at ``key`` formatted for stdout."""
    section, field_name = _split_dotted_key(key)
    cfg = load_config(home)
    return _format_value(getattr(getattr(cfg, section), field_name))


def _format_value(value: Any) -> str:
    # bool subclasses int in Python; check it first so True doesn't
    # render as "1". We mirror TOML's lowercase 'true'/'false' so the
    # round-trip print -> --set yields the same value.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def set_value(key: str, raw_value: str, home: Path | None = None) -> None:
    """Update one dotted key in ``config.toml``, preserving everything else.

    Other sections, other keys within the same section, and any
    comments on lines we are not editing all survive. The value is
    coerced to the field's declared type before writing.
    """
    section, field_name = _split_dotted_key(key)
    typed_value = _coerce(section, field_name, raw_value)
    path = ensure_config_file(home)
    text = path.read_text(encoding="utf-8")
    new_text = _rewrite_toml(text, section, field_name, typed_value)
    path.write_text(new_text, encoding="utf-8")


def _coerce(section: str, field_name: str, raw: str) -> Any:
    t = _field_type(section, field_name)
    if t is bool:
        low = raw.strip().lower()
        if low in {"true", "yes", "1", "on"}:
            return True
        if low in {"false", "no", "0", "off"}:
            return False
        raise ConfigCLIError(
            f"Invalid bool {raw!r} for {section}.{field_name}: expected true/false."
        )
    if t is int:
        try:
            return int(raw)
        except ValueError:
            raise ConfigCLIError(
                f"Invalid int {raw!r} for {section}.{field_name}: expected an integer."
            ) from None
    return raw


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_SECTION_HEADER = re.compile(r"^\s*\[\s*([^\]\s]+)\s*\]\s*(?:#.*)?$")


def _rewrite_toml(text: str, section: str, field_name: str, value: Any) -> str:
    """Replace ``field_name = ...`` under ``[section]`` with ``value``.

    The matched line is rewritten in full (any inline comment on that
    one line is dropped; all other lines are byte-identical). If the
    section is missing, append a new ``[section]`` block at the end of
    the file. If the section exists but the field is missing, insert
    the new line at the end of that section's block.
    """
    lines = text.splitlines(keepends=True)
    section_start: int | None = None
    section_end: int | None = None
    for i, line in enumerate(lines):
        m = _SECTION_HEADER.match(line.rstrip("\n"))
        if not m:
            continue
        name = m.group(1)
        if section_start is None:
            if name == section:
                section_start = i
        elif section_end is None:
            section_end = i
    if section_start is not None and section_end is None:
        section_end = len(lines)

    new_literal = _toml_literal(value)

    if section_start is None:
        prefix = "" if text == "" else ("" if text.endswith("\n") else "\n")
        separator = "\n" if text != "" else ""
        return f"{text}{prefix}{separator}[{section}]\n{field_name} = {new_literal}\n"

    field_re = re.compile(rf"^\s*{re.escape(field_name)}\s*=")
    assert section_end is not None
    for i in range(section_start + 1, section_end):
        if field_re.match(lines[i]):
            ends_with_newline = lines[i].endswith("\n")
            lines[i] = f"{field_name} = {new_literal}" + ("\n" if ends_with_newline else "")
            return "".join(lines)

    insert_at = section_end
    while insert_at > section_start + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    new_line = f"{field_name} = {new_literal}\n"
    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        new_line = "\n" + new_line
    lines.insert(insert_at, new_line)
    return "".join(lines)


def open_editor(home: Path | None = None) -> int:
    """Launch ``$EDITOR`` on ``config.toml`` and return its exit code.

    Falls back to ``vi`` when ``$EDITOR`` is unset, since vi is on
    every POSIX system Praxis runs on. Raises :class:`ConfigCLIError`
    if the chosen editor isn't installed, so the CLI surfaces a clear
    message instead of a Python traceback.
    """
    path = ensure_config_file(home)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        result = subprocess.run([editor, str(path)], check=False)
    except FileNotFoundError as exc:
        raise ConfigCLIError(
            f"Editor not found: {editor!r}. Set $EDITOR to an installed editor."
        ) from exc
    return result.returncode


__all__ = [
    "SECTION_TYPES",
    "ConfigCLIError",
    "config_path",
    "get_value",
    "open_editor",
    "set_value",
]
