"""What one hook event does to the ledger. Design: docs/design/01-recorder.md and
docs/design/03-measurement.md section 5.

Every function here takes the payload as untrusted structure. Field presence is checked
once, at the top of ``handle``, and anything missing makes the event unobservable rather
than guessed at. ``cwd`` is passed to git and never otherwise opened or followed. No
prompt, tool input or response is stored: a decision keeps an option count, an index and
a hash of the labels; an edge keeps a path and a component id.

A registered task binds to the next UserPromptSubmit in its repository and its scored
segment closes at the first Stop of the bound session with an agent delta since the
bind. Prompts in between do not end the segment (design 5.3); they and answered agent
questions are flagged ``owner_answered_question``.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from praxis.gitdiff import Snap, Unobservable, repo_id, snapshot
from praxis.schema import Pointers
from praxis.store import Attribution, Evidence, Files, Store

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    from praxis.components import ComponentMap
    from praxis.extract import Edge

# Codex reports its file edits as apply_patch (design 5.1).
MUTATING_TOOLS = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "apply_patch"}
)
DECISION_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
# The one string a hook ever prints (design 5.4): fixed, nothing variable escaped in.
NOTICE = "\x1b]9;praxis: result ready\x07"
EDGES_REDUCER = "recorder-edges-v1"
OUTCOME_REDUCER = "segment-outcome-v1"
DECISION_REDUCER = "decision-pointer-v1"

ASSUMPTIONS = [
    "content-identical lines from two authors are indistinguishable; multiset counts"
    " split them by count, not by author",
    "a change made between a PreToolUse and its PostToolUse is attributed to the agent",
    "lines are compared with the session's base commit; a commit moves no line, and a"
    " HEAD outside the base's history makes the boundary unobservable",
]
EDGE_ASSUMPTIONS = [
    "an absent edge may be an extractor blind spot (dynamic, configuration or SQL"
    " wiring)",
    "file grain: a file is scanned at a Stop when an agent delta touched it since the"
    " session's previous Stop; a TYPE_CHECKING import into the file's own component"
    " counts as a blind spot",
]
OUTCOME_ASSUMPTIONS = [
    "o_first: a component is touched when an agent delta since the bound prompt changed"
    " a file mapped to it",
    "o_final is NULL at the segment close: it needs the session's last boundary",
    "edges compare each touched Python file's scan at this Stop with its latest"
    " earlier scan; owner edits since that scan count as the segment's",
]
DECISION_ASSUMPTIONS = [
    "one question, multiSelect false, string labels, or nothing is counted",
    "ExitPlanMode is two options, approve 0 and reject 1; a PostToolUse is an approval",
]


@dataclass(frozen=True)
class Result:
    outcome: str  # ok | unobservable
    note: str | None = None
    head_sha: str | None = None
    notice: bool = False


@dataclass(frozen=True)
class _Event:
    name: str
    session: str
    cwd: str
    tool: str | None
    tool_use_id: str | None
    pointers: Pointers
    payload: dict[str, Any]


def _str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def pointers(payload: dict[str, Any]) -> Pointers:
    """The ids that join a hook row to its transcript entry or Codex turn. Claude Code
    2.1.303 carries prompt_id, permission_mode and, in a subagent, agent_id; Codex
    carries turn_id. Neither carries the CLI version, so cli_version stays NULL."""
    return Pointers(
        prompt_id=_str(payload, "prompt_id"),
        turn_id=_str(payload, "turn_id"),
        permission_mode=_str(payload, "permission_mode"),
        agent_id=_str(payload, "agent_id"),
    )


def handle(payload: dict[str, Any], store: Store, now: str) -> Result:
    event = _str(payload, "hook_event_name")
    session = _str(payload, "session_id")
    cwd = _str(payload, "cwd")
    if event is None or session is None:
        return Result("unobservable", "payload lacks hook_event_name or session_id")
    if cwd is None:
        return Result("unobservable", "payload lacks cwd")
    ev = _Event(
        event,
        session,
        cwd,
        _str(payload, "tool_name"),
        _str(payload, "tool_use_id"),
        pointers(payload),
        payload,
    )
    if event == "SessionStart":
        return _session_start(ev, store, now)
    if event == "PreToolUse":
        return _pre_tool(ev, store, now)
    if event in ("PostToolUse", "PostToolUseFailure"):
        return _post_tool(ev, store, now)
    if event == "UserPromptSubmit":
        return _prompt(ev, store, now)
    if event == "Stop":
        return _stop(ev, store, now)
    return Result("ok", "event not used by the recorder")


def _snap(ev: _Event, store: Store, now: str, base: str | None) -> Snap | Unobservable:
    """One snapshot, and the session-to-repository link the instrument reads later."""
    snap = snapshot(ev.cwd, base)
    if snap.toplevel is not None:
        store.touch_session_repo(ev.session, repo_id(snap.toplevel), now)
    return snap


def _session_start(ev: _Event, store: Store, now: str) -> Result:
    if store.has_baseline(ev.session):
        return Result("ok", "baseline kept from an earlier SessionStart")
    snap = _snap(ev, store, now, None)
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason)
    store.baseline(ev.session, snap.files)
    note = f"baseline: {_count(snap.files)} changed lines in {len(snap.files)} files"
    return Result("ok", note, snap.head)


def _pre_tool(ev: _Event, store: Store, now: str) -> Result:
    if ev.tool not in MUTATING_TOOLS:
        return Result("ok", "not a mutating tool")
    if ev.tool_use_id is None:
        return Result("unobservable", "payload lacks tool_use_id")
    snap = _snap(ev, store, now, store.session_base(ev.session))
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason)
    store.pending_put(ev.session, ev.tool_use_id, now, snap.files)
    return Result("ok", "pre snapshot stored", snap.head)


def _post_tool(ev: _Event, store: Store, now: str) -> Result:
    if ev.tool in DECISION_TOOLS and ev.name == "PostToolUse":
        return _decision(ev, store, now)
    if ev.tool not in MUTATING_TOOLS or ev.tool is None:
        return Result("ok", "not a mutating tool")
    if ev.tool_use_id is None:
        return Result("unobservable", "payload lacks tool_use_id")
    pre = store.pending_take(ev.session, ev.tool_use_id)
    if pre is None:
        return Result("unobservable", "no pre snapshot for this tool_use_id")
    snap = _snap(ev, store, now, store.session_base(ev.session))
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason)
    changes = _delta(pre, snap.files)
    if changes:
        store.agent_delta(
            ev.session, ev.tool_use_id, ev.tool, now, changes, **ev.pointers
        )
    return Result("ok", f"{len(changes)} line changes by {ev.tool}", snap.head)


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


# -- decisions ------------------------------------------------------------------------

Shape = tuple[int, int | None, str]


def _decision(ev: _Event, store: Store, now: str) -> Result:
    if ev.tool is None or ev.tool_use_id is None:
        return Result("unobservable", "payload lacks tool_use_id")
    shape = decision_shape(ev.tool, ev.payload)
    count, chosen, digest = shape or (None, None, None)
    if shape is None:
        coverage, warnings = "unobservable", ["decision_unscorable"]
    else:
        coverage, warnings = ("full", []) if chosen is not None else (
            "partial", ["chosen_unknown"])  # fmt: skip
    evidence = Evidence(
        coverage,
        [f"hook:PostToolUse:{ev.session}:{ev.tool_use_id}"],
        DECISION_REDUCER,
        DECISION_ASSUMPTIONS,
        warnings,
    )
    store.decision(
        ev=evidence,
        session_id=ev.session,
        tool_use_id=ev.tool_use_id,
        tool_name=ev.tool,
        prompt_id=ev.pointers.get("prompt_id"),
        ts=now,
        option_count=count,
        chosen_index=chosen,
        options_hash=digest,
    )
    flagged = _flag_answer(ev, store, now, {"tool_use_id": ev.tool_use_id})
    return Result("ok", f"decision recorded{flagged}")


def decision_shape(tool: str, payload: dict[str, Any]) -> Shape | None:
    """(option count, chosen index or None, options hash), transcript.py's rule: None
    when the call's shape cannot be scored."""
    tool_input = payload.get("tool_input")
    response = payload.get("tool_response")
    if not isinstance(tool_input, dict):
        return None
    from praxis.transcript import options_hash  # off the path of every other hook

    if tool == "ExitPlanMode":
        plan = _str(tool_input, "plan")
        if plan is None and isinstance(response, dict):
            plan = _str(response, "plan")
        return None if plan is None else (2, 0, options_hash([plan]))
    asked = _single_select(tool_input)
    if asked is None:
        return None
    question, labels = asked
    answers = _answers(tool_input, response)
    answer = answers.get(question.get("question")) if answers is not None else None
    chosen = labels.index(answer) if labels.count(answer) == 1 else None
    return len(labels), chosen, options_hash([str(label) for label in labels])


