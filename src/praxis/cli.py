"""The `praxis` command. A verb lands beside its integration test, never before it.

Argument parsing and dispatch only; every behaviour lives in the module that owns it.
`praxis hook` runs inside every agent tool call, so a verb's module is imported inside
its handler: task, probe, pool and report each cost a hook tens of milliseconds.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from praxis import __version__, doctor, hooks, install, store
from praxis.schema import MAP_REASONS
from praxis.unassisted import locate

if TYPE_CHECKING:
    from collections.abc import Callable


def main(argv: list[str] | None = None) -> int:
    # One clock reading at command start: task seconds are measured from it (E07).
    now = hooks.now_iso()
    parser = _parser()
    args = parser.parse_args(argv)
    verb = _VERBS.get(args.verb)
    if verb is None:
        parser.print_usage(sys.stdout)
        return 0
    return verb(args, now)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="praxis",
        description=(
            "Measures unassisted capability on the skills used with coding agents and "
            "chooses the agent's mode per skill."
        ),
    )
    parser.add_argument("--version", action="version", version=f"praxis {__version__}")
    sub = parser.add_subparsers(dest="verb")

    sub.add_parser(
        "hook", help="run as an agent hook: reads one payload on stdin, exits 0"
    )

    setup = sub.add_parser("setup", help="print or install the hook block for an agent")
    setup.add_argument("agent", choices=("claude", "codex"))
    setup.add_argument(
        "--print", action="store_true", dest="print_", help="print the block"
    )
    setup.add_argument(
        "--apply",
        action="store_true",
        help="merge the block into the file, with consent",
    )
    setup.add_argument(
        "--remove",
        action="store_true",
        help="take praxis out of the file, with consent",
    )

    sub.add_parser("doctor", help="what is installed, recorded and pending; read-only")

    attr = sub.add_parser("attributions", help="attribution rows from the ledger")
    attr.add_argument("--session", help="one session id")
    attr.add_argument("--json", action="store_true", help="one JSON object per line")

    sub.add_parser("sessions", help="sessions in the ledger with their latest totals")

    sub.add_parser("task", help="register the next task, before its prompt")
    sub.add_parser("last", help="feedback on the last closed task")
    probe = sub.add_parser("probe", help="an unassisted probe, with no agent attached")
    probe.add_argument("kind", help="which probe; an unknown kind lists them")
    comps = sub.add_parser("components", help="the frozen component map and its pool")
    actions = comps.add_subparsers(dest="action")
    freeze = actions.add_parser(
        "freeze", help="derive or load the map, freeze it and fill the item pool"
    )
    freeze.add_argument("--reason", choices=MAP_REASONS, help="why the map changed")
    derive = actions.add_parser(
        "derive", help="print the mechanical map as an owner map draft"
    )
    derive.add_argument(
        "--write", action="store_true", help="save the draft as the owner map, once"
    )
    scenarios = sub.add_parser("scenarios", help="the owner's runtime scenarios")
    scenarios.add_subparsers(dest="action").add_parser(
        "run", help="run each scenario in a scratch clone of HEAD and record its trace"
    )
    sub.add_parser("report", help="scores, references and tests from the ledger")
    return parser


def _hook(_args: argparse.Namespace, _now: str) -> int:
    return hooks.run(sys.stdin.buffer, store.home())


def _doctor(_args: argparse.Namespace, _now: str) -> int:
    return doctor.run(
        cwd=Path.cwd(), home=Path.home(), praxis_home=store.home(), out=sys.stdout
    )


def _task(_args: argparse.Namespace, now: str) -> int:
    from praxis import task

    return task.register(
        Path.cwd(), store.home(), sys.stdin, sys.stdout, now, task.SEED
    )


def _last(_args: argparse.Namespace, _now: str) -> int:
    from praxis import task

    return task.last(Path.cwd(), store.home(), sys.stdout)


def _probe(args: argparse.Namespace, now: str) -> int:
    from praxis import probe

    if args.kind not in probe.KINDS:
        print(f"praxis probe: kinds are {', '.join(probe.KINDS)}", file=sys.stderr)
        return 2
    return probe.administer(
        Path.cwd(), store.home(), sys.stdin, sys.stdout, now, args.kind
    )


def _components(args: argparse.Namespace, now: str) -> int:
    from praxis import pool

    if args.action == "freeze":
        return pool.freeze(Path.cwd(), store.home(), sys.stdout, now, args.reason)
    if args.action == "derive":
        return pool.draft(Path.cwd(), store.home(), sys.stdout, args.write)
    return pool.show(Path.cwd(), store.home(), sys.stdout)


def _scenarios(args: argparse.Namespace, now: str) -> int:
    from praxis import scenarios

    if args.action == "run":
        return scenarios.run(Path.cwd(), store.home(), sys.stdout, now)
    return scenarios.show(Path.cwd(), store.home(), sys.stdout)


def _report(_args: argparse.Namespace, _now: str) -> int:
    from praxis import report, task

    repo = locate(Path.cwd())
    if repo is None:
        print("praxis report: not inside a git repository")
        return 2
    ledger = store.Store(store.home() / "ledger.db").open()
    try:
        scores = report.scores(ledger, repo.repo_id, seed=task.SEED, draws=report.DRAWS)
    finally:
        ledger.close()
    try:
        report.render(scores, sys.stdout)
    except ValueError as exc:  # a refused word with no met rule: nothing was written
        print(exc, file=sys.stderr)
        return 1
    return 0


def _setup(args: argparse.Namespace, _now: str) -> int:
    agent = args.agent
    if args.apply or args.remove:
        return install.apply(
            agent=agent,
            remove=args.remove,
            home=Path.home(),
            praxis_home=store.home(),
            stdin=sys.stdin,
            out=sys.stdout,
        )
    target = f"~/{install.TARGETS[agent]}"
    if not args.print_:
        print(
            f"praxis setup {agent} --print   # the block to merge into {target}\n"
            f"praxis setup {agent} --apply   # merge it there, after your yes"
        )
        return 0
    print(json.dumps(install.snippet(agent), indent=2))
    return 0


def _attributions(args: argparse.Namespace, _now: str) -> int:
    ledger = store.Store(store.home() / "ledger.db").open()
    try:
        rows = ledger.attribution_rows(args.session)
    finally:
        ledger.close()
    if args.json:
        for r in rows:
            print(json.dumps(dict(r)))
        return 0
    print(
        "session  ts  trigger  file  agent  human  preexisting  ambiguous  reverted"
        "  coverage"
    )
    for r in rows:
        print(
            f"{r['session_id'][:8]}  {r['ts']}  {r['trigger']}  {r['file']}  "
            f"{r['agent']}  {_opt(r['human'])}  {_opt(r['preexisting'])}  "
            f"{r['ambiguous']}  {_opt(r['reverted'])}  {r['coverage']}"
        )
    return 0


def _sessions(_args: argparse.Namespace, _now: str) -> int:
    ledger = store.Store(store.home() / "ledger.db").open()
    try:
        sessions = ledger.sessions()
        print(
            "session  boundaries  agent  human  preexisting  ambiguous  reverted"
            "  (latest boundary)"
        )
        for s in sessions:
            rows = ledger.attribution_rows(s)
            if not rows:
                print(f"{s[:8]}  0  -  -  -  -  -")
                continue
            latest = rows[-1]["ts"]
            last = [r for r in rows if r["ts"] == latest]
            boundaries = len({r["ts"] for r in rows})
            print(
                f"{s[:8]}  {boundaries}  {sum(r['agent'] for r in last)}  "
                f"{_sum_opt([r['human'] for r in last])}  "
                f"{_sum_opt([r['preexisting'] for r in last])}  "
                f"{sum(r['ambiguous'] for r in last)}  "
                f"{_sum_opt([r['reverted'] for r in last])}"
            )
    finally:
        ledger.close()
    return 0


def _opt(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _sum_opt(values: list[int | None]) -> str:
    # One unknown makes the total unknown: a sum over NULLs is not a number.
    return (
        "unknown"
        if any(v is None for v in values)
        else str(sum(v or 0 for v in values))
    )


_VERBS: dict[str, Callable[[argparse.Namespace, str], int]] = {
    "hook": _hook,
    "setup": _setup,
    "doctor": _doctor,
    "attributions": _attributions,
    "sessions": _sessions,
    "task": _task,
    "last": _last,
    "probe": _probe,
    "components": _components,
    "scenarios": _scenarios,
    "report": _report,
}
