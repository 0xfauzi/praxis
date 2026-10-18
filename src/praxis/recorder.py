"""What one hook event does to the ledger. Design: docs/design/01-recorder.md.

Every function here takes the payload as untrusted structure. Field presence is checked
once, at the top of ``handle``, and anything missing makes the event unobservable rather
than guessed at. ``cwd`` is passed to git and never otherwise opened or followed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from praxis.gitdiff import Snap, Unobservable, repo_id, snapshot
from praxis.store import Attribution, Files, Store

MUTATING_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})
BOUNDARY_EVENTS = frozenset({"UserPromptSubmit", "Stop"})

ASSUMPTIONS = [
    "content-identical lines from two authors are indistinguishable; multiset counts"
    " split them by count, not by author",
    "a change made between a PreToolUse and its PostToolUse is attributed to the agent",
    "lines are compared relative to HEAD; committed lines stop being changed lines",
]


@dataclass(frozen=True)
class Result:
    outcome: str  # ok | unobservable
    note: str | None = None


def _str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def handle(payload: dict[str, Any], store: Store, now: str) -> Result:
    event = _str(payload, "hook_event_name")
    session = _str(payload, "session_id")
    cwd = _str(payload, "cwd")
    if event is None or session is None:
        return Result("unobservable", "payload lacks hook_event_name or session_id")
    if cwd is None:
        return Result("unobservable", "payload lacks cwd")
    tool = _str(payload, "tool_name")
    tool_use_id = _str(payload, "tool_use_id")

    if event == "SessionStart":
        return _session_start(session, cwd, store, now)
    if event == "PreToolUse":
        return _pre_tool(session, cwd, tool, tool_use_id, store, now)
    if event in ("PostToolUse", "PostToolUseFailure"):
        return _post_tool(session, cwd, tool, tool_use_id, store, now)
    if event in BOUNDARY_EVENTS:
        return _boundary(session, cwd, event, store, now)
    return Result("ok", "event not used by the recorder")


def _snap(session: str, cwd: str, store: Store, now: str) -> Snap | Unobservable:
    """One snapshot, and the session-to-repository link the instrument reads later."""
    snap = snapshot(cwd)
    if not isinstance(snap, Unobservable):
        store.touch_session_repo(session, repo_id(snap.toplevel), now)
    return snap


def _session_start(session: str, cwd: str, store: Store, now: str) -> Result:
    if store.has_baseline(session):
        return Result("ok", "baseline kept from an earlier SessionStart")
    snap = _snap(session, cwd, store, now)
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason)
    store.baseline(session, snap.files)
    return Result(
        "ok", f"baseline: {_count(snap.files)} changed lines in {len(snap.files)} files"
    )


def _pre_tool(
    session: str,
    cwd: str,
    tool: str | None,
    tool_use_id: str | None,
    store: Store,
    now: str,
) -> Result:
    if tool not in MUTATING_TOOLS:
        return Result("ok", "not a mutating tool")
    if tool_use_id is None:
        return Result("unobservable", "payload lacks tool_use_id")
    snap = _snap(session, cwd, store, now)
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason)
    store.pending_put(session, tool_use_id, now, snap.files)
    return Result("ok", "pre snapshot stored")


def _post_tool(
    session: str,
    cwd: str,
    tool: str | None,
    tool_use_id: str | None,
    store: Store,
    now: str,
) -> Result:
    if tool not in MUTATING_TOOLS:
        return Result("ok", "not a mutating tool")
    if tool_use_id is None:
        return Result("unobservable", "payload lacks tool_use_id")
    pre = store.pending_take(session, tool_use_id)
    if pre is None:
        return Result("unobservable", "no pre snapshot for this tool_use_id")
    snap = _snap(session, cwd, store, now)
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason)
    changes = _delta(pre, snap.files)
    if changes:
        store.agent_delta(session, tool_use_id, tool, now, changes)
    return Result("ok", f"{len(changes)} line changes by {tool}")


def _delta(pre: Files, post: Files) -> list[tuple[str, str, int]]:
    changes: list[tuple[str, str, int]] = []
    for file in sorted(set(pre) | set(post)):
        before = pre.get(file, Counter())
        after = post.get(file, Counter())
        changes.extend(
            (file, h, after[h] - before[h])
            for h in sorted(set(before) | set(after))
            if after[h] != before[h]
        )
    return changes


def _boundary(session: str, cwd: str, trigger: str, store: Store, now: str) -> Result:
    snap = _snap(session, cwd, store, now)
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason)
    baseline = store.baseline_of(session)
    agent, sources = store.agent_multiset(session)
    rows = attribute(session, now, trigger, snap, baseline, agent, sources)
    if rows:
        store.attributions(rows)
    return Result("ok", f"{len(rows)} files attributed at {trigger}")


@dataclass(frozen=True)
class _Split:
    """One file's lines at a boundary, divided. ``unmatched`` is human when a baseline
    exists and unknown otherwise; the caller decides which."""

    agent: int
    unmatched: int
    preexisting: int
    ambiguous: int
    reverted: int


def _split(
    present: Counter[str], mine: Counter[str], before: Counter[str] | None
) -> _Split:
    agent_n = unmatched_n = pre_n = 0
    consumed: Counter[str] = Counter()
    for h, n in present.items():
        a = min(n, mine[h])
        agent_n += a
        rem = n - a
        if before is not None:
            p = min(rem, before[h])
            consumed[h] = p
            pre_n += p
            rem -= p
        unmatched_n += rem
    ambiguous = sum(max(0, n - present[h]) for h, n in mine.items())
    reverted = (
        0 if before is None else sum(max(0, n - consumed[h]) for h, n in before.items())
    )
    return _Split(agent_n, unmatched_n, pre_n, ambiguous, reverted)


def attribute(
    session: str,
    now: str,
    trigger: str,
    snap: Snap,
    baseline: Files | None,
    agent: Files,
    sources: dict[str, list[str]],
) -> list[Attribution]:
    # A skipped file gets a row too: zero counts under coverage partial, with the reason
    # on warnings, so that "nothing attributed" and "not looked at" stay different rows.
    skipped_files = {s.split(":", 1)[0] for s in snap.skipped}
    files = (
        set(snap.files)
        | set(agent)
        | (set(baseline) if baseline else set())
        | skipped_files
    )
    rows: list[Attribution] = []
    for file in sorted(files):
        present = snap.files.get(file, Counter())
        mine = agent.get(file, Counter())
        before = baseline.get(file, Counter()) if baseline is not None else None
        warnings = [s for s in snap.skipped if s.startswith(f"{file}:")]
        if not (present or mine or before or warnings):
            continue
        split = _split(present, mine, before)
        rows.append(
            _row(
                session,
                now,
                trigger,
                file,
                split,
                before is not None,
                warnings,
                sources.get(file, []),
            )
        )
    return rows


def _row(
    session: str,
    now: str,
    trigger: str,
    file: str,
    split: _Split,
    has_baseline: bool,
    warnings: list[str],
    tool_use_ids: list[str] | None = None,
) -> Attribution:
    if not has_baseline:
        coverage = "no_baseline"
    elif warnings:
        coverage = "partial"
    else:
        coverage = "full"
    sources = [f"snapshot:{trigger}:{now}", *(tool_use_ids or [])]
    if has_baseline:
        sources.append(f"baseline:{session}")
    return Attribution(
        session_id=session,
        ts=now,
        trigger=trigger,
        file=file,
        agent=split.agent,
        human=split.unmatched if has_baseline else None,
        preexisting=split.preexisting if has_baseline else None,
        ambiguous=split.ambiguous,
        reverted=split.reverted if has_baseline else None,
        coverage=coverage,
        sources=sources,
        assumptions=ASSUMPTIONS,
        warnings=warnings,
    )


def _count(files: Files) -> int:
    return sum(sum(c.values()) for c in files.values())