def _single_select(tool_input: dict[str, Any]) -> tuple[Any, list[Any]] | None:
    """The one question and its option labels, when the call asks exactly one
    single-select question whose every option has a string label; else None."""
    questions = tool_input.get("questions")
    if not (isinstance(questions, list) and len(questions) == 1):
        return None
    question = questions[0]
    options = question.get("options") if isinstance(question, dict) else None
    if not isinstance(options, list) or question.get("multiSelect") is not False:
        return None
    labels = [o.get("label") if isinstance(o, dict) else None for o in options]
    if not labels or not all(isinstance(label, str) for label in labels):
        return None
    return question, labels


def _answers(tool_input: dict[str, Any], response: object) -> dict[str, Any] | None:
    """The response's answers, else answers a hook supplied on the input."""
    for source in (response, tool_input):
        found = source.get("answers") if isinstance(source, dict) else None
        if isinstance(found, dict):
            return found
    return None


# -- the scored segment ---------------------------------------------------------------


def _flag_answer(ev: _Event, store: Store, now: str, detail: dict[str, object]) -> str:
    seg = store.task_bound_to(ev.session)
    if seg is None:
        return ""
    store.flag(
        ts=now,
        code="owner_answered_question",
        detail={"event": ev.name, **detail},
        repo_id=seg["repo_id"],
        session_id=ev.session,
        task_id=seg["task_id"],
    )
    return "; owner_answered_question"


