"""The `praxis` command. A verb lands beside its integration test, never before it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from praxis import __version__, doctor, drill, hooks, install, store


def main(argv: list[str] | None = None) -> int:
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

    dr = sub.add_parser("drill", help="one write-it item in this repository, no agent")
    dr.add_argument(
        "--test-cmd", help="the project's test command; remembered per repo"
    )
    dr.add_argument(
        "--min-agent-fraction",
        type=float,
        default=drill.DEFAULT_MIN_FRACTION,
        help="smallest share of a function's lines the agent must have written",
    )
    dr.add_argument(
        "--list", action="store_true", help="show candidates, change nothing"
    )
    dr.add_argument("--restore", action="store_true", help="restore a crashed drill")

    args = parser.parse_args(argv)
    if args.verb == "hook":
        return hooks.run(sys.stdin.buffer, store.home())
    if args.verb == "setup":
        return _setup(args.agent, args.print_, args.apply, args.remove)
    if args.verb == "doctor":
        return doctor.run(
            cwd=Path.cwd(), home=Path.home(), praxis_home=store.home(), out=sys.stdout
        )
    if args.verb == "attributions":
        return _attributions(args.session, args.json)
    if args.verb == "sessions":
        return _sessions()
    if args.verb == "drill":
        return _drill(args)
    parser.print_usage(sys.stdout)
    return 0


def _drill(args: argparse.Namespace) -> int:
    home = store.home()
    if args.restore:
        return drill.restore(home, sys.stdout)
    ledger = store.Store(home / "ledger.db").open()
    try:
        return drill.run(
            cwd=Path.cwd(),
            home=home,
            store=ledger,
            test_cmd=args.test_cmd,
            min_fraction=args.min_agent_fraction,
            list_only=args.list,
            stdin=sys.stdin,
            out=sys.stdout,
        )
    finally:
        ledger.close()


def _setup(agent: str, print_: bool, apply: bool, remove: bool) -> int:
    if apply or remove:
        return install.apply(
            agent=agent,
            remove=remove,
            home=Path.home(),
            praxis_home=store.home(),
            stdin=sys.stdin,
            out=sys.stdout,
        )
    target = f"~/{install.TARGETS[agent]}"
    if not print_:
        print(
            f"praxis setup {agent} --print   # the block to merge into {target}\n"
            f"praxis setup {agent} --apply   # merge it there, after your yes"
        )
        return 0
    print(json.dumps(install.snippet(agent), indent=2))
    return 0


def _attributions(session: str | None, as_json: bool) -> int:
    ledger = store.Store(store.home() / "ledger.db").open()
    try:
        rows = ledger.attribution_rows(session)
    finally:
        ledger.close()
    if as_json:
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


def _sessions() -> int:
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
