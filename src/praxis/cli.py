"""The `praxis` command. Phase 0 ships `version` only; every later verb is added beside
its integration test, never before it."""

from __future__ import annotations

import argparse
import sys

from praxis import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="praxis",
        description=(
            "Measures unassisted capability on the skills used with coding agents and "
            "chooses the agent's mode per skill."
        ),
    )
    parser.add_argument("--version", action="version", version=f"praxis {__version__}")
    parser.parse_args(argv)
    # argparse exits for --version; reaching here means no verb was given. Phase 0 has
    # none, so saying so is the whole command.
    parser.print_usage(sys.stdout)
    return 0