def _prompt(ev: _Event, store: Store, now: str) -> Result:
    result, snap = _boundary(ev, store, now, "UserPromptSubmit")
    prompt_id = ev.pointers.get("prompt_id")
    seg = store.task_bound_to(ev.session)
    if seg is not None:
        if seg["bound_prompt_id"] is None or seg["bound_prompt_id"] != prompt_id:
            tail = _flag_answer(ev, store, now, {"prompt_id": prompt_id})
            return _noted(result, tail.removeprefix("; "))
        return result
    if snap.toplevel is None:
        return result
    task = store.task_open(repo_id(snap.toplevel))
    if task is None or task["status"] != "unbound" or not _earlier(task["ts"], now):
        return result
    store.task_bind(task["task_id"], ev.session, prompt_id, now)
    return _noted(result, "task bound")


def _earlier(registered: str, now: str) -> bool:
    """A task binds only to a prompt after it (G8). ``praxis start task`` writes its own
    timestamp, so both are parsed rather than compared as strings."""
    try:
        return dt.datetime.fromisoformat(registered) < dt.datetime.fromisoformat(now)
    except (TypeError, ValueError):
        return False


def _stop(ev: _Event, store: Store, now: str) -> Result:
    result, snap = _boundary(ev, store, now, "Stop")
    seg = store.task_bound_to(ev.session)
    if snap.toplevel is None:
        return result
    repo = repo_id(snap.toplevel)
    if seg is not None and seg["repo_id"] != repo:
        seg = None
    if isinstance(snap, Unobservable):
        if seg is not None and snap.reason == "head_not_descendant":
            store.task_void(seg["task_id"], "head_not_descendant", now)
            return _noted(result, "task void: head_not_descendant")
        return result
    since = store.last_stop_ts(ev.session)
    scan_files = _claimed(store.deltas_files_since(ev.session, since))
    if not scan_files and seg is None:
        return result
    cmap = _frozen_map(store, repo)
    if cmap is None:
        return _noted(result, "edges: no_frozen_map")
    after, note = _edges(ev, store, now, snap, cmap, scan_files)
    result = _noted(result, note)
    if seg is None:
        return result
    return _close(ev, store, now, seg, cmap, after, result)


def _noted(result: Result, note: str) -> Result:
    joined = note if not result.note else f"{result.note}; {note}"
    return Result(result.outcome, joined, result.head_sha, result.notice)


def _claimed(files: list[str]) -> list[str]:
    """The files a registered language reads, chosen by suffix with no reader loaded."""
    from praxis.lang import claimant  # off the path of every other hook

    return [f for f in files if claimant(f) is not None]


