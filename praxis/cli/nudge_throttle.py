"""Throttle state for ``praxis nudge``.

The same commitment cue can fire from many surfaces -- a Claude Code
SessionStart hook, a Codex SessionStart hook, the shell-startup nudge --
and a single human action (opening a new project window) can trip
several at once. Without dedup the user sees the same cue three times
within a few seconds, which collapses the signal-to-noise ratio of the
commitment and ultimately trains the user to ignore Praxis.

`~/.praxis/.last_nudge` is a small JSON file keyed by ``f"{surface}:
{sha1(cwd)}"`` whose value is the ISO-8601 timestamp of the most recent
fire for that (surface, project) pair. Before printing anything,
``praxis nudge`` checks whether the prior fire for the current
(surface, cwd) is within ``[nudge].throttle_minutes`` (default 30; see
``praxis/config.py``). If yes, the command returns empty without ever
opening the DB -- AC US-018 #2.

Recovery contract for a corrupt file (AC US-018 #3): if the JSON cannot
be parsed (or is not an object), the file is renamed to
``.last_nudge.corrupt`` so the user can inspect it, and the current
call proceeds as if no prior fire had been recorded. We never crash
the hot path on bad disk state.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from praxis.storage.profile_store import resolve_home

# File names are intentionally not configurable: the spec (PRD US-018
# AC #1) names ``.last_nudge`` literally and the corrupt sibling
# follows the documented `.last_nudge.corrupt` convention so a user
# inspecting `~/.praxis/` can recognise both at a glance.
_THROTTLE_FILENAME = ".last_nudge"
_CORRUPT_FILENAME = ".last_nudge.corrupt"


def throttle_file_path(home: Path | None = None) -> Path:
    """Return the canonical ``~/.praxis/.last_nudge`` path."""
    base = home if home is not None else resolve_home()
    return base / _THROTTLE_FILENAME


def corrupt_file_path(home: Path | None = None) -> Path:
    """Return the sibling ``~/.praxis/.last_nudge.corrupt`` quarantine path."""
    base = home if home is not None else resolve_home()
    return base / _CORRUPT_FILENAME


def _cwd_hash(cwd: str | None = None) -> str:
    """Return ``sha1(cwd)`` so the on-disk key never leaks the path.

    Two reasons we hash rather than store the raw path: privacy (a user
    backing up ``~/.praxis`` to a public location shouldn't expose
    every project directory they've used Praxis in), and key length
    (file system paths can be long and contain characters that need
    escaping in JSON-key strings).
    """
    value = cwd if cwd is not None else os.getcwd()
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _throttle_key(surface: str, cwd: str | None = None) -> str:
    """Return the JSON key for a (surface, cwd) pair: ``"{surface}:{sha1}"``."""
    return f"{surface}:{_cwd_hash(cwd)}"


def _load_state(path: Path) -> dict[str, str]:
    """Return the throttle state dict, recovering from a corrupt file.

    Returns ``{}`` when the file is missing, empty, malformed, or not
    a JSON object. In the malformed/non-object case the file is renamed
    to ``.last_nudge.corrupt`` first so the user can inspect what went
    wrong (and so the next ``record_fire`` writes a clean file).
    """
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _quarantine_corrupt_file(path)
        return {}
    if not isinstance(data, dict):
        # Valid JSON but the wrong shape (e.g. a list); quarantine too
        # so the user notices the schema drift instead of silently
        # losing fires.
        _quarantine_corrupt_file(path)
        return {}
    # Keys must be strings (json.loads always returns str keys) and the
    # spec stores ISO-8601 timestamps as values; we don't validate the
    # timestamp shape here -- `is_throttled` is tolerant of unparseable
    # values and treats them as "no prior fire", which is the safe
    # default for a poisoned entry.
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def _quarantine_corrupt_file(path: Path) -> None:
    """Rename a malformed throttle file to ``.last_nudge.corrupt``.

    Best-effort: if the rename fails (e.g. read-only mount) the next
    ``record_fire`` will overwrite the file with valid JSON anyway, so
    we never raise on disk errors during the throttle hot path.
    """
    target = path.with_name(_CORRUPT_FILENAME)
    try:
        if target.exists():
            target.unlink()
        path.rename(target)
    except OSError:
        pass


def is_throttled(
    surface: str,
    *,
    throttle_minutes: int,
    cwd: str | None = None,
    home: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True if the prior fire for (surface, cwd) is within the window.

    ``throttle_minutes <= 0`` disables throttling -- the function always
    returns False, so an admin can short-circuit the cue dedup logic by
    setting ``[nudge] throttle_minutes = 0`` in ``config.toml``. A
    missing file or missing key for this (surface, cwd) likewise
    returns False (nothing to throttle against). Unparseable timestamp
    values are treated as "no prior fire" so a poisoned entry never
    locks the user out forever.
    """
    if throttle_minutes <= 0:
        return False
    path = throttle_file_path(home)
    state = _load_state(path)
    key = _throttle_key(surface, cwd)
    last_fire_str = state.get(key)
    if not last_fire_str:
        return False
    try:
        last_fire = datetime.fromisoformat(last_fire_str)
    except ValueError:
        return False
    if last_fire.tzinfo is None:
        # Older entries (or external tooling) may have written naive
        # timestamps; assume UTC so comparisons are at least consistent.
        last_fire = last_fire.replace(tzinfo=timezone.utc)
    current = now if now is not None else datetime.now(timezone.utc)
    return (current - last_fire) < timedelta(minutes=throttle_minutes)


def record_fire(
    surface: str,
    *,
    cwd: str | None = None,
    home: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Update the throttle state with a fresh fire timestamp for (surface, cwd).

    Writes the JSON file with ``sort_keys=True`` so concurrent re-reads
    from a user inspecting the file see deterministic ordering. The
    ``~/.praxis`` parent dir is created if missing (the
    ``ensure_config_file`` call in CLI ``main`` already does this, but
    we re-do it here so the function is callable from tests that bypass
    the CLI entry point).
    """
    path = throttle_file_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state(path)
    key = _throttle_key(surface, cwd)
    current = now if now is not None else datetime.now(timezone.utc)
    state[key] = current.isoformat()
    # Atomic write so a concurrent reader -- or another surface's near-
    # simultaneous record_fire, the exact multi-surface case this module
    # dedupes -- never observes a half-written file. The per-pid temp keeps
    # os.replace atomic on POSIX without two writers clobbering one tempfile;
    # last replace wins, which is the intended best-effort dedup semantics.
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(
        json.dumps(state, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)
