"""``praxis hook``: the one command every hook event runs. It exits 0 on every input.

AGENTS.md invariant 7. This process runs inside the owner's agent session. Whatever
happens here (bad JSON, no repository, git hanging, a full disk) is recorded in the
ledger when it can be and is otherwise dropped. The exit code is 0 so the tool call it
wraps is unaffected, and stdout stays empty, so nothing reaches the agent's context,
with one exception: at the Claude Code Stop that closes a registered task's segment it
prints exactly ``{"terminalSequence": "\\u001b]9;praxis: feedback ready\\u0007"}``,
which Claude Code writes to the owner's terminal and never to the agent (design 5.4).
That Stop hook must be synchronous: an async hook's JSON fields reach the model.
"""

from __future__ import annotations

import datetime as dt
import json
import signal
import sys
import time
from typing import TYPE_CHECKING, Any, BinaryIO, TextIO

from praxis import recorder, store

if TYPE_CHECKING:
    from pathlib import Path

    from praxis.schema import Pointers


# A hook payload is a few KB; the recorded Edit body in fixtures/ is under 4 KB.
# Anything past this is not a payload we understand; reading it would only cost time.
STDIN_LIMIT = 4_000_000
# Belt over the 2 s git timeouts: the whole hook gives up well inside Claude Code's and
# Codex's 600 s defaults, and far inside the 10 s the printed snippet sets.
WATCHDOG_S = 20


class _Watchdog(Exception):
    pass


def _alarm(_signum: int, _frame: object) -> None:
    raise _Watchdog


def run(stdin: BinaryIO, praxis_home: Path) -> int:
    started = time.perf_counter()
    now = (
        dt.datetime.now(dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    session: str | None = None
    event: str | None = None
    tool: str | None = None
    tool_use_id: str | None = None
    ledger: store.Store | None = None
    outcome = "error"
    note: str | None = None
    marks: Pointers = {}
    head: str | None = None
    previous = None
    try:
        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(WATCHDOG_S)
        if stdin.isatty():
            sys.stderr.write("praxis hook reads a hook payload on stdin\n")
            return 0
        raw = stdin.read(STDIN_LIMIT + 1)
        ledger = store.Store(praxis_home / "ledger.db").open()
        if len(raw) > STDIN_LIMIT:
            outcome, note = "unobservable", "payload over size limit"
        else:
            payload = _payload(raw)
            if payload is None:
                outcome, note = "unobservable", "payload is not a JSON object"
            else:
                session = _field(payload, "session_id")
                event = _field(payload, "hook_event_name")
                tool = _field(payload, "tool_name")
                tool_use_id = _field(payload, "tool_use_id")
                marks = recorder.pointers(payload)
                result = recorder.handle(payload, ledger, now)
                outcome, note = result.outcome, result.note
                head = result.head_sha
                if result.notice:
                    _notify(sys.stdout)
    except _Watchdog:
        outcome, note = "error", f"watchdog: hook exceeded {WATCHDOG_S} s"
    except BaseException as exc:  # fail open is the contract; see the module docstring
        outcome, note = "error", f"{type(exc).__name__}: {exc}"[:200]
    finally:
        signal.alarm(0)
        if previous is not None:
            signal.signal(signal.SIGALRM, previous)
        _record(
            ledger,
            praxis_home,
            session,
            event,
            tool,
            tool_use_id,
            now,
            started,
            outcome,
            note,
            marks,
            head,
        )
    return 0


def _notify(out: TextIO) -> None:
    out.write(json.dumps({"terminalSequence": recorder.NOTICE}))
    out.flush()


def _record(
    ledger: store.Store | None,
    praxis_home: Path,
    session: str | None,
    event: str | None,
    tool: str | None,
    tool_use_id: str | None,
    now: str,
    started: float,
    outcome: str,
    note: str | None,
    marks: Pointers,
    head: str | None,
) -> None:
    try:
        if ledger is None:
            ledger = store.Store(praxis_home / "ledger.db").open()
        ledger.hook_event(
            session_id=session,
            event=event,
            tool_name=tool,
            tool_use_id=tool_use_id,
            ts=now,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            outcome=outcome,
            note=note,
            head_sha=head,
            **marks,
        )
        ledger.close()
    except Exception:  # there is nowhere left to report to
        return


def _payload(raw: bytes) -> dict[str, Any] | None:
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None