def _frozen_map(store: Store, repo: str) -> ComponentMap | None:
    frozen = store.component_map_frozen(repo)
    if frozen is None:
        return None
    from praxis.components import from_json  # off the path of every other hook

    return from_json(frozen)


@dataclass
class _Scan:
    """One Stop's X1 at file grain. ``after`` maps a touched Python file to the
    component ids it imports, or to None when it could not be parsed."""

    scanned: dict[str, str]
    edges: list[tuple[str, str, int, bool]]
    after: dict[str, frozenset[str] | None]
    warnings: list[str]


def _edges(
    ev: _Event, store: Store, now: str, snap: Snap, cmap: ComponentMap, files: list[str]
) -> tuple[dict[str, frozenset[str] | None], str]:
    if not files:
        return {}, "edges: none touched"
    try:
        scan = scan_files(Path(snap.toplevel), cmap, files)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return {}, "edges: unobservable"
    if scan.scanned:
        evidence = Evidence(
            "partial" if scan.warnings else "full",
            [f"snapshot:Stop:{now}", f"session:{ev.session}"],
            EDGES_REDUCER,
            EDGE_ASSUMPTIONS,
            scan.warnings,
        )
        store.file_edges(
            scanned=scan.scanned,
            edges=scan.edges,
            ev=evidence,
            session_id=ev.session,
            repo_id=cmap.repo_id,
            map_version=cmap.version,
            ts=now,
            extractor_version=_extractor_version(),
        )
    return scan.after, f"edges: {len(scan.edges)} in {len(scan.scanned)} files"


def _extractor_version() -> str:
    from praxis.extract import EXTRACTOR_VERSION

    return EXTRACTOR_VERSION


def scan_files(root: Path, cmap: ComponentMap, files: list[str]) -> _Scan:
    """X1 over ``files`` at file grain, against the whole working tree. A listed file
    that is gone imports nothing; one that does not parse is unknown."""
    from praxis.components import assign

    present = [f for f in files if (root / f).is_file()]
    found, blind, failed = _file_graph(root, cmap, present)
    scan = _Scan({}, [], {f: frozenset() for f in files if f not in present}, [])
    for f in present:
        if f in failed:
            scan.after[f] = None
            scan.warnings.append(f"parse_failed:{f}")
            continue
        own = assign(cmap, f)
        mine = [(f, e.dst, e.sites, e.module_level) for e in found.get(f, [])
                if e.dst != own]  # fmt: skip
        scan.scanned[f] = own
        scan.edges += mine
        scan.after[f] = frozenset(d for _, d, _, _ in mine)
        if blind.get(f):
            scan.warnings.append(f"blind_spots:{f}:{blind[f]}")
    return scan


def _file_graph(
    root: Path, cmap: ComponentMap, present: list[str]
) -> tuple[dict[str, list[Edge]], dict[str, int], frozenset[str]]:
    """Each scanned file is its own node and every other path its component. A file
    that imports another scanned file is scanned again alone, so that one statement
    naming two files of one component still counts one site."""
    from praxis.components import assign
    from praxis.extract import static_imports

    node = {f: "\0" + f for f in present}
    path_of = {v: k for k, v in node.items()}

    def lift(only: str | None = None) -> Callable[[str], str]:
        def place(p: str) -> str:
            mine = p in node and only in (None, p)
            return node[p] if mine else assign(cmap, p)

        return place

    graph = static_imports(root, lift(), present)
    alone = {path_of[e.src] for e in graph.edges if e.dst in path_of}
    found: dict[str, list[Edge]] = {}
    for e in graph.edges:
        if path_of[e.src] not in alone:
            found.setdefault(path_of[e.src], []).append(e)
    blind = {path_of[k]: n for k, n in graph.blind_spots.items() if k in path_of}
    for f in sorted(alone):
        one = static_imports(root, lift(f), [f])
        found[f] = list(one.edges)
        blind[f] = one.blind_spots.get(node[f], 0)
    return found, blind, frozenset(graph.parse_failures)


