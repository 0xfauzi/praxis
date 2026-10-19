"""The `praxis` command: a verb, then an object (`praxis show status`). A command lands
beside its integration test, never before it.

Argument parsing and dispatch only; every behaviour lives in the module that owns it.
Every praxis module is imported inside its handler, for two reasons: `praxis hook` runs
inside every agent tool call and pays for each import, and the knockout tracer counts a
module body that runs at import as a component the scenario entered.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, NamedTuple, NoReturn

from praxis import __version__

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

# install.TARGETS names the same agents; parsing must not import install to learn them.
AGENTS = ("claude", "codex")
# The command object names the probe kind that the ledger stores (a technical identity).
TEST_KINDS = {
    "structure": "structural",
    "changes": "drift",
    "decisions": "decision",
    "knockout": "knockout",
    "pathfinder": "pathfinder",
}


class Option(NamedTuple):
    name: str  # "--json" is a flag, "--session" takes a value, "agent" is positional
    metavar: str | None


class Command(NamedTuple):
    verb: str
    obj: str | None
    args: str
    help: str
    handler: Callable[[argparse.Namespace], int]
    options: tuple[Option, ...] = ()

    @property
    def name(self) -> str:
        return " ".join(w for w in ("praxis", self.verb, self.obj) if w)

    @property
    def words(self) -> str:
        return f"{self.name} {self.args}".rstrip()


class _Parser(argparse.ArgumentParser):
    """argparse with the help and the refusals in the praxis words: the usage is the
    command list, and an unknown word prints that list."""

    def format_help(self) -> str:
        return self.format_usage()

    def error(self, message: str) -> NoReturn:
        option, _, problem = message.partition(": ")
        if problem == "expected one argument":  # argparse's words for a bare option
            message = f"write a value after {option.removeprefix('argument ')}."
        self.print_usage(sys.stderr)
        if not message.startswith("praxis "):
            message = f"{self.prog}: {message}"
        self.exit(2, message + "\n")

    def _check_value(self, action: argparse.Action, value: object) -> None:
        if action.choices is not None and value not in action.choices:
            raise argparse.ArgumentError(None, f"{self.prog} {value} is not a command.")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args, extra = parser.parse_known_args(argv)
    if args.verb is None:
        parser.print_usage(sys.stdout)
        return 0
    group = [c for c in COMMANDS if c.verb == args.verb]
    found = [c for c in group if c.obj == args.object]
    prog = " ".join(w for w in ("praxis", args.verb, args.object) if w)
    problem = _problem(found, args, extra)
    if problem is not None:
        return _refuse(found or group, prog, problem)
    (command,) = found
    return command.handler(args)


def _problem(
    found: list[Command], args: argparse.Namespace, extra: list[str]
) -> str | None:
    if extra:
        return f"these words are not part of a command: {' '.join(extra)}"
    if not found:
        return "the object is missing."
    if _AGENT[0] in found[0].options and args.agent is None:
        return "the agent is missing. Write claude or codex."
    return None


def _refuse(group: Sequence[Command], prog: str, message: str) -> int:
    sys.stderr.write(f"usage: {_usage(group)}\n{prog}: {message}\n")
    return 2


def _usage(commands: Sequence[Command]) -> str:
    width = max(len(c.words) for c in commands) + 2
    lines = [f"  {c.words:<{width}}{c.help}" for c in commands]
    if any("AGENT" in c.args for c in commands):
        lines.append("  AGENT is claude or codex.")
    return "\n".join(["praxis VERB OBJECT [OPTIONS]", "", *lines])


def _parser() -> _Parser:
    top = _Parser(prog="praxis", usage=_usage(COMMANDS), allow_abbrev=False)
    top.add_argument("--version", action="version", version=f"praxis {__version__}")
    verbs = top.add_subparsers(dest="verb", metavar="VERB", parser_class=_Parser)
    for verb in dict.fromkeys(c.verb for c in COMMANDS):
        group = [c for c in COMMANDS if c.verb == verb]
        parser = verbs.add_parser(
            verb, prog=f"praxis {verb}", usage=_usage(group), allow_abbrev=False
        )
        if group[0].obj is None:
            parser.set_defaults(object=None)
            _add_options(parser, group[0].options)
            continue
        objects = parser.add_subparsers(dest="object", metavar="OBJECT")
        for c in group:
            sub = objects.add_parser(
                str(c.obj), prog=c.name, usage=_usage([c]), allow_abbrev=False
            )
            _add_options(sub, c.options)
    return top


def _add_options(parser: argparse.ArgumentParser, options: tuple[Option, ...]) -> None:
    for o in options:
        if not o.name.startswith("-"):
            # Optional to argparse so that a missing agent gets the praxis refusal.
            parser.add_argument(o.name, nargs="?", choices=AGENTS)
        elif o.metavar is None:
            parser.add_argument(o.name, action="store_true")
        else:
            parser.add_argument(o.name, metavar=o.metavar)


def _now() -> str:
    # One clock reading at command start, before any prompt: task seconds are measured
    # from it (E07).
    from praxis.hooks import now_iso

    return now_iso()


def _home() -> Path:
    from praxis.store import home

    return home()


def _cwd() -> Path:
    from pathlib import Path

    return Path.cwd()


def _hook(_args: argparse.Namespace) -> int:
    from praxis import hooks

    return hooks.run(sys.stdin.buffer, _home())


def _show_install(args: argparse.Namespace) -> int:
    import json

    from praxis import install

    print(json.dumps(install.snippet(args.agent), indent=2))
    return 0


def _install(args: argparse.Namespace) -> int:
    return _write_settings(args.agent, remove=False)


def _remove(args: argparse.Namespace) -> int:
    return _write_settings(args.agent, remove=True)


def _write_settings(agent: str, *, remove: bool) -> int:
    from pathlib import Path

    from praxis import install

    return install.apply(
        agent=agent,
        remove=remove,
        home=Path.home(),
        praxis_home=_home(),
        stdin=sys.stdin,
        out=sys.stdout,
    )


def _show_status(_args: argparse.Namespace) -> int:
    from pathlib import Path

    from praxis import doctor

    return doctor.run(cwd=_cwd(), home=Path.home(), praxis_home=_home(), out=sys.stdout)


def _show_lines(args: argparse.Namespace) -> int:
    from praxis.store import Store

    ledger = Store(_home() / "ledger.db").open()
    try:
        rows = ledger.attribution_rows(args.session)
    finally:
        ledger.close()
    if args.json:
        import json

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


def _show_sessions(_args: argparse.Namespace) -> int:
    from praxis.store import Store

    ledger = Store(_home() / "ledger.db").open()
    try:
        print(
            "session  hook events  agent  human  preexisting  ambiguous  reverted"
            "  (at the last hook event)"
        )
        for s in ledger.sessions():
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


def _show_components(_args: argparse.Namespace) -> int:
    from praxis import pool

    return pool.show(_cwd(), _home(), sys.stdout)


def _set_components(args: argparse.Namespace) -> int:
    now = _now()
    from praxis import pool

    return pool.freeze(_cwd(), _home(), sys.stdout, now, args.reason)


def _make_components(args: argparse.Namespace) -> int:
    from praxis import pool

    return pool.draft(_cwd(), _home(), sys.stdout, args.write)


def _start_task(_args: argparse.Namespace) -> int:
    now = _now()
    from praxis import task

    return task.register(_cwd(), _home(), sys.stdin, sys.stdout, now, task.SEED)


def _show_task(_args: argparse.Namespace) -> int:
    from praxis import task

    return task.last(_cwd(), _home(), sys.stdout)


def _test(args: argparse.Namespace) -> int:
    now = _now()
    from praxis import probe

    kind = TEST_KINDS[args.object]
    return probe.administer(_cwd(), _home(), sys.stdin, sys.stdout, now, kind)


def _show_scenarios(_args: argparse.Namespace) -> int:
    from praxis import scenarios

    return scenarios.show(_cwd(), _home(), sys.stdout)


def _run_scenarios(_args: argparse.Namespace) -> int:
    now = _now()
    from praxis import scenarios

    return scenarios.run(_cwd(), _home(), sys.stdout, now)


def _show_report(_args: argparse.Namespace) -> int:
    from praxis import report, task
    from praxis.store import Store
    from praxis.unassisted import locate

    repo = locate(_cwd())
    if repo is None:
        print("praxis show report: run this command in a git repository.")
        return 2
    ledger = Store(_home() / "ledger.db").open()
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


_AGENT = (Option("agent", "AGENT"),)
# The grammar, in the order that the usage prints it: one line each, and each help is
# 20 words or fewer (test_cli counts them).
COMMANDS: tuple[Command, ...] = (
    Command("show", "status", "", "Show the hooks, the ledger counts, the component "
            "maps, the tasks and the tests to do.", _show_status),
    Command("show", "install", "AGENT", "Show the hook settings that the agent needs. "
            "This command writes no file.", _show_install, _AGENT),
    Command("install", None, "AGENT", "Write the hook settings into the agent settings "
            "file after you type yes.", _install, _AGENT),
    Command("remove", None, "AGENT", "Remove the praxis hook settings from the agent "
            "settings file after you type yes.", _remove, _AGENT),
    Command("make", "components", "[--write]", "Make a component map draft from the "
            "imports. With --write, write it to a file that you can edit.",
            _make_components, (Option("--write", None),)),
    Command("set", "components", "[--reason REASON]", "Set the component map and the "
            "test items for this repository.", _set_components,
            (Option("--reason", "REASON"),)),
    Command("show", "components", "", "Show the set component map and the count of "
            "test items that are left.", _show_components),
    Command("start", "task", "", "Record a task before you write its prompt. A "
            "treatment task asks for your predictions.", _start_task),
    Command("show", "task", "", "Show the result of the last closed task.", _show_task),
    Command("test", "structure", "", "Answer questions about the imports between "
            "components, with no agent attached.", _test),
    Command("test", "changes", "", "Select the components that changed in the last "
            "week, with no agent attached.", _test),
    Command("test", "decisions", "", "Select the answer that you gave to a past agent "
            "question, with no agent attached.", _test),
    Command("test", "knockout", "", "Select the component that a scenario cannot run "
            "without, with no agent attached.", _test),
    Command("test", "pathfinder", "", "Give a number for how closely each pair of "
            "components is related, with no agent attached.", _test),
    Command("show", "scenarios", "", "Show each scenario in the scenario file and its "
            "last run.", _show_scenarios),
    Command("run", "scenarios", "", "Run each scenario in a scratch clone of the "
            "repository. Praxis records the components that it enters.",
            _run_scenarios),
    Command("show", "report", "", "Show the scores and their analysis for this "
            "repository.", _show_report),
    Command("show", "lines", "[--session ID] [--json]", "Show the line counts by "
            "author for each file at each hook event.", _show_lines,
            (Option("--session", "ID"), Option("--json", None))),
    Command("show", "sessions", "", "Show each session with its line counts at its "
            "last hook event.", _show_sessions),
    Command("hook", None, "", "Record one hook event from the agent. Only the agent "
            "runs this command.", _hook),
)  # fmt: skip