def _close(
    ev: _Event,
    store: Store,
    now: str,
    seg: sqlite3.Row,
    cmap: ComponentMap,
    after: dict[str, frozenset[str] | None],
    result: Result,
) -> Result:
    touched = store.deltas_files_since(ev.session, str(seg["bound_ts"]))
    if not touched:
        return result
    task_id = str(seg["task_id"])
    if cmap.version != seg["map_version"]:
        store.task_void(task_id, "map_changed", now)
        return _noted(result, "task void: map_changed")
    from praxis.components import assign, product_components

    before = _history(store, cmap, _claimed(touched), now)
    rows = []
    warnings = _gaps(store, ev.session, str(seg["bound_ts"]))
    for c in product_components(cmap):
        mine = [f for f in touched if assign(cmap, f) == c]
        added, removed = _edge_change(_claimed(mine), before, after)
        rows.append((c, bool(mine), None, added, removed))
        if added is None:
            warnings.append(f"edges_unknown:{c}")
    evidence = Evidence(
        "partial" if "recorder_gap" in warnings else "full",
        [f"task:{task_id}", f"session:{ev.session}", f"snapshot:Stop:{now}"],
        OUTCOME_REDUCER,
        OUTCOME_ASSUMPTIONS,
        warnings,
    )
    store.segment_close(task_id, rows, now, ev=evidence)
    notice = ev.pointers.get("turn_id") is None
    closed = _noted(result, "task closed")
    return Result(closed.outcome, closed.note, closed.head_sha, notice)


def _history(
    store: Store, cmap: ComponentMap, files: list[str], now: str
) -> dict[str, frozenset[str]]:
    """Each file's destinations at its latest scan under this map before ``now``.
    Rows arrive ordered by ts, so a later scan replaces an earlier one; (file,
    destination) is unique per scan (file_edges_once), so a set loses nothing."""
    wanted = set(files)
    latest: dict[str, tuple[str, set[str]]] = {}
    for row in store.edge_history(cmap.repo_id):
        f = row["src_file"]
        if f not in wanted or row["ts"] >= now or row["map_version"] != cmap.version:
            continue
        ts, dsts = latest.get(f, ("", set()))
        if row["ts"] != ts:
            ts, dsts = row["ts"], set()
        if row["dst_component"] is not None:
            dsts.add(row["dst_component"])
        latest[f] = (ts, dsts)
    return {f: frozenset(d) for f, (_, d) in latest.items()}


def _edge_change(
    files: list[str],
    before: dict[str, frozenset[str]],
    after: dict[str, frozenset[str] | None],
) -> tuple[int | None, int | None]:
    added = removed = 0
    for f in files:
        old, new = before.get(f), after.get(f)
        if old is None or new is None:
            return None, None
        added += len(new - old)
        removed += len(old - new)
    return added, removed


def _gaps(store: Store, session: str, since: str) -> list[str]:
    """recorder_gap (design 4.4 f) when a hook row since the bind is not ok or a
    mutating PreToolUse is still waiting for its PostToolUse."""
    return ["recorder_gap"] if store.recorder_gaps(session, since) else []


# -- attribution at a boundary --------------------------------------------------------


def _boundary(
    ev: _Event, store: Store, now: str, trigger: str
) -> tuple[Result, Snap | Unobservable]:
    base = store.session_base(ev.session)
    snap = _snap(ev, store, now, base)
    if isinstance(snap, Unobservable):
        return Result("unobservable", snap.reason), snap
    baseline = store.baseline_of(ev.session)
    agent, sources = store.agent_multiset(ev.session)
    moved = base is not None and _moved_outside_tool(store, ev.session, snap.head)
    extra = ["head_moved_outside_tool"] if moved else []
    rows = attribute(ev.session, now, trigger, snap, baseline, agent, sources, extra)
    if rows:
        store.attributions(rows)
    note = f"{len(rows)} files attributed at {trigger}"
    return Result("ok", note, snap.head), snap


def _moved_outside_tool(store: Store, session: str, head: str) -> bool:
    """A HEAD seen in this session that neither the base nor a tool call produced.
    A tool call produces the HEAD its PostToolUse saw, when its PreToolUse was seen;
    a pull or an owner commit between tool calls produces one nothing explains."""
    rows = store.head_sequence(session)
    pre = {r["tool_use_id"] for r in rows if r["event"] == "PreToolUse"}
    explained = {r["head_sha"] for r in rows if r["event"] == "SessionStart"}
    explained |= {
        r["head_sha"]
        for r in rows
        if r["event"] in ("PostToolUse", "PostToolUseFailure")
        and r["tool_use_id"] in pre
    }
    seen = {r["head_sha"] for r in rows} | {head}
    return not seen <= explained


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
    extra: Sequence[str] = (),
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
        warnings += extra
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
